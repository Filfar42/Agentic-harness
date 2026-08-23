"""Test HTTP del backend FastAPI contro il finto server Ollama.

Verificano il percorso che l'utente vede davvero: avvio del turno, streaming
degli eventi, sospensione su ``ask_user_question`` e -- soprattutto -- che il
turno sia legato alla **conversazione** e non alla connessione del browser.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_agent_loop as fake  # noqa: E402  (riusa il finto Ollama)
from core import config as config_mod  # noqa: E402


@pytest.fixture()
def client(fake_ollama, tmp_path, monkeypatch):
    """Server FastAPI isolato: sessioni e memorie in una cartella temporanea."""
    url, _ = fake_ollama
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config_mod, "MEMORY_FILE", tmp_path / "memories.json")

    from core import memory as memory_mod
    from core import session as session_mod

    monkeypatch.setattr(session_mod, "DATA_DIR", tmp_path / "sessions")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", tmp_path / "memories.json")
    # Le preferenze vere della macchina le isola conftest.py, per tutti.

    from server import main as server_main
    from server.runner import RunnerRegistry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.py").write_text("print('ciao')\n", encoding="utf-8")

    server_main.RUNNERS = RunnerRegistry()
    server_main.STATE = server_main.AppState()
    server_main.STATE.settings.update(
        {
            "api_base": url,
            "transport": "ollama",
            "model_name": "fake:latest",
            "workspace_dir": str(workspace),
            "timeout_seconds": 20,
            # Niente avvio automatico di Docker nei test: e' un effetto
            # collaterale sulla macchina che li esegue, non un comportamento
            # sotto esame qui.
            "docker_autostart": False,
        }
    )
    with TestClient(server_main.app) as test_client:
        test_client.workspace = workspace
        test_client.server = server_main
        yield test_client

    # Nessun turno deve sopravvivere al test. I worker sono thread demoni e
    # ``fake.SCRIPT`` e' una variabile globale: un turno rimasto in volo
    # consuma le risposte finte preparate dal test successivo, che fallisce
    # per un motivo che non ha niente a che vedere con quello che prova.
    # E' il difetto che faceva cadere test_answer_flows_to_the_pending_question
    # una volta ogni tanto, e mai da solo.
    scadenza = time.monotonic() + 10
    while server_main.RUNNERS.running_ids() and time.monotonic() < scadenza:
        time.sleep(0.02)


fake_ollama = fake.fake_ollama  # riesporta la fixture


def read_sse(response) -> list[dict]:
    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def run_and_collect(client, session_id: str, prompt: str) -> list[dict]:
    """Avvia un turno e ne raccoglie gli eventi fino alla fine."""
    started = client.post("/api/chat", json={"session_id": session_id, "prompt": prompt})
    assert started.status_code == 200, started.text
    return read_sse(client.get(f"/api/stream/{session_id}"))


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def current_session(client) -> str:
    return client.get("/api/bootstrap").json()["session"]["session_id"]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


def test_bootstrap_returns_everything_the_ui_needs(client):
    data = client.get("/api/bootstrap").json()
    assert data["app"]["name"]
    assert data["settings"]["model_name"] == "fake:latest"
    assert data["backend"]["online"] is True
    assert data["backend"]["streams_tools"] is True
    session = data["session"]
    assert session["messages"] == []
    assert session["running"] is False
    assert session["pending"] is None
    assert "context_window" in session["stats"]


def test_index_and_static_assets_are_served(client):
    assert "<title>Local Agent Harness</title>" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_chat_runs_in_background_and_touches_the_disk(client):
    session_id = current_session(client)
    events = run_and_collect(client, session_id, "cosa c'e' nel progetto?")
    kinds = [e["type"] for e in events]

    assert kinds[0] == "start"
    assert "reasoning" in kinds          # il pensiero arriva come canale separato
    assert "tool_end" in kinds
    assert "done" in kinds
    assert kinds[-1] == "state"          # i pannelli si aggiornano a fine turno

    tool = next(e for e in events if e["type"] == "tool_end")
    assert tool["name"] == "list_files"
    assert "hello.py" in tool["result"]

    contents = [e["text"] for e in events if e["type"] == "content"]
    assert all("<think>" not in c for c in contents)

    sessions = client.get("/api/sessions").json()["sessions"]
    assert sessions and sessions[0]["n_messages"] > 0


def test_stream_of_an_idle_session_returns_immediately(client):
    events = read_sse(client.get("/api/stream/inesistente"))
    assert [e["type"] for e in events] == ["idle"]


# ---------------------------------------------------------------------------
# Il turno vive nella conversazione, non nella connessione
# ---------------------------------------------------------------------------


def test_reattaching_replays_the_whole_turn(client):
    """Tornare sulla chat deve rimostrare pensiero e tool gia' eseguiti."""
    session_id = current_session(client)
    first = run_and_collect(client, session_id, "cosa c'e' nel progetto?")

    # una seconda connessione allo stesso turno rivede tutto dall'inizio
    again = read_sse(client.get(f"/api/stream/{session_id}"))
    assert [e["type"] for e in again] == [e["type"] for e in first]
    assert any(e["type"] == "tool_end" for e in again)
    assert any(e["type"] == "reasoning" for e in again)


def test_turn_survives_switching_conversation(client):
    """Il bug: cambiando chat l'agente smetteva di lavorare.

    Qui si avvia un turno su A, si passa a B *mentre gira*, e si verifica che
    il lavoro prosegua fino in fondo e che tornando su A sia tutto li'.
    """
    handler = fake._Handler
    handler.delay = 0.25                       # rende il turno abbastanza lento
    try:
        session_a = current_session(client)
        assert client.post(
            "/api/chat", json={"session_id": session_a, "prompt": "guarda il progetto"}
        ).status_code == 200

        # si cambia conversazione col turno ancora in corso
        session_b = client.post("/api/sessions").json()["session_id"]
        assert session_b != session_a
        opened_b = client.post(f"/api/sessions/{session_b}/open").json()
        assert opened_b["running"] is False

        # A risulta in esecuzione nell'elenco: e' il feedback della sidebar
        listed = {s["id"]: s for s in client.get("/api/sessions").json()["sessions"]}
        assert listed[session_a]["running"] is True

        # tornando su A si riceve lo snapshot pre-turno e il flag "in corso"
        opened_a = client.post(f"/api/sessions/{session_a}/open").json()
        assert opened_a["running"] is True

        # ...e riattaccandosi si rivede tutto, fino al termine
        events = read_sse(client.get(f"/api/stream/{session_a}"))
    finally:
        handler.delay = 0.0

    assert any(e["type"] == "tool_end" for e in events), "il turno e' morto col cambio chat"
    assert events[-1]["type"] == "state"

    # il lavoro e' finito nella conversazione giusta, non in quella aperta dopo
    messages_a = client.post(f"/api/sessions/{session_a}/open").json()["messages"]
    assert any(m.get("role") == "tool" for m in messages_a)
    messages_b = client.post(f"/api/sessions/{session_b}/open").json()["messages"]
    assert messages_b == []


def test_second_turn_on_a_busy_conversation_is_refused(client):
    handler = fake._Handler
    handler.delay = 0.25
    try:
        session_id = current_session(client)
        client.post("/api/chat", json={"session_id": session_id, "prompt": "vai"})
        second = client.post("/api/chat", json={"session_id": session_id, "prompt": "ancora"})
        assert second.status_code == 409
        read_sse(client.get(f"/api/stream/{session_id}"))   # attende la fine
    finally:
        handler.delay = 0.0


def test_deleting_a_running_conversation_is_refused(client):
    handler = fake._Handler
    handler.delay = 0.25
    try:
        session_id = current_session(client)
        client.post("/api/chat", json={"session_id": session_id, "prompt": "vai"})
        assert client.delete(f"/api/sessions/{session_id}").status_code == 409
        read_sse(client.get(f"/api/stream/{session_id}"))
    finally:
        handler.delay = 0.0


def test_changing_chat_stops_previews_and_frees_the_ports(client, monkeypatch):
    """Le anteprime di altre chat non devono tenere le porte al cambio conversazione.

    ``ensure_container`` pubblica l'intero intervallo con un solo -p e Docker
    tiene i proxy sulle porte dell'host per tutta la vita del container: se
    cambiando chat il container resta in piedi, l'app della vecchia chat continua
    a occupare le porte e il bind successivo fallisce. Qui si finge che lo stop
    funzioni (niente docker reale nel test) e si verifica che venga chiamato e
    che i pannelli di anteprima delle altre conversazioni vengano sgomberati,
    altrimenti sarebbero link morti finche' non c'e' un altro serve.
    """
    stops = []

    def _stop_background(*_args, **_kwargs):  # noqa: ARG002 - firma finta
        return False      # niente processi in background: la catena arriva a stop()

    def _stop(workspace):
        stops.append(str(workspace))
        return True

    monkeypatch.setattr(client.server.sandbox_mod, "stop_background", _stop_background)
    monkeypatch.setattr(client.server.sandbox_mod, "stop", _stop)

    state = client.server.STATE
    a = current_session(client)
    state.store_preview(a, {"kind": "serve", "path": "app.py", "title": "App"})
    assert state.preview(a) is not None      # su A c'è una anteprima viva

    b = client.post("/api/sessions").json()["session_id"]   # il cambio innesca lo stop
    assert b != a
    client.post(f"/api/sessions/{b}/open")

    assert stops, "cambiando chat le porte della sandbox non vengono liberate"
    assert state.preview(a) is None          # A perde l'anteprima: link morto evitato


def test_changing_chat_without_previews_does_not_probe_the_sandbox(client, monkeypatch):
    """Senza app avviate, il cambio di conversazione non deve guardare nel container.

    Il vecchio percorso chiamava ``stop_background`` a ogni cambio: senza
    nulla da fermare ricreava la sandbox per intero solo per scoprirlo (secondi
    in piu' al click, e il turno dopo pagava di nuovo la creazione). Ora, senza
    marchi host-side, resta solo la spazzata leggera del container residuo.
    """
    bg_calls = []
    stops = []

    def _stop_background(*_args, **_kwargs):  # noqa: ARG002 - firma finta
        bg_calls.append(1)
        return False

    def _stop(workspace):
        stops.append(str(workspace))
        return True

    monkeypatch.setattr(client.server.sandbox_mod, "stop_background", _stop_background)
    monkeypatch.setattr(client.server.sandbox_mod, "stop", _stop)
    monkeypatch.setattr(client.server.sandbox_mod, "background_live", lambda _ws: False)

    a = current_session(client)
    b = client.post("/api/sessions").json()["session_id"]   # il cambio innesca la pulizia
    assert b != a

    assert bg_calls == [], "niente da fermare eppure si e' andati a guardare nel container"
    assert stops, "la spazzata del container residuo deve restare"


def test_chat_switch_with_live_mark_stops_the_app_per_slot(client, monkeypatch):
    """Con un'app avviata dall'harness il cambio chat la ferma e spazza il container.

    Comportamento confermato con l'utente: a ogni cambio di conversazione le app
    si fermano e le porte si liberano (vedi test qui sopra). Il punto nuovo e'
    che lo stato host-side guida il percorso: marchio presente -> si prova
    prima l'kill per slot senza ricreare la sandbox a vuoto, e se quello non
    basta passa comunque ``stop``.
    """
    calls_bg = []
    stops = []

    def _stop_background(*_args, **_kwargs):  # noqa: ARG002 - firma finta
        calls_bg.append(1)
        return False

    def _stop(workspace):
        stops.append(str(workspace))
        return True

    monkeypatch.setattr(client.server.sandbox_mod, "stop_background", _stop_background)
    monkeypatch.setattr(client.server.sandbox_mod, "stop", _stop)
    monkeypatch.setattr(client.server.sandbox_mod, "background_live", lambda _ws: True)

    a = current_session(client)
    b = client.post("/api/sessions").json()["session_id"]   # il cambio innesca la pulizia
    assert b != a

    assert calls_bg == [1], "con un'app viva si deve prendere il percorso per slot"
    assert stops, "se l'kill dello slot non basta, la spazzata del container resta"


# ---------------------------------------------------------------------------
# Domande all'agente
# ---------------------------------------------------------------------------


def test_question_suspends_then_answer_resumes(client):
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
                                    "question": "Quale formato preferisci?",
                                    "options": ["TOML", "JSON"],
                                },
                            }
                        }
                    ],
                }
            }
        ],
        [{"message": {"content": "Fatto: creato config.toml.\nPoi: aggiungo un test."}}],
    ]
    try:
        session_id = current_session(client)
        first = run_and_collect(client, session_id, "crea la configurazione")
        question = next(e for e in first if e["type"] == "question")
        assert question["question"] == "Quale formato preferisci?"
        assert [o["label"] for o in question["options"]] == ["TOML", "JSON"]
        assert next(e for e in first if e["type"] == "done")["reason"] == "awaiting_user"

        # la domanda sopravvive a un reload della pagina
        pending = client.post(f"/api/sessions/{session_id}/open").json()["pending"]
        assert pending["question"] == "Quale formato preferisci?"

        # un nuovo prompt viene rifiutato finche' non si risponde
        assert client.post(
            "/api/chat", json={"session_id": session_id, "prompt": "altro"}
        ).status_code == 409

        assert client.post(
            "/api/answer", json={"session_id": session_id, "answer": "TOML"}
        ).status_code == 200
        second = read_sse(client.get(f"/api/stream/{session_id}"))
        assert next(e for e in second if e["type"] == "done")["reason"] == "completed"
        assert any("Poi:" in e.get("text", "") for e in second if e["type"] == "content")
    finally:
        fake.SCRIPT = original

    assert client.post(f"/api/sessions/{session_id}/open").json()["pending"] is None


def test_answering_without_a_question_is_rejected(client):
    session_id = current_session(client)
    response = client.post("/api/answer", json={"session_id": session_id, "answer": "x"})
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Persistenza, impostazioni, workspace
# ---------------------------------------------------------------------------


def test_switching_session_keeps_both_intact(client):
    first_id = current_session(client)
    run_and_collect(client, first_id, "prima conversazione")

    second_id = client.post("/api/sessions").json()["session_id"]
    assert second_id != first_id
    run_and_collect(client, second_id, "seconda conversazione")

    back = client.post(f"/api/sessions/{first_id}/open").json()
    assert any(m.get("content") == "prima conversazione" for m in back["messages"])
    forward = client.post(f"/api/sessions/{second_id}/open").json()
    assert any(m.get("content") == "seconda conversazione" for m in forward["messages"])


def test_settings_roundtrip(client):
    data = client.post("/api/settings", json={"values": {"num_ctx": 32768}}).json()
    assert data["settings"]["num_ctx"] == 32768
    assert client.get("/api/bootstrap").json()["settings"]["num_ctx"] == 32768


def test_memory_endpoints(client):
    added = client.post("/api/memories", json={"text": "I test: pytest -q"}).json()
    assert added["ok"] is True
    memory_id = added["memories"][0]["id"]
    assert client.delete(f"/api/memories/{memory_id}").json()["memories"] == []


def test_workspace_change_is_validated(client, tmp_path):
    assert client.post(
        "/api/workspace", json={"path": str(tmp_path / "assente")}
    ).status_code == 400
    good = tmp_path / "altra"
    good.mkdir()
    chosen = client.post("/api/workspace", json={"path": str(good)}).json()
    assert chosen["workspace_dir"] == str(good.resolve())


def test_pick_workspace_opens_the_system_dialog(client, tmp_path, monkeypatch):
    """Il selettore e' quello del sistema operativo, non uno disegnato da noi."""
    from server import main as server_main
    from server import nativedialog

    chosen = tmp_path / "scelta"
    chosen.mkdir()
    calls = []

    def fake_dialog(_initial=None):
        calls.append(_initial)
        return str(chosen)

    monkeypatch.setattr(server_main, "pick_folder", fake_dialog)
    data = client.post("/api/workspace/pick").json()

    assert data["cancelled"] is False
    assert data["workspace_dir"] == str(chosen.resolve())
    # il dialogo si apre gia' posizionato sul workspace corrente
    assert calls == [str(client.workspace)]
    assert nativedialog.pick_folder is not fake_dialog   # nessuna patch residua


def test_pick_workspace_handles_cancel(client, monkeypatch):
    from server import main as server_main

    before = server_main.STATE.settings["workspace_dir"]
    monkeypatch.setattr(server_main, "pick_folder", lambda _initial=None: None)
    assert client.post("/api/workspace/pick").json() == {"cancelled": True}
    assert server_main.STATE.settings["workspace_dir"] == before


def test_pick_workspace_reports_a_headless_machine(client, monkeypatch):
    """Senza ambiente grafico l'errore deve dire cosa fare, non solo fallire."""
    from server import main as server_main
    from server.nativedialog import DialogUnavailable

    def explode(_initial=None):
        raise DialogUnavailable("nessun display")

    monkeypatch.setattr(server_main, "pick_folder", explode)
    response = client.post("/api/workspace/pick")
    assert response.status_code == 501
    assert "Impostazioni" in response.json()["detail"]


def test_native_dialog_degrades_cleanly_without_a_display():
    """Nel container di test non c'e' grafica: deve sollevare, non piantarsi."""
    import platform

    from server.nativedialog import DialogUnavailable, pick_folder

    if platform.system() != "Linux":
        pytest.skip("verifica specifica per l'ambiente di test headless")
    with pytest.raises(DialogUnavailable):
        pick_folder("/tmp")

def test_known_files_survive_across_turns(client):
    """Letto una volta, l'agente non deve rileggere lo stesso file ogni turno."""
    from server import main as server_main

    session_id = current_session(client)
    (client.workspace / "esistente.py").write_text("vecchio\n", encoding="utf-8")

    ctx = server_main.STATE.tool_ctx(session_id)
    from core.tools import dispatch

    blocked = json.loads(
        dispatch(ctx, "write_file", {"filepath": "esistente.py", "content": "nuovo"})
    )
    assert "error" in blocked

    dispatch(ctx, "read_file", {"filepath": "esistente.py"})
    server_main.STATE.save(session_id)

    # turno successivo: nuovo ToolContext, stessa conversazione
    later = server_main.STATE.tool_ctx(session_id)
    assert "esistente.py" in later.known_files
    written = json.loads(
        dispatch(later, "write_file", {"filepath": "esistente.py", "content": "nuovo"})
    )
    assert written["status"] == "ok"

    # ...ma in una conversazione nuova la conoscenza riparte da zero
    fresh = server_main.STATE.tool_ctx(server_main.STATE.new_session())
    assert "esistente.py" not in fresh.known_files


def test_thinking_is_enabled_only_for_reasoning_models(client, monkeypatch):  # noqa: ARG001
    """Con 'auto' il canale nativo si accende solo se il modello ce l'ha."""
    from server import main as server_main

    state = server_main.STATE
    state.forget_backend()
    state.settings["native_think"] = "auto"

    # il finto Ollama dichiara solo ["completion", "tools"]
    assert state.thinking_enabled() is False

    class Reasoning:
        def supports_thinking(self, _model):
            return True

    monkeypatch.setattr(state, "backend", lambda: Reasoning())
    # Il rilevamento e' in cache: cambiare backend sotto banco non basta,
    # serve invalidarla - ed e' esattamente cio' che fanno il salvataggio
    # delle impostazioni e la sonda manuale.
    assert state.thinking_enabled() is False
    state.forget_backend()
    assert state.thinking_enabled() is True

    # l'impostazione manuale ha comunque la precedenza
    state.settings["native_think"] = "no"
    assert state.thinking_enabled() is False


def test_prompt_drops_the_think_clause_for_reasoning_models(client):  # noqa: ARG001
    """Con il canale nativo, chiedere anche <think> nel prompt e' rumore."""
    from server import main as server_main

    state = server_main.STATE
    state.settings["native_think"] = "no"
    assert "<think>" in state.system_prompt()

    state.settings["native_think"] = "si"
    assert "<think>" not in state.system_prompt()


# --- prestazioni: sfogliare le chat non deve costare I/O --------------------
# Aprire una conversazione passava per session_stats -> system_prompt ->
# thinking_enabled -> backend(), e ogni anello faceva rete: due round-trip
# verso Ollama per click. Se Ollama stava generando, quelle richieste
# restavano in coda dietro alla generazione e la UI sembrava bloccata.


def test_opening_a_chat_never_talks_to_ollama(client, fake_ollama):
    _, handler = fake_ollama
    first = client.post("/api/sessions").json()["session_id"]
    client.post(f"/api/sessions/{first}/open")     # scalda le cache

    handler.hits.clear()
    second = client.post("/api/sessions").json()["session_id"]
    client.post(f"/api/sessions/{second}/open")
    client.post(f"/api/sessions/{first}/open")
    assert handler.hits == []


def test_the_backend_is_reused_between_requests(client):
    state = client.server.STATE
    assert state.backend() is state.backend()


def test_changing_the_endpoint_rebuilds_the_backend(client):
    state = client.server.STATE
    before = state.backend()
    client.post("/api/settings", json={"values": {"api_base": "http://127.0.0.1:1"}})
    assert state.backend() is not before


def test_a_turn_still_sees_the_workspace_as_it_is_now(client):
    """La cache dell'env header non deve nascondere un file appena creato."""
    state = client.server.STATE
    state.env_header()                                    # popola la cache
    (client.workspace / "appena_creato.py").write_text("x = 1\n", encoding="utf-8")
    assert "appena_creato.py" not in (state.env_header() or "")
    assert "appena_creato.py" in (state.env_header(fresh=True) or "")


def test_the_session_index_is_not_reparsed_when_nothing_changed(client, monkeypatch):
    from core import session as session_mod

    client.post("/api/sessions")
    session_mod.list_sessions()          # popola la cache su mtime+size

    opened = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        if str(path).endswith(".json"):
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    session_mod.list_sessions()
    assert opened == []


# --- stop del turno ---------------------------------------------------------


def test_stop_ends_the_turn_and_keeps_the_partial_work(client):
    """Il tasto stop chiude il turno senza lasciare la cronologia rotta."""
    handler = fake._Handler
    handler.delay = 0.3                      # turno lento abbastanza da fermarlo
    try:
        session_id = current_session(client)
        client.post("/api/chat", json={"session_id": session_id, "prompt": "lavora"})
        assert wait_until(lambda: client.server.RUNNERS.is_running(session_id))
        time.sleep(0.4)                      # lo si lascia cominciare davvero
        assert client.post(f"/api/stop/{session_id}").json()["ok"] is True
        assert wait_until(lambda: not client.server.RUNNERS.is_running(session_id))
        events = read_sse(client.get(f"/api/stream/{session_id}"))
    finally:
        handler.delay = 0.0

    done = next(e for e in events if e["type"] == "done")
    assert done["reason"] == "stopped"

    # nessuna tool_call resta senza il suo risultato: la chat e' riutilizzabile
    messages = client.post(f"/api/sessions/{session_id}/open").json()["messages"]
    called = [c["id"] for m in messages for c in (m.get("tool_calls") or [])]
    answered = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
    assert sorted(called) == sorted(x for x in answered if x)

    # e la conversazione resta utilizzabile dopo lo stop
    assert client.post(
        "/api/chat", json={"session_id": session_id, "prompt": "riprendi"}
    ).status_code == 200


def test_stopping_an_idle_conversation_is_harmless(client):
    assert client.post(f"/api/stop/{current_session(client)}").json()["ok"] is False


# --- allegati ---------------------------------------------------------------


def test_an_attachment_lands_in_the_workspace_and_in_the_context(client):
    session_id = current_session(client)
    response = client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("relazione.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    entry = response.json()["added"][0]
    assert entry["path"] == "allegati/relazione.csv"
    assert (client.workspace / "allegati" / "relazione.csv").read_bytes() == b"a,b\n1,2\n"

    # l'agente lo vede elencato, ma il contenuto non entra nel contesto
    header = client.server.STATE.context_header(session_id)
    assert "allegati/relazione.csv" in header
    assert "a,b" not in header

    assert response.json()["stats"]["attachments"][0]["name"] == "relazione.csv"


def test_attachment_names_cannot_escape_the_folder(client):
    session_id = current_session(client)
    client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("../../evasione.txt", b"x", "text/plain")},
    )
    assert (client.workspace / "allegati" / "evasione.txt").is_file()
    assert not (client.workspace.parent / "evasione.txt").exists()


def test_two_files_with_the_same_name_do_not_overwrite_each_other(client):
    session_id = current_session(client)
    for body in (b"primo", b"secondo"):
        client.post(
            f"/api/attachments/{session_id}",
            files={"files": ("note.txt", body, "text/plain")},
        )
    names = [a["name"] for a in client.server.STATE.attachments(session_id)]
    assert names == ["note.txt", "note-1.txt"]
    assert (client.workspace / "allegati" / "note.txt").read_bytes() == b"primo"
    assert (client.workspace / "allegati" / "note-1.txt").read_bytes() == b"secondo"


def test_removing_an_attachment_deletes_the_file_too(client):
    session_id = current_session(client)
    client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("tmp.txt", b"x", "text/plain")},
    )
    data = client.delete(f"/api/attachments/{session_id}?name=tmp.txt").json()
    assert data["attachments"] == []
    assert not (client.workspace / "allegati" / "tmp.txt").exists()


def test_attachments_survive_a_reload_of_the_conversation(client):
    session_id = current_session(client)
    client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("dati.json", b"{}", "application/json")},
    )
    client.server.STATE.drop(session_id)      # simula un riavvio del server
    reopened = client.post(f"/api/sessions/{session_id}/open").json()
    assert reopened["stats"]["attachments"][0]["name"] == "dati.json"


# --- adattamento al modello -------------------------------------------------
# I default dell'harness erano tarati su qwen2.5-coder. Su un modello che
# ragiona sono sbagliati in modo silenzioso: il prompt esteso occupa contesto
# con stampelle inutili, e num_predict 2048 tronca il turno perche' conta
# anche i token di pensiero.


def test_a_thinking_model_gets_the_lean_prompt(client):
    from core.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN

    state = client.server.STATE
    state.forget_backend()
    state.settings["native_think"] = "auto"

    # il finto Ollama dichiara solo ["completion", "tools"]: prompt esteso
    assert SYSTEM_PROMPT.strip()[:60] in state.system_prompt()

    state.forget_backend()
    state._think = ((state.settings["api_base"], state.settings["model_name"]), True)
    lean = state.system_prompt()
    assert SYSTEM_PROMPT_LEAN.strip()[:60] in lean
    assert "# Quale tool chiamare" not in lean       # la tabella di trigger sparisce


def test_a_prompt_rewritten_by_the_user_always_wins(client):
    state = client.server.STATE
    state.settings["system_prompt"] = "Sono io che comando."
    state._think = ((state.settings["api_base"], state.settings["model_name"]), True)
    assert state.system_prompt().startswith("Sono io che comando.")


def test_the_recommended_profile_follows_the_selected_model(client):
    client.post("/api/settings", json={"values": {"model_name": "qwen3.5:9b"}})
    data = client.get("/api/profile").json()
    assert "Qwen3.5" in data["profile"]
    assert data["values"]["top_k"] == 20
    assert data["values"]["max_tokens"] > 2048


def test_applying_the_profile_changes_the_settings(client):
    client.post("/api/settings", json={"values": {"model_name": "qwen3.5:9b", "top_k": 40}})
    applied = client.post("/api/profile/apply").json()
    assert applied["settings"]["top_k"] == 20
    assert client.server.STATE.gen_params().ollama_options()["top_k"] == 20


def test_the_think_level_reaches_the_request(client):
    client.post("/api/settings", json={"values": {"native_think": "high"}})
    params = client.server.STATE.gen_params()
    assert params.think_payload == "high"


# --- vision -----------------------------------------------------------------


def test_images_are_not_sent_to_a_model_without_vision(client):
    """Il finto Ollama dichiara solo completion e tools."""
    session_id = current_session(client)
    client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("schermata.png", b"\x89PNG\r\n\x1a\nfinta", "image/png")},
    )
    images, skipped = client.server.STATE.turn_images(session_id)
    assert images == [] and skipped == []


def test_images_reach_the_model_when_it_has_vision(client):
    session_id = current_session(client)
    client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("schermata.png", b"\x89PNG\r\n\x1a\nfinta", "image/png")},
    )
    state = client.server.STATE
    state._vision = ((state.settings["api_base"], state.settings["model_name"]), True)

    images, skipped = state.turn_images(session_id)
    assert len(images) == 1 and not skipped

    # e finiscono sull'ultimo messaggio visibile dell'utente, non sui solleciti
    from core.agent import build_api_messages

    api = build_api_messages(
        [
            {"role": "user", "content": "guarda questa"},
            {"role": "user", "content": "SOLLECITO", "hidden": True},
        ],
        system_prompt="SYS", env_header=None, images=images,
    )
    carriers = [m for m in api if m.get("images")]
    assert len(carriers) == 1
    assert carriers[0]["content"] == "guarda questa"


def test_a_non_image_attachment_never_becomes_an_image(client):
    session_id = current_session(client)
    client.post(
        f"/api/attachments/{session_id}",
        files={"files": ("dati.csv", b"a,b\n1,2\n", "text/csv")},
    )
    state = client.server.STATE
    state._vision = ((state.settings["api_base"], state.settings["model_name"]), True)
    images, _ = state.turn_images(session_id)
    assert images == []


# ---------------------------------------------------------------------------
# Bus eventi globale (/api/events): la sincronizzazione fra le due interfacce
# ---------------------------------------------------------------------------


def _frame(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame[6:])


def test_event_bus_delivers_to_every_subscriber_until_unsubscribed(client):
    from server import main as server_main

    bus = server_main.EVENTS
    primo, secondo = bus.subscribe(), bus.subscribe()
    try:
        bus.publish("turn", session_id="s1", running=True)
        atteso = {"type": "turn", "session_id": "s1", "running": True}
        assert _frame(primo.get(timeout=2)) == atteso
        assert _frame(secondo.get(timeout=2)) == atteso

        bus.unsubscribe(primo)
        bus.publish("sessions", reason="created", session_id="s2")
        assert primo.empty() is True  # disiscritto: niente piu' copie per lui
        assert _frame(secondo.get(timeout=2)) == {
            "type": "sessions",
            "reason": "created",
            "session_id": "s2",
        }
    finally:
        bus.unsubscribe(primo)
        bus.unsubscribe(secondo)


def test_event_bus_queue_cap_drops_instead_of_growing_forever(client):
    from server import main as server_main

    bus = server_main.EVENTS
    sub = bus.subscribe()
    try:
        for n in range(bus._QUEUE_MAX + 100):
            bus.publish("tick", n=n)
        # Oltre il tetto gli eventi si buttano: un abbonato morto non deve
        # poter gonfiare la memoria del server.
        assert sub.qsize() <= bus._QUEUE_MAX
    finally:
        bus.unsubscribe(sub)


def test_creating_a_chat_from_one_side_notifies_the_other(client):
    from server import main as server_main

    coda = server_main.EVENTS.subscribe()
    try:
        creata = client.post("/api/sessions").json()["session_id"]
        assert _frame(coda.get(timeout=2)) == {
            "type": "sessions",
            "reason": "created",
            "session_id": creata,
        }
    finally:
        server_main.EVENTS.unsubscribe(coda)


def test_deleting_a_session_notifies_the_bus(client):
    from server import main as server_main

    session_id = current_session(client)
    coda = server_main.EVENTS.subscribe()
    try:
        client.delete(f"/api/sessions/{session_id}")
        assert _frame(coda.get(timeout=2)) == {
            "type": "sessions",
            "reason": "deleted",
            "session_id": session_id,
        }
    finally:
        server_main.EVENTS.unsubscribe(coda)


def test_turn_lifecycle_and_question_hit_the_global_bus(client):
    """Il telefono vede domanda e fine turno senza agganciarsi allo stream."""
    import threading

    from server import main as server_main

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
                                "arguments": {"question": "Formato?", "options": ["A"]},
                            }
                        }
                    ],
                }
            }
        ],
        [{"message": {"content": "Fatto."}}],
    ]
    coda = server_main.EVENTS.subscribe()
    try:
        session_id = current_session(client)
        client.post("/api/chat", json={"session_id": session_id, "prompt": "chiedimi"})

        visti: list[dict] = []
        finito = threading.Event()

        def bevi():
            while not finito.is_set():
                try:
                    visti.append(_frame(coda.get(timeout=0.5)))
                except Exception:  # queue.Empty -- semplicemente niente ancora
                    continue

        lettore = threading.Thread(target=bevi, daemon=True)
        lettore.start()
        time.sleep(0.4)

        client.post("/api/answer", json={"session_id": session_id, "answer": "A"})
        # La risposta chiude il turno: l'ultimo evento deve essere turn=False.
        scadenza = time.time() + 15
        while time.time() < scadenza:
            if any(
                e.get("type") == "turn" and e.get("running") is False for e in visti
            ):
                break
            time.sleep(0.2)
        finito.set()
        lettore.join(timeout=2)

        tipi = [e["type"] for e in visti]
        assert "question" in tipi                       # la domanda e' arrivata
        domanda = next(e for e in visti if e["type"] == "question")
        assert domanda["session_id"] == session_id      # della chat giusta
        assert any(e.get("reason") == "answer" for e in visti)  # la risposta e' passata dal bus
        assert tipi.count("turn") >= 2                  # avvio e chiusura turno
        ultimo = next(e for e in reversed(visti) if e["type"] == "turn")
        assert ultimo["running"] is False               # il turno e' finito
    finally:
        server_main.EVENTS.unsubscribe(coda)
        fake.SCRIPT = original


def test_events_stream_yields_hello_then_published_frames_and_unsubscribes(client):
    """Il generatore del bus: saluto, eventi pubblicati, pulizia alla chiusura.

    Non passa da HTTP perche' un SSE infinito con TestClient non ha una
    chiusura netta; il cablaggio della rotta e' verificato nel test sotto.
    """
    import inspect

    from server import main as server_main

    bus = server_main.EVENTS
    prima = len(bus._subscribers)
    generatore = bus.stream()
    try:
        assert _frame(next(generatore))["type"] == "hello"
        bus.publish("sessions", reason="created", session_id="sx")
        frame = next(generatore)
        assert _frame(frame) == {
            "type": "sessions",
            "reason": "created",
            "session_id": "sx",
        }
    finally:
        generatore.close()
    # chiusura il generatore disiscrive davvero: nessun abbonato fantasma
    assert len(bus._subscribers) == prima

    # il keepalive deve esistere, senno' proxy e browser chiudono la connessione
    sorgente = inspect.getsource(bus.stream)
    assert ": keepalive" in sorgente


def test_events_route_is_wired_to_the_bus(app_client=None):
    """La rotta /api/events esiste ed e' collegata al flusso del bus."""
    from fastapi.routing import APIRoute

    from server import main as server_main

    rotte = [r for r in server_main.app.routes if isinstance(r, APIRoute)]
    eventi = [r for r in rotte if r.path == "/api/events"]
    assert eventi, "la rotta /api/events manca dall'app"
    assert "GET" in eventi[0].methods


def test_rileggere_una_conversazione_non_ferma_le_anteprime(client, monkeypatch):
    """Aprire e rileggere sono due verbi diversi, e ora due rotte diverse.

    Il bus globale annuncia la fine di ogni turno e la pagina si riallinea.
    Farlo con la POST ``/open`` -- come nel fork -- significava passare da
    ``ferma_anteprime()`` ad ogni fine turno: container smontato e anteprima
    che sparisce sotto gli occhi di chi la stava guardando. Dal telefono era
    peggio: il polling la chiamava ogni cinque secondi.
    """
    session_id = client.post("/api/sessions", json={}).json()["session_id"]

    fermate: list[int] = []
    monkeypatch.setattr(
        client.server.STATE, "ferma_anteprime", lambda: bool(fermate.append(1))
    )

    letta = client.get(f"/api/sessions/{session_id}")
    assert letta.status_code == 200
    assert letta.json()["session_id"] == session_id
    assert fermate == [], "la rilettura non deve toccare la sandbox"

    client.post(f"/api/sessions/{session_id}/open", json={})
    assert fermate == [1], "aprire, invece, libera le porte della chat precedente"


def test_la_ricerca_non_viene_scambiata_per_un_id_di_conversazione(client):
    """``/api/sessions/search`` e ``/api/sessions/{id}`` hanno la stessa forma:
    l'ordine di dichiarazione e' l'unica cosa che le tiene distinte."""
    risposta = client.get("/api/sessions/search", params={"q": "niente"})
    assert risposta.status_code == 200
    assert "sessions" in risposta.json()


# ---------------------------------------------------------------------------
# Spegnimento: le risposte che non finiscono mai devono poter finire
# ---------------------------------------------------------------------------


def test_il_bus_globale_si_chiude_quando_il_processo_si_ferma():
    """La ragione per cui Ctrl+C aveva smesso di funzionare.

    ``/api/events`` e' una risposta HTTP che per mestiere non termina: ogni
    scheda aperta sulla UI ne tiene una. Uvicorn, ricevuto il segnale, smette
    di accettare connessioni e poi **aspetta che le risposte in corso
    finiscano** -- e questa non finiva. Dall'esterno: Ctrl+C ignorato e la
    finestra del terminale da chiudere a mano.
    """
    from server import runner as runner_mod
    from server.main import EventBus

    runner_mod.dimentica_spegnimento()
    bus = EventBus()
    flusso = bus.stream()
    assert "hello" in next(flusso), "il saluto apre lo stream"

    finito = threading.Event()

    def consuma():
        for _ in flusso:
            pass
        finito.set()

    lettore = threading.Thread(target=consuma, daemon=True)
    lettore.start()
    # Prima dello spegnimento lo stream e' vivo: nessuno lo chiude.
    assert not finito.wait(0.3)

    runner_mod.annuncia_spegnimento()
    try:
        # E adesso finisce **subito**, non fra quindici secondi di keepalive.
        assert finito.wait(2.0), "lo stream non si e' chiuso allo spegnimento"
    finally:
        runner_mod.dimentica_spegnimento()


def test_anche_lo_stream_di_un_turno_si_stacca_allo_spegnimento():
    """Stessa regola per ``/api/stream/{id}``: un turno lungo terrebbe aperta
    la sua risposta, e uvicorn aspetterebbe lui invece del Ctrl+C. Il turno
    non viene fermato -- vive nel suo thread -- si chiude la connessione."""
    from server import runner as runner_mod

    runner_mod.dimentica_spegnimento()
    registro = runner_mod.RunnerRegistry()
    partito = threading.Event()
    libera = threading.Event()

    def lavoro(runner):
        runner.emit('data: {"type": "start"}\n\n')
        partito.set()
        libera.wait(5)

    registro.start("sessione-di-prova", [], lavoro)
    assert partito.wait(2)

    runner = registro.get("sessione-di-prova")
    flusso = runner.stream()
    assert next(flusso)                       # l'arretrato c'e'

    finito = threading.Event()

    def consuma():
        for _ in flusso:
            pass
        finito.set()

    threading.Thread(target=consuma, daemon=True).start()
    assert not finito.wait(0.3)

    runner_mod.annuncia_spegnimento()
    try:
        assert finito.wait(2.0), "la risposta del turno non si e' chiusa"
        assert not runner.finished.is_set(), "il turno non va fermato, solo scollegato"
    finally:
        libera.set()
        runner_mod.dimentica_spegnimento()
