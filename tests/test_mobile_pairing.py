"""QR monouso, un solo browser, revoca e confine fra ponte e desktop."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from core.mobile_pairing import MobilePairing, PairingError
from server import mobile_control, mobile


@pytest.fixture
def desktop(monkeypatch):
    from server.main import app

    monkeypatch.setattr(mobile_control, "bridge_port", lambda: 8200)
    monkeypatch.setattr(mobile_control, "start_bridge", lambda *_args: None)
    monkeypatch.setattr("run_mobile.ip_lan", lambda: "192.168.1.20")
    return TestClient(app)


@pytest.fixture
def phone(desktop, monkeypatch):
    from server.main import app

    monkeypatch.setattr(mobile, "_client", httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"))
    with TestClient(mobile.app) as client:
        yield client


def qr_code(desktop):
    result = desktop.post("/api/mobile/pairing")
    assert result.status_code == 200
    data = result.json()
    assert data["url"].startswith("http://192.168.1.20:8200/pair#")
    svg = base64.b64decode(data["qr"].split(",")[1])
    assert b"<svg" in svg and b"<path" in svg
    assert 290 < data["expires_at"] - time.time() <= 300
    assert result.headers["cache-control"] == "no-store"
    return data["url"].split("#")[1]


def test_real_pairing_cookie_single_device_and_revoke(desktop, phone):
    code = qr_code(desktop)
    assert phone.get("/pair").status_code == 200
    assert phone.get("/").status_code == 401
    result = phone.post("/pair", json={"code": code}, headers={"user-agent": "iPhone Safari"})
    assert result.status_code == 200
    cookie = result.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age=31536000" in cookie and "expires=" in cookie and "path=/" in cookie
    credential = phone.cookies.get(mobile.COOKIE)
    assert credential not in result.text
    assert phone.get("/").status_code == 200
    status = desktop.get("/api/mobile").json()
    assert status["device"]["name"] == "iPhone"
    assert status["pending_expires_at"] is None
    assert credential not in json.dumps(status)
    assert code not in mobile_control.pairing().path.read_text()
    assert credential not in mobile_control.pairing().path.read_text()
    # Anche con una seconda sessione HTTP, il QR non apre piu' la pagina.
    # La lifespan appartiene al server della fixture, non al secondo browser.
    other = TestClient(mobile.app)
    try:
        assert other.post("/pair", json={"code": code}).status_code == 401
        assert other.get("/").status_code == 401
    finally:
        other.close()
    assert desktop.post("/api/mobile/pairing").status_code == 409
    assert desktop.delete("/api/mobile/pairing").status_code == 200
    assert phone.get("/").status_code == 401
    assert phone.get("/static/app.js").status_code == 401
    assert phone.get("/api/sessions").status_code == 401


def test_restart_and_expiration(tmp_path, monkeypatch):
    path = tmp_path / "pair.json"
    store = MobilePairing(path)
    code, _ = store.create()
    credential = store.claim(code, "Android")
    restored = MobilePairing(path)
    assert restored.authorized(credential)
    assert restored.status()["device"]["name"] == "Android"
    with pytest.raises(PairingError):
        restored.create()
    monkeypatch.setattr("core.mobile_pairing.time.time", lambda: time_now + 366 * 86400)
    # Non chiamare time.time dopo averlo sostituito: il modulo time e' condiviso.
    time_now = store._read()["paired_at"]
    assert not restored.authorized(credential)
    assert restored.create()


def test_qr_expiry_regeneration_cancellation_and_race(tmp_path, monkeypatch):
    store = MobilePairing(tmp_path / "pair.json")
    old, _ = store.create()
    code, expires = store.create()
    with pytest.raises(PairingError):
        store.claim(old, "")
    with monkeypatch.context() as patch:
        patch.setattr("core.mobile_pairing.time.time", lambda: expires + 1)
        with pytest.raises(PairingError):
            store.claim(code, "")
    code, _ = store.create()
    store.revoke()
    with pytest.raises(PairingError):
        store.claim(code, "")
    code, _ = store.create()

    def try_claim(_):
        try:
            return store.claim(code, "")
        except PairingError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(try_claim, range(8)))
    assert len([result for result in results if result]) == 1


def test_device_is_bound_to_desktop_store(tmp_path):
    first = MobilePairing(tmp_path / "a.json")
    second = MobilePairing(tmp_path / "b.json")
    code, _ = first.create()
    credential = first.claim(code, "")
    assert first.authorized(credential)
    assert not second.authorized(credential)


def test_mobile_cannot_manage_pairing_even_using_normalized_path(desktop, phone):
    phone.post("/pair", json={"code": qr_code(desktop)})
    for path in ("/api/mobile", "/api/mobile/pairing", "/api/mobile/authorize",
                 "/api/mobile/claim", "/api/mobile/bridge", "/api/mobile/start",
                 "/api/x/%2e%2e/mobile/pairing"):
        assert phone.post(path, json={"code": "x"}).status_code == 403, path
    assert phone.delete("/api/mobile/pairing").status_code == 403
    # Origini estranee non possono neanche consumare il QR pubblico.
    assert phone.post("/pair", json={"code": "x"}, headers={"Origin": "https://evil.test"}).status_code == 403


def test_legacy_tokens_and_malformed_input_cannot_bypass_pairing(desktop, phone, monkeypatch):
    monkeypatch.setenv("HARNESS_MOBILE_TOKEN", "old-shared-secret")
    phone.cookies.set("harness_mobile", "old-shared-secret")
    for path in ("/?k=old-shared-secret", "/api/sessions?k=old-shared-secret", "/static/app.js"):
        assert phone.get(path, headers={"X-Harness-Token": "old-shared-secret"}).status_code == 401
    for payload in ({}, [], {"code": 2}, {"code": ""}, {"code": "x" * 129}):
        assert phone.post("/pair", json=payload).status_code == 400
    assert phone.post("/pair", json={"code": "àè日本語"}).status_code == 401
    assert phone.post("/pair", content="x" * 1025).status_code == 413


@pytest.mark.parametrize("corrupt", ["{broken", '{"expires_at": "tomorrow"}', '{"expires_at": 9999999999}'])
def test_corrupt_store_fails_closed_and_can_be_revoked(desktop, phone, corrupt):
    store = mobile_control.pairing()
    store.path.write_text(corrupt)
    assert desktop.get("/api/mobile").json()["error"]
    assert desktop.post("/api/mobile/pairing").status_code == 409
    assert phone.get("/api/sessions").status_code == 401
    assert desktop.delete("/api/mobile/pairing").status_code == 200
    assert qr_code(desktop)


def test_live_stream_stops_when_device_is_revoked(monkeypatch):
    """Verifica anche uno stream fermo fra due frame, senza aspettare dati nuovi."""
    from starlette.requests import Request

    class QuietStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            await asyncio.sleep(20)
            yield b"too late"

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=QuietStream())), base_url="http://desktop") as upstream:
            monkeypatch.setattr(mobile, "_client", upstream)
            async def revoked(_credential):
                return False
            monkeypatch.setattr(mobile, "device_authorized", revoked)
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            request = Request({"type": "http", "method": "GET", "path": "/api/events",
                               "query_string": b"", "headers": []}, receive)
            response = await mobile.proxy_api("events", request)
            chunks = []
            async with asyncio.timeout(3):
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
            assert chunks == [b"first"]
    asyncio.run(run())


def test_qr_requires_lan_address(desktop, monkeypatch):
    monkeypatch.setattr("run_mobile.ip_lan", lambda: "127.0.0.1")
    response = desktop.post("/api/mobile/pairing")
    assert response.status_code == 409
    assert "rete" in response.json()["detail"]
    assert not mobile_control.pairing().status()["pending_expires_at"]


def test_camera_can_open_pairing_page_from_another_origin(desktop, phone):
    assert phone.get("/pair", headers={"sec-fetch-site": "cross-site"}).status_code == 200
    assert phone.post("/pair", json={"code": qr_code(desktop)},
                      headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_reopening_from_home_navigation_keeps_the_device(desktop, phone):
    phone.post("/pair", json={"code": qr_code(desktop)})
    credential = phone.cookies.get(mobile.COOKIE)
    device = desktop.get("/api/mobile").json()["device"]
    response = phone.get("/", headers={"sec-fetch-site": "cross-site",
                                       "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"})
    assert response.status_code == 200
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert phone.cookies.get(mobile.COOKIE) == credential
    assert desktop.get("/api/mobile").json()["device"] == device


def test_resume_migrates_existing_cookie_without_new_pairing(desktop, phone):
    phone.post("/pair", json={"code": qr_code(desktop)})
    credential = phone.cookies.get(mobile.COOKIE)
    device = desktop.get("/api/mobile").json()["device"]
    response = phone.post("/api/mobile/session")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert response.headers["cache-control"] == "no-store"
    assert phone.cookies.get(mobile.COOKIE) == credential
    assert credential not in response.text
    assert desktop.get("/api/mobile").json()["device"] == device
    desktop.delete("/api/mobile/pairing")
    assert phone.post("/api/mobile/session").status_code == 401
    assert not mobile_control.pairing().status()["device"]


def test_navigation_exception_never_allows_foreign_api_or_iframe(desktop, phone):
    phone.post("/pair", json={"code": qr_code(desktop)})
    for path, mode, dest in (("/", "cors", "empty"), ("/", "navigate", "iframe"),
                             ("/api/sessions", "navigate", "document")):
        assert phone.get(path, headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": mode,
                                        "sec-fetch-dest": dest}).status_code == 403
    assert phone.post("/api/mobile/session", headers={"Origin": "https://foreign.test"}).status_code == 403
    assert phone.post("/api/mobile/session", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert phone.post("/pair", json={"code": "old"}, headers={"Origin": "https://foreign.test"}).status_code == 403


def test_resume_page_cannot_associate_a_different_browser(desktop, phone):
    phone.post("/pair", json={"code": qr_code(desktop)})
    device = desktop.get("/api/mobile").json()["device"]
    phone.cookies.clear()
    response = phone.get("/")
    assert response.status_code == 401
    assert "Ricollegamento al desktop" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert phone.post("/api/mobile/session").status_code == 401
    assert not phone.cookies
    assert desktop.get("/api/mobile").json()["device"] == device


def test_unreachable_desktop_does_not_look_like_a_revoked_device(phone, monkeypatch):
    upstream = mobile._client
    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(upstream, "post", unavailable)
    phone.cookies.set(mobile.COOKIE, "already-paired")
    response = phone.get("/api/sessions")
    assert response.status_code == 503
    assert "Desktop non raggiungibile" in response.json()["detail"]


def test_port_in_use_reports_error_without_starting_another_bridge(monkeypatch):
    import socket

    monkeypatch.setattr(mobile_control, "_server", None)
    monkeypatch.setattr(mobile_control, "_lease", None)
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        sock.listen(1)
        with pytest.raises(mobile_control.HTTPException) as exc:
            mobile_control.start_bridge("http://127.0.0.1:8123", sock.getsockname()[1])
        assert exc.value.status_code == 409
        assert mobile_control._server is None


def test_restart_reopens_paired_bridge_and_port_error_does_not_block_desktop(monkeypatch):
    from server import main

    code, _ = mobile_control.pairing().create()
    mobile_control.pairing().claim(code, "")
    monkeypatch.setenv("HARNESS_DESKTOP_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("HARNESS_MOBILE_AUTOSTART", "0")
    monkeypatch.setattr(main, "remember_workspace", lambda: None)
    monkeypatch.setattr(main, "autostart_docker", lambda: None)
    monkeypatch.setattr(main.sandbox_mod, "dimentica_marchi_vivi", lambda: None)
    monkeypatch.setattr(mobile_control, "shutdown", lambda: None)
    started = []

    def start(upstream):
        started.append(upstream)
        raise mobile_control.HTTPException(409, "Porta occupata")

    monkeypatch.setattr(mobile_control, "start_bridge", start)
    with TestClient(main.app) as client:
        assert client.get("/api/mobile").status_code == 200
    assert started == ["http://127.0.0.1:9000"]
