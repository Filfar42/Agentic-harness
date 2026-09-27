"""Seconda interfaccia dell'harness, pensata per il telefono.

Non e' un secondo cervello: e' un **ponte**. Tutte le chiamate ``/api/*``
attraversano questo processo e finiscono sul server principale
(``python run.py``, di default su ``127.0.0.1:8123``), che resta l'unico a
leggere impostazioni, eseguire turni e scrivere le sessioni. E' questa scelta
che tiene le due interfacce sincronizzate per costruzione: un messaggio
spedito dal telefono appare sul desktop al prossimo refresh, e viceversa,
perche' lo stato ce n'e' uno solo -- quello del processo principale.

Lo streaming SSE viene girato pezzo per pezzo senza buffering, quindi i
passi di pensiero si vedono dal telefono mentre il modello li produce.

Avvio: ``python run_mobile.py`` (ascolta su 0.0.0.0:8200, visibile in LAN).
L'indirizzo del server principale si cambia con ``--upstream`` o con la
variabile d'ambiente ``HARNESS_UPSTREAM``.

L'accesso richiede il cookie del solo browser associato dal desktop.
Il QR e' monouso; la credenziale non compare in URL, manifest o log.
"""
from __future__ import annotations

import hashlib
import json
import os
import asyncio
import time
from pathlib import Path
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from core.mobile_pairing import DEVICE_TTL
from server.security import browser_request_allowed, API_TOKEN_ENV, API_TOKEN_HEADER, MAX_REQUEST_BYTES

WEB_MOBILE_DIR = Path(__file__).resolve().parent.parent / "web_mobile"
# I file che il telefono condivide con il desktop: la resa dei passi del
# lavoro dell'agente (righe dei tool, pensiero, blocchi). Una copia sola, in
# ``web/``: due copie di "com'e' fatto un passo" erano gia' divergite una volta.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COMUNI = {"passi.js": "text/javascript", "passi.css": "text/css"}

DEFAULT_UPSTREAM = "http://127.0.0.1:8123"

# Intestazioni che non hanno senso ritrasmettere: parlano della singola
# connessione, non della risposta. ``host`` in particolare va tolto, senno'
# il client a monte finirebbe nell'intestazione verso il server principale.
_HOP_BY_HOP = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "upgrade",
}


def upstream_base() -> str:
    return os.environ.get("HARNESS_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


# Un solo client per il processo: connection pooling verso l'upstream e,
# soprattutto, timeout di lettura infinito. Su un turno agentico fra un tool
# e l'altro possono passare minuti di silenzio sullo stream: il default di
# httpx (5 secondi) taglierebbe il filo a meta' ragionamento.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    # Singleton di modulo, e deve restarlo: il client tiene il pool di
    # connessioni verso l'harness e ha una chiusura asincrona che il ciclo di
    # vita dell'app chiama una volta sola. Farne un attributo di classe
    # significherebbe un'istanza da passare a ogni rotta per non guadagnare
    # niente.
    global _client  # noqa: PLW0603 - vedi sopra
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=f"{upstream_base()}/",
            timeout=httpx.Timeout(connect=5.0, read=45.0, write=30.0, pool=5.0),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
            headers={API_TOKEN_HEADER: os.environ.get(API_TOKEN_ENV, "")},
            trust_env=False,
        )
    return _client


async def close_client() -> None:
    global _client  # noqa: PLW0603 - la meta' dell'altro: si crea li', si chiude qui
    if _client is not None:
        await _client.aclose()
        _client = None


COOKIE = "harness_mobile_device"
COOKIE_MAX_AGE = DEVICE_TTL
PRIVATE_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def remember_device(response: Response, request: Request, credential: str) -> None:
    # Strict omette il cookie quando il telefono rientra da un link esterno,
    # pur conservandolo su disco. Lax consente la navigazione GET alla pagina;
    # il controllo di origine resta obbligatorio per API e associazione POST.
    response.set_cookie(COOKIE, credential, max_age=COOKIE_MAX_AGE,
                        expires=COOKIE_MAX_AGE, path="/", httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")


async def register_bridge() -> None:
    """La scheda desktop vede anche il ponte avviato con run_mobile.py."""
    while True:
        try:
            await get_client().post("api/mobile/bridge", json={
                "port": int(os.environ.get("HARNESS_MOBILE_PORT", "8200"))}, timeout=5)
        except httpx.HTTPError:
            pass  # il desktop potrebbe essere ancora in avvio
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(register_bridge())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await close_client()


app = FastAPI(title="Harness Mobile", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


async def device_authorized(credential: str) -> bool:
    if not credential or len(credential) > 128:
        return False
    try:
        response = await get_client().post("api/mobile/authorize",
                                           json={"credential": credential}, timeout=5)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Risposta non valida")
        return data.get("authorized") is True
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Desktop non raggiungibile. Riprova quando è acceso.") from exc


@app.middleware("http")
async def guardia_dispositivo(request: Request, call_next):
    # Link, preferiti e fotocamera possono navigare alla pagina iniziale.
    # Nessuna eccezione per fetch, iframe, API o richieste che modificano dati.
    public_navigation = (request.url.path in {"/", "/pair"} and request.method == "GET"
                         and request.headers.get("sec-fetch-mode", "navigate") == "navigate"
                         and request.headers.get("sec-fetch-dest", "document") == "document")
    if not public_navigation and not browser_request_allowed(request.scope, request.headers):
        return JSONResponse({"detail": "Origine non autorizzata."}, status_code=403)
    credential = request.cookies.get(COOKIE, "")
    try:
        allowed = request.url.path == "/pair" or await device_authorized(credential)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers={"Cache-Control": "no-store"})
    if not allowed:
        detail = "Dispositivo non associato. Apri Impostazioni → Mobile sul desktop e scansiona il QR."
        if request.url.path == "/":
            # Un vecchio cookie Strict può mancare solo nella navigazione iniziale.
            # La pagina riprova dalla propria origine; non crea nuove credenziali.
            return HTMLResponse((WEB_MOBILE_DIR / "access.html").read_text(encoding="utf-8"),
                                status_code=401, headers=PRIVATE_HEADERS)
        return JSONResponse({"detail": detail}, status_code=401, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers.update(PRIVATE_HEADERS)
    if request.url.path == "/" and request.method == "GET" and response.status_code == 200:
        remember_device(response, request, credential)
    return response


@app.post("/api/mobile/session")
async def resume_session(request: Request) -> JSONResponse:
    """Conferma il cookie già autorizzato dalla guardia e migra i vecchi Strict."""
    response = JSONResponse({"ok": True})
    remember_device(response, request, request.cookies[COOKIE])
    return response


@app.get("/pair")
def pairing_page() -> HTMLResponse:
    return HTMLResponse((WEB_MOBILE_DIR / "pair.html").read_text(encoding="utf-8"))


@app.post("/pair")
async def pair(request: Request) -> JSONResponse:
    try:
        # Un codice QR e' corto: non accettiamo upload su questa rotta pubblica.
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024:
                    raise HTTPException(413, "Richiesta troppo grande.")
        data = json.loads(body)
        code = data.get("code") if isinstance(data, dict) else None
        if not isinstance(code, str) or not 1 <= len(code) <= 128:
            raise HTTPException(400, "Codice non valido.")
        result = await get_client().post("api/mobile/claim", json={
            "code": code, "user_agent": request.headers.get("user-agent", "")[:512]}, timeout=5)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Codice non valido.") from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(503, "Desktop non raggiungibile. Riprova quando è acceso.") from exc
    if result.status_code != 200:
        return JSONResponse({"detail": "QR scaduto o già utilizzato. Generane uno nuovo dal desktop."}, status_code=401)
    response = JSONResponse({"ok": True})
    remember_device(response, request, result.json()["credential"])
    return response


# ---------------------------------------------------------------------------
# Stato del ponte
# ---------------------------------------------------------------------------


@app.get("/api/mobile/health")
async def health() -> JSONResponse:
    """Il telefono puo' distinguere 'rete giu'' da 'server principale spento'."""
    try:
        response = await get_client().get("api/sessions")
        reachable = response.status_code < 500
        detail: Any = None
    except httpx.HTTPError as exc:
        reachable = False
        detail = f"{type(exc).__name__}: {exc}"
    return JSONResponse(
        {
            "ok": reachable,
            "upstream": upstream_base(),
            "detail": detail,
        }
    )


# ---------------------------------------------------------------------------
# Il ponte: tutto il resto sotto /api passa attraverso
# ---------------------------------------------------------------------------


def _filtered_headers(headers: Any) -> dict[str, str]:
    connection_headers = {name.strip().lower() for name in headers.get("connection", "").split(",")}
    return {
        k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP | connection_headers
    }


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_api(path: str, request: Request) -> StreamingResponse:
    if path == "mobile" or path.startswith("mobile/"):
        raise HTTPException(403, "Gestisci l'associazione dal desktop.")
    async def read_body() -> bytes:
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "Richiesta troppo grande.")
            body.extend(block)
        return bytes(body)
    try:
        body = await asyncio.wait_for(read_body(), timeout=30.0)
    except TimeoutError as exc:
        raise HTTPException(408, "Timeout della richiesta.") from exc
    client = get_client()
    headers = {
        k: v for k, v in _filtered_headers(request.headers).items()
        if k.lower() not in {"cookie", "authorization", "x-harness-token", API_TOKEN_HEADER,
                             "origin", "referer", "sec-fetch-site", "forwarded", "x-forwarded-for",
                             "x-harness-mobile"}
    }
    headers["origin"] = str(client.base_url).rstrip("/")
    headers[API_TOKEN_HEADER] = os.environ.get(API_TOKEN_ENV, "")
    headers["x-harness-mobile"] = "1"
    # Scarta anche il vecchio parametro di accesso: non autentica più e non
    # deve finire nei log del desktop.
    parametri = {k: v for k, v in request.query_params.items() if k != "k"}
    try:
        # Il pattern cattura il percorso DOPO "/api": va rimesso il prefisso,
        # senno' "/api/sessions" finirebbe su "/sessions" upstream (404).
        upstream_response = await client.send(
            client.build_request(
                request.method,
                f"api/{path}",
                params=parametri,
                content=body if body else None,
                headers=headers,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        # Errore chiaro e in italiano: sul telefono deve capirsi al volo se
        # il problema e' la rete o il server principale non avviato.
        raise HTTPException(
            502,
            f"Server principale non raggiungibile su {upstream_base()} "
            f"({type(exc).__name__}). Avvialo con 'python run.py'.",
        ) from exc

    async def flow() -> AsyncIterator[bytes]:
        try:
            # Anche una connessione SSE gia' aperta perde accesso dopo la revoca.
            iterator = upstream_response.aiter_bytes().__aiter__()
            pending = asyncio.create_task(anext(iterator, None))
            checked_at = time.monotonic()
            try:
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=1)
                    if time.monotonic() - checked_at >= 1:
                        try:
                            allowed = await device_authorized(request.cookies.get(COOKIE, ""))
                        except HTTPException:
                            allowed = False
                        if not allowed:
                            break
                        checked_at = time.monotonic()
                    if done:
                        chunk = pending.result()
                        if chunk is None:
                            break
                        yield chunk
                        pending = asyncio.create_task(anext(iterator, None))
            finally:
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
        finally:
            await upstream_response.aclose()

    # Niente media_type esplicito: il Content-Type dell'upstream (compreso
    # text/event-stream) viaggia gia' nelle intestazioni filtrate.
    return StreamingResponse(
        flow(),
        status_code=upstream_response.status_code,
        headers={k: v for k, v in _filtered_headers(upstream_response.headers).items()
                 if k.lower() not in {"content-encoding", "set-cookie"}},
    )


# ---------------------------------------------------------------------------
# La pagina
# ---------------------------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory=str(WEB_MOBILE_DIR)),
    name="static-mobile",
)


@app.get("/comune/{nome}")
def comune(nome: str) -> Response:
    """Un file condiviso con il desktop (``COMUNI``), letto da ``web/``.

    Un elenco chiuso e non una seconda cartella montata: da qui deve uscire
    quello che il telefono usa, non tutto ``web/``."""
    tipo = COMUNI.get(nome)
    if tipo is None:
        raise HTTPException(404, "File non trovato.")
    try:
        corpo = (WEB_DIR / nome).read_bytes()
    except OSError as exc:
        raise HTTPException(404, "File non trovato.") from exc
    return Response(corpo, media_type=f"{tipo}; charset=utf-8")


def _asset_version() -> str:
    """Impronta di app.js e style.css: cambia il file, cambia l'URL.

    Senza questo il browser del telefono continua a eseguire il vecchio
    JavaScript per ore (la freschezza euristica e' aggressiva su LAN) e un
    fix all'interfaccia "non si vede". Con ``?v=`` ogni modifica invalida
    da sola la cache.
    """
    imprint = hashlib.sha256()
    for cartella, nome in ((WEB_MOBILE_DIR, "app.js"), (WEB_MOBILE_DIR, "style.css"),
                           *((WEB_DIR, n) for n in COMUNI)):
        try:
            imprint.update((cartella / nome).read_bytes())
        except OSError:
            imprint.update(nome.encode())
    return imprint.hexdigest()[:10]


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    """Nessuna credenziale nel manifest: l'accesso resta nel cookie HttpOnly."""
    try:
        dati = json.loads(
            (WEB_MOBILE_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        dati = {"name": "Harness (manifest mancante)", "display": "standalone"}
    dati["start_url"] = "/"
    return Response(
        json.dumps(dati, ensure_ascii=False, indent=2),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/")
def index() -> Response:
    """La pagina, con gli asset referenziati alla loro versione esatta."""
    html = (WEB_MOBILE_DIR / "index.html").read_text(encoding="utf-8")
    versione = _asset_version()
    html = html.replace("/static/app.js", f"/static/app.js?v={versione}")
    html = html.replace("/static/style.css", f"/static/style.css?v={versione}")
    for nome in COMUNI:
        html = html.replace(f"/comune/{nome}", f"/comune/{nome}?v={versione}")
    return Response(
        html,
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )
