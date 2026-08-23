"""Test del ponte mobile: la pagina, il passaggio delle API e lo streaming.

Il proxy viene provato **senza rete**: l'``httpx.AsyncClient`` interno viene
puntato, tramite ``ASGITransport``, direttamente sull'app FastAPI del server
principale costruita come fa ``tests/test_server.py`` (stesso finto Ollama,
stesso isolamento delle cartelle). Cosi' il percorso provato e' quello vero --
telefono -> ponte -> principale -- tolta solo la socket TCP.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake  # noqa: E402  (riusa il finto Ollama)
from core import config as config_mod  # noqa: E402


# Chiave del ponte usata da tutti i test: fissa e nota, invece di quella
# generata a caso all'avvio.
CHIAVE = "chiave-di-prova-del-ponte"


@pytest.fixture()
def mobile(fake_ollama, tmp_path, monkeypatch):
    """Ponte mobile davanti al server principale isolato."""
    url, _ = fake_ollama
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from server import main as server_main
    from server import mobile as mobile_mod
    from server.runner import RunnerRegistry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    server_main.RUNNERS = RunnerRegistry()
    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {
            "api_base": url,
            "transport": "ollama",
            "model_name": "fake:latest",
            "workspace_dir": str(workspace),
            "timeout_seconds": 20,
            "docker_autostart": False,
        }
    )

    # Il client del ponte parla direttamente con l'app del principale.
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_main.app),
        base_url="http://principale/",
    )
    monkeypatch.setattr(mobile_mod, "_client", upstream_client)

    # La chiave del ponte: fissata qui invece di lasciarla generare, cosi' i
    # test la conoscono. Viaggia come intestazione, che e' il terzo modo
    # previsto (query, cookie, header) ed e' quello comodo per uno script.
    monkeypatch.setenv(mobile_mod.TOKEN_ENV, CHIAVE)

    with TestClient(mobile_mod.app, headers={"X-Harness-Token": CHIAVE}) as test_client:
        test_client.mobile = mobile_mod
        test_client.server = server_main
        yield test_client

    # Il TestClient chiude il ponte (shutdown -> close_client): il client
    # sostituito va chiuso a mano perche' non e' quello gestito dal modulo.
    import asyncio

    asyncio.get_event_loop_policy()
    try:
        asyncio.new_event_loop().run_until_complete(upstream_client.aclose())
    except Exception:
        pass


fake_ollama = fake.fake_ollama  # riesporta la fixture


def read_sse(response_text: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# La pagina e i suoi asset
# ---------------------------------------------------------------------------


def test_index_and_static_assets_are_served(mobile):
    page = mobile.get("/")
    assert page.status_code == 200
    assert "Harness Mobile" in page.text
    # Gli asset sono referenziati con la loro impronta: e' il cache-busting
    # che costringe il telefono a scaricare il JS nuovo dopo ogni modifica.
    versione = mobile.mobile._asset_version()
    assert f'src="/static/app.js?v={versione}"' in page.text
    assert f'href="/static/style.css?v={versione}"' in page.text
    # E l'URL versionato risponde davvero.
    assert mobile.get(f"/static/app.js?v={versione}").status_code == 200

    js = mobile.get("/static/app.js")
    css = mobile.get("/static/style.css")
    assert js.status_code == 200 and css.status_code == 200
    assert "api/chat" in js.text  # e' il client che parla col principale


# ---------------------------------------------------------------------------
# Elenco conversazioni attraverso il ponte
# ---------------------------------------------------------------------------


def test_session_created_via_mobile_is_listed(mobile):
    """Una chat nata dal telefono entra nell'elenco dopo il primo turno:
    il file di sessione nasce col primo salvataggio su disco (come sul
    desktop), quindi prima del primo messaggio non puo' esserci."""
    created = mobile.post("/api/sessions").json()
    session_id = created["session_id"]
    assert session_id

    # finche' non c'e' un salvataggio, l'indice e' vuoto: e' il comportamento
    # del principale, il ponte non lo cambia
    assert mobile.get("/api/sessions").json()["sessions"] == []

    started = mobile.post(
        "/api/chat", json={"session_id": session_id, "prompt": "ciao"}
    )
    assert started.status_code == 200, started.text

    def listed_ids():
        return [s["id"] for s in mobile.get("/api/sessions").json()["sessions"]]

    assert wait_until(lambda: session_id in listed_ids())


def test_mobile_sees_chats_created_by_the_desktop(mobile):
    """Sincronizzazione desktop -> telefono: una chat nata sul desktop si
    vede dal telefono perche' lo stato e' uno solo."""
    desktop_id = mobile.post("/api/sessions").json()["session_id"]
    mobile.post(
        "/api/chat", json={"session_id": desktop_id, "prompt": "dal desktop"}
    )
    # Il turno parte in background: basta che la lista la mostri subito.
    assert wait_until(
        lambda: mobile.get("/api/sessions").json()["sessions"][0]["running"]
        is True
    )


# ---------------------------------------------------------------------------
# Turno completo dal telefono: messaggio, stream, lettura
# ---------------------------------------------------------------------------


def test_full_turn_through_the_bridge(mobile):
    session_id = mobile.post("/api/sessions").json()["session_id"]

    started = mobile.post(
        "/api/chat", json={"session_id": session_id, "prompt": "dimmi due parole"}
    )
    assert started.status_code == 200, started.text

    events = read_sse(mobile.get(f"/api/stream/{session_id}").text)
    types = [ev["type"] for ev in events]
    assert "start" in types
    assert "done" in types or "error" in types

    opened = mobile.post(f"/api/sessions/{session_id}/open", json={}).json()
    ruoli_e_testi = [(m["role"], m.get("content", "")) for m in opened["messages"]]
    assert ("user", "dimmi due parole") in ruoli_e_testi
    assert any(role == "assistant" and text for role, text in ruoli_e_testi)


def test_answer_flows_to_the_pending_question(mobile):
    """La domanda dell'agente arriva sul telefono e la risposta riparte."""
    original = fake.SCRIPT
    fake.SCRIPT = [
        # passo 1: l'agente sospende con una domanda
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "ask_user_question",
                                "arguments": {
                                    "question": "Quale formato?",
                                    "options": ["TOML", "JSON"],
                                },
                            }
                        }
                    ],
                }
            }
        ],
        # passo 2 (dopo la risposta): conclude
        [{"message": {"content": "Fatto."}}],
    ]
    try:
        session_id = mobile.post("/api/sessions").json()["session_id"]
        mobile.post(
            "/api/chat", json={"session_id": session_id, "prompt": "chiedimelo"}
        )

        def pending():
            payload = mobile.post(
                f"/api/sessions/{session_id}/open", json={}
            ).json()
            return payload["pending"]

        assert wait_until(lambda: pending() is not None)
        # Valori attesi espliciti: la card del telefono deve potersi costruire
        # con domanda, scelte e modalita' multipla che l'agente ha passato.
        # Le opzioni arrivano gia' normalizzate in {label, description}.
        assert pending()["question"] == "Quale formato?"
        assert pending()["options"] == [
            {"label": "TOML", "description": ""},
            {"label": "JSON", "description": ""},
        ]
        assert pending()["allow_multiple"] is False

        answered = mobile.post(
            "/api/answer", json={"session_id": session_id, "answer": "TOML"}
        )
        assert answered.status_code == 200, answered.text

        # la risposta riparte e consuma la domanda: quando il secondo passo
        # e' finito non c'e' piu' nulla di pendente
        assert wait_until(lambda: pending() is None)
    finally:
        fake.SCRIPT = original


def test_question_frame_on_the_stream_carries_the_whole_card(mobile):
    """L'evento 'question' sullo stream porta domanda, scelte e modalita'."""
    original = fake.SCRIPT
    fake.SCRIPT = [
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "ask_user_question",
                                "arguments": {
                                    "question": "Quale formato?",
                                    "options": ["TOML", "JSON"],
                                },
                            }
                        }
                    ],
                }
            }
        ],
        [{"message": {"content": "Fatto."}}],
    ]
    try:
        session_id = mobile.post("/api/sessions").json()["session_id"]
        mobile.post(
            "/api/chat", json={"session_id": session_id, "prompt": "chiedimelo"}
        )

        def sospeso():
            return wait_until(
                lambda: not mobile.get("/api/sessions").json()["sessions"][0][
                    "running"
                ]
            )

        assert sospeso()
        eventi = read_sse(mobile.get(f"/api/stream/{session_id}").text)
        domande = [ev for ev in eventi if ev["type"] == "question"]
        assert len(domande) == 1
        # La card del telefono si costruisce da questo frame solo se contiene
        # tutto: testo della domanda, scelte normalizzate e modalita'.
        assert domande[0]["question"] == "Quale formato?"
        assert domande[0]["options"] == [
            {"label": "TOML", "description": ""},
            {"label": "JSON", "description": ""},
        ]
        assert domande[0]["allow_multiple"] is False
        assert any(
            ev["type"] == "done" and ev.get("reason") == "awaiting_user"
            for ev in eventi
        )
    finally:
        fake.SCRIPT = original


# ---------------------------------------------------------------------------
# Server principale spento: errori chiari, non pagine bianche
# ---------------------------------------------------------------------------


def test_health_reports_unreachable_upstream(mobile):
    import httpx as _httpx

    spento = _httpx.AsyncClient(base_url="http://127.0.0.1:1/")
    mobile.mobile._client = spento
    try:
        data = mobile.get("/api/mobile/health").json()
        assert data["ok"] is False
        assert data["detail"]
    finally:
        mobile.mobile._client = None


def test_api_error_is_a_clear_502_when_upstream_is_down(mobile):
    import httpx as _httpx

    spento = _httpx.AsyncClient(base_url="http://127.0.0.1:1/")
    mobile.mobile._client = spento
    try:
        response = mobile.get("/api/sessions")
        assert response.status_code == 502
        assert "run.py" in response.json()["detail"]
    finally:
        mobile.mobile._client = None


# ---------------------------------------------------------------------------
# Test diretti dei pezzi del ponte: upstream_base, get_client, proxy_api
# ---------------------------------------------------------------------------


def test_upstream_base_default_e_override(monkeypatch):
    from server import mobile as mobile_mod

    # Default esplicito: il principale su 8123.
    monkeypatch.delenv("HARNESS_UPSTREAM", raising=False)
    assert mobile_mod.upstream_base() == "http://127.0.0.1:8123"

    # L'override da ambiente vince, e la barra finale viene tolta: il client
    # costruisce gli URL come "{base}/api/...", una doppia barra romperebbe
    # il percorso.
    monkeypatch.setenv("HARNESS_UPSTREAM", "http://192.168.1.10:9000/")
    assert mobile_mod.upstream_base() == "http://192.168.1.10:9000"


def test_get_client_is_a_singleton_with_infinite_read_timeout(monkeypatch):
    import asyncio

    from server import mobile as mobile_mod

    monkeypatch.delenv("HARNESS_UPSTREAM", raising=False)
    mobile_mod._client = None
    try:
        primo = mobile_mod.get_client()
        secondo = mobile_mod.get_client()
        # Un solo client per processo: due istanze aprirebbero due pool di
        # connessioni e il shutdown chiuderebbe solo l'ultima.
        assert secondo is primo
        assert str(primo.base_url) == "http://127.0.0.1:8123/"
        # Un turno fra un tool e l'altro resta minuti in silenzio: il read
        # timeout di httpx di default (5s) taglierebbe lo stream a meta'.
        assert primo.timeout.read is None
        assert primo.timeout.connect == 5.0
    finally:
        asyncio.run(mobile_mod.close_client())
    assert mobile_mod._client is None


def test_proxy_api_forwards_method_path_query_body_and_headers(monkeypatch):
    """proxy_api visto da vicino: cosa arriva davvero all'upstream."""
    from server import mobile as mobile_mod

    visto: dict[str, object] = {}

    async def upstream_spia(scope, receive, send):
        corpo = b""
        if scope["type"] == "http":
            while True:
                message = await receive()
                if message["type"] == "http.request":
                    corpo += message.get("body", b"")
                    if not message.get("more_body"):
                        break
                else:
                    break
        intestazioni = {
            k.decode().lower(): v.decode()
            for k, v in scope.get("headers", [])
        }
        visto.update(
            metodo=scope["method"],
            percorso=scope["path"],
            query=scope.get("query_string", b""),
            corpo=corpo,
            # L'Host del telefono NON deve attraversare: httpx lo sostituisce
            # con quello dell'upstream ("principale").
            host=intestazioni.get("host", ""),
        )
        payload = json.dumps({"echo": "ok", "metodo": scope["method"]}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-prova-upstream", b"s1"),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": payload, "more_body": False}
        )

    mobile_mod._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_spia),
        base_url="http://principale/",
    )
    monkeypatch.setenv(mobile_mod.TOKEN_ENV, CHIAVE)
    try:
        with TestClient(mobile_mod.app) as client:
            risposta = client.post(
                "/api/sessions/search",
                # La chiave del ponte viaggia come gli altri parametri: il
                # test qui sotto verifica che NON prosegua verso l'upstream.
                params={"limite": 7, "k": CHIAVE},
                json={"q": "piano"},
                headers={"X-Mia": "valore"},
            )
    finally:
        import asyncio

        asyncio.run(mobile_mod.close_client())

    # Cosa e' arrivato all'upstream, campo per campo.
    assert visto["metodo"] == "POST"
    assert visto["percorso"] == "/api/sessions/search"
    assert visto["query"] == b"limite=7"  # la chiave "k" si ferma al ponte
    assert json.loads(visto["corpo"]) == {"q": "piano"}  # type: ignore[arg-type]
    assert visto["host"] == "principale"

    # Cosa e' tornato al telefono: status, corpo e intestazioni utili passano;
    # le intestazioni hop-by-hop no.
    assert risposta.status_code == 201
    assert risposta.json() == {"echo": "ok", "metodo": "POST"}
    assert risposta.headers["x-prova-upstream"] == "s1"
    assert "transfer-encoding" not in risposta.headers


# ---------------------------------------------------------------------------
# La chiave del ponte
# ---------------------------------------------------------------------------


def test_senza_chiave_il_ponte_non_apre(mobile):
    """Il ponte ascolta su 0.0.0.0: dietro c'e' un agente che esegue comandi
    sul computer di casa. Senza chiave non deve rispondere niente -- nemmeno
    la pagina, nemmeno un file statico, nemmeno l'elenco delle chat."""
    for percorso in ("/", "/static/app.js", "/api/sessions"):
        risposta = mobile.get(percorso, headers={"X-Harness-Token": "sbagliata"})
        assert risposta.status_code == 401, percorso


def test_la_chiave_nell_indirizzo_lascia_un_cookie(mobile):
    """Un gesto solo: si apre il link con ?k=, e da li' in poi il telefono
    entra da solo -- le fetch della pagina non portano la query."""
    aperta = mobile.get(f"/?k={CHIAVE}", headers={"X-Harness-Token": "sbagliata"})
    assert aperta.status_code == 200
    assert mobile.cookies.get(mobile.mobile.COOKIE) == CHIAVE

    # Ora vale il cookie, anche con l'intestazione sbagliata.
    dopo = mobile.get("/api/sessions", headers={"X-Harness-Token": "sbagliata"})
    assert dopo.status_code == 200
