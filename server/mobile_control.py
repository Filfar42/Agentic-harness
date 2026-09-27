"""Controllo dal desktop e autorità unica per l'associazione del telefono.

Queste rotte non sono mai inoltrate dal ponte mobile. Solo il processo desktop
scrive l'associazione, anche quando run_mobile.py gira in un altro processo.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
from pathlib import Path

import qrcode
import qrcode.image.svg
import uvicorn
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import settings
from core.mobile_pairing import MobilePairing, PairingError

router = APIRouter(prefix="/api/mobile")
_stores: dict[Path, MobilePairing] = {}
_lock = threading.RLock()
_server: uvicorn.Server | None = None
_thread: threading.Thread | None = None
_lease: tuple[int, float] | None = None


def pairing() -> MobilePairing:
    path = Path(settings.SETTINGS_FILE).with_suffix(".mobile.json").resolve()
    with _lock:
        if path not in _stores:
            _stores[path] = MobilePairing(path)
        return _stores[path]


def reply(data: dict) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def bridge_port() -> int | None:
    if _server and _server.started and not _server.should_exit and _thread and _thread.is_alive():
        return _server.config.port
    if not _server and _lease and _lease[1] > time.monotonic():
        return _lease[0]
    return None


def start_bridge(upstream: str, port: int | None = None) -> None:
    global _server, _thread  # noqa: PLW0603 - ciclo di vita del ponte unico
    with _lock:
        if bridge_port():
            return
        port = port or int(os.environ.get("HARNESS_MOBILE_PORT", "8200"))
        if not 1 <= port <= 65535:
            raise HTTPException(400, "La porta mobile deve essere compresa tra 1 e 65535.")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", port))
            sock.listen(128)
        except OSError as exc:
            sock.close()
            raise HTTPException(409, f"Porta mobile {port} non disponibile. Chiudi l'altro servizio o usa --mobile-port.") from exc
        os.environ["HARNESS_UPSTREAM"] = upstream
        os.environ["HARNESS_MOBILE_PORT"] = str(port)
        _server = uvicorn.Server(uvicorn.Config("server.mobile:app", host="0.0.0.0", port=port,
                                log_level="warning", timeout_graceful_shutdown=2))
        server = _server

        def run() -> None:
            try:
                server.run(sockets=[sock])
            finally:
                sock.close()

        _thread = threading.Thread(target=run, daemon=True, name="interfaccia-mobile")
        _thread.start()
        deadline = time.monotonic() + 5
        while not server.started and _thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            server.should_exit = True
            raise HTTPException(503, "L'interfaccia mobile non è riuscita ad avviarsi.")


def shutdown() -> None:
    global _server, _thread, _lease  # noqa: PLW0603 - ciclo di vita del ponte unico
    with _lock:
        if _server:
            _server.should_exit = True
        if _thread:
            _thread.join(timeout=4)
        _server = None
        _thread = None
        _lease = None


@router.get("")
def status() -> JSONResponse:
    from run_mobile import ip_lan

    port = bridge_port()
    address = ip_lan()
    try:
        data = pairing().status()
    except PairingError as exc:
        data = {"device": None, "pending_expires_at": None, "error": str(exc)}
    return reply({**data, "running": port is not None, "desktop": socket.gethostname(),
                  "url": f"http://{address}:{port}/" if port and address != "127.0.0.1" else None})


@router.post("/pairing")
def create_pairing(request: Request) -> JSONResponse:
    # Il socket del server identifica il desktop corrente: mai l'Host della richiesta
    # o HARNESS_UPSTREAM, che potrebbe appartenere a un'altra istanza.
    port = (request.scope.get("server") or ("127.0.0.1", 8123))[1]
    start_bridge(f"http://127.0.0.1:{port}")
    info = json.loads(status().body)
    if not info["url"]:
        raise HTTPException(409, "Nessun indirizzo di rete disponibile. Collega desktop e telefono alla stessa rete Wi-Fi.")
    try:
        code, expires = pairing().create()
    except PairingError as exc:
        raise HTTPException(409, str(exc)) from exc
    url = info["url"] + "pair#" + code
    qr = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathFillImage, border=4)
    svg = base64.b64encode(qr.to_string()).decode("ascii")
    return reply({"url": url, "expires_at": expires, "qr": "data:image/svg+xml;base64," + svg})


@router.post("/start")
def start(request: Request) -> JSONResponse:
    port = (request.scope.get("server") or ("127.0.0.1", 8123))[1]
    start_bridge(f"http://127.0.0.1:{port}")
    return status()


@router.delete("/pairing")
def revoke() -> JSONResponse:
    pairing().revoke()
    return reply({"ok": True})


class Claim(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    user_agent: str = Field(default="", max_length=512)


@router.post("/claim")
def claim(body: Claim) -> JSONResponse:
    try:
        return reply({"credential": pairing().claim(body.code, body.user_agent)})
    except PairingError as exc:
        raise HTTPException(401, str(exc)) from exc


class Credential(BaseModel):
    credential: str = Field(default="", max_length=128)


@router.post("/authorize")
def authorize(body: Credential) -> JSONResponse:
    try:
        allowed = pairing().authorized(body.credential)
    except PairingError:
        allowed = False
    return reply({"authorized": allowed})


class Bridge(BaseModel):
    port: int = Field(ge=1, le=65535)


@router.post("/bridge")
def register_bridge(body: Bridge) -> JSONResponse:
    global _lease  # noqa: PLW0603 - registrazione periodica del processo standalone
    _lease = (body.port, time.monotonic() + 25)
    return reply({"ok": True})
