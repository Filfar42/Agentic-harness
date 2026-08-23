"""Test HTTP del backend FastAPI contro il finto server Ollama.

Verificano il percorso che l'utente vede davvero: avvio del turno, streaming
degli eventi, sospensione su ``ask_user_question`` e -- soprattutto -- che il
turno sia legato alla **conversazione** e non alla connessione del browser.
"""

from __future__ import annotations

import json
import sys
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
