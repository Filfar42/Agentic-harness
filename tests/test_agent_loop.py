"""Test end-to-end del ciclo agentico contro un finto server Ollama.

Verifica il percorso completo: streaming NDJSON -> parsing del canale
thinking -> tool call nativa -> esecuzione del tool -> secondo giro ->
risposta finale. E' il test che tiene insieme rendering, orchestrazione e
transport: se si rompe uno dei tre, fallisce qui.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as agent_mod
from core.backend import OllamaBackend
from core.config import GenParams
from core.tools import TOOLS_SCHEMA, ToolContext

# Copione delle risposte del finto modello, un elemento per passo agentico.
SCRIPT: list[list[dict]] = [
    # Passo 1: pensa (a token spezzati) e chiama un tool.
    [
        {"message": {"content": "<thi"}},
        {"message": {"content": "nk>Devo prima "}},
        {"message": {"content": "guardare i file.</thi"}},
        {"message": {"content": "nk>"}},
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "list_files", "arguments": {"subfolder": "."}}}
                ],
            }
        },
    ],
    # Passo 2: risponde in chiaro e chiude il turno.
    [
        {"message": {"content": "Nel workspace "}},
        {"message": {"content": "c'e' un solo file: hello.py."}},
    ],
]


_DONE_CHUNK = {
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 100,
    "eval_count": 20,
    "eval_duration": 1_000_000_000,
    "total_duration": 1_500_000_000,
}


def _merge_script_step(chunks: list[dict]) -> dict:
    """Ricompone i chunk di un passo in un unico ``message`` non-streaming."""
    content = "".join((c.get("message") or {}).get("content") or "" for c in chunks)
    tool_calls: list[dict] = []
    for chunk in chunks:
        tool_calls.extend((chunk.get("message") or {}).get("tool_calls") or [])
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    # Ogni percorso richiesto, in ordine: serve ai test che verificano quanti
    # round-trip costa un'azione della UI (aprire una chat deve costarne zero).
    hits: list[str] = []
    step = 0
    # Versione dichiarata dal finto server: sopra la soglia l'harness usa lo
    # streaming, sotto ripiega sul non-streaming.
    version = "0.12.0"
    # Simula il comportamento di Ollama < 0.8.0: in streaming le tool call
    # semplicemente non compaiono.
    drop_tool_calls_when_streaming = False
    # Ritardo per chunk: serve ai test che devono agire *mentre* il turno gira.
    delay = 0.0

    def log_message(self, *args):  # silenzia il logging su stderr
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/api/tags":
            self._json({"models": [{"name": "fake:latest"}]})
        elif self.path == "/api/version":
            self._json({"version": type(self).version})
        else:
            self.send_error(404)

    def do_POST(self):
        type(self).hits.append(self.path)
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/show":
            self._json({"capabilities": ["completion", "tools"]})
            return
        if self.path != "/api/chat":
            self.send_error(404)
            return

        type(self).calls.append(payload)
        idx = min(type(self).step, len(SCRIPT) - 1)
        type(self).step += 1
        chunks = SCRIPT[idx]

        if not payload.get("stream", True):
            self._json({"model": "fake:latest", "message": _merge_script_step(chunks), **_DONE_CHUNK})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for grezzo in chunks:
            chunk = json.loads(json.dumps(grezzo))  # copia: non mutare lo script
            chunk.setdefault("done", False)
            if type(self).drop_tool_calls_when_streaming:
                (chunk.get("message") or {}).pop("tool_calls", None)
            self.wfile.write(json.dumps(chunk).encode() + b"\n")
            self.wfile.flush()
            if type(self).delay:
                time.sleep(type(self).delay)
        self.wfile.write(json.dumps({"message": {"content": ""}, **_DONE_CHUNK}).encode() + b"\n")
        self.wfile.flush()


@pytest.fixture()
def fake_ollama():
    _Handler.calls = []
    _Handler.hits = []          # ogni percorso richiesto, non solo /api/chat
    _Handler.step = 0
    _Handler.version = "0.12.0"
    _Handler.drop_tool_calls_when_streaming = False
    _Handler.delay = 0.0
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Handler
    server.shutdown()
    server.server_close()


def test_full_turn_thinking_toolcall_answer(fake_ollama, tmp_path):
    url, _handler = fake_ollama
    (tmp_path / "hello.py").write_text("print('ciao')\n", encoding="utf-8")

    backend = OllamaBackend(url, timeout_s=20)
    ok, _ = backend.ping()
    assert ok
    assert backend.supports_tools("fake:latest") is True

    ui_messages: list[dict] = [{"role": "user", "content": "cosa c'e' nel progetto?"}]
    ctx = ToolContext(workspace=str(tmp_path), timeout_s=10, sandbox="host")

    events = list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake:latest", num_ctx=8192),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ctx,
            ui_messages=ui_messages,
            system_prompt="SYS",
            env_header="ENV",
            max_steps=4,
        )
    )

    kinds = [type(e).__name__ for e in events]
    assert "ToolFinished" in kinds
    assert kinds[-1] == "TurnFinished"

    # 1) il pensiero e' stato ricomposto nonostante i tag spezzati fra chunk
    reasonings = [e.text for e in events if isinstance(e, agent_mod.ReasoningDelta)]
    assert reasonings[-1] == "Devo prima guardare i file."

    # 2) il pensiero non e' MAI finito nel canale della risposta
    contents = [e.text for e in events if isinstance(e, agent_mod.ContentDelta)]
    assert all("<think>" not in c and "Devo prima" not in c for c in contents)
    assert contents[-1] == "Nel workspace c'e' un solo file: hello.py."

    # 3) il tool e' stato eseguito davvero sul disco
    tool_events = [e for e in events if isinstance(e, agent_mod.ToolFinished)]
    assert len(tool_events) == 1
    assert tool_events[0].name == "list_files"
    assert tool_events[0].ok
    assert "hello.py" in tool_events[0].result

    # 4) il turno si chiude come completato
    final = events[-1]
    assert final.reason == "completed"
    assert final.usage["prompt_tokens"] == 200  # 100 per ciascuno dei 2 passi


def test_options_actually_reach_ollama(fake_ollama, tmp_path):
    """Regressione sul bug piu' grave: num_ctx/num_gpu venivano ignorati."""
    url, handler = fake_ollama
    backend = OllamaBackend(url, timeout_s=20)

    list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake:latest", num_ctx=32768, num_gpu=42, keep_alive="-1"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "ciao"}],
            system_prompt="SYS",
            env_header=None,
            max_steps=1,
        )
    )

    first = handler.calls[0]
    assert first["options"]["num_ctx"] == 32768
    assert first["options"]["num_gpu"] == 42
    assert first["keep_alive"] == "-1"
    assert first["tools"], "gli schemi dei tool devono essere inviati ad ogni chiamata"


def test_prompt_prefix_is_stable_between_steps(fake_ollama, tmp_path):
    """Il prefisso deve restare identico: e' la condizione per il KV cache."""
    url, handler = fake_ollama
    (tmp_path / "hello.py").write_text("x\n", encoding="utf-8")

    list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "cosa c'e'?"}],
            system_prompt="SYS",
            env_header="ENV",
            max_steps=4,
        )
    )

    assert len(handler.calls) == 2
    first, second = handler.calls[0]["messages"], handler.calls[1]["messages"]
    # il secondo passo estende il primo senza riscriverne il prefisso
    assert second[: len(first)] == first
    assert len(second) > len(first)


def test_think_is_not_sent_back_to_the_model(fake_ollama, tmp_path):
    url, handler = fake_ollama
    (tmp_path / "hello.py").write_text("x\n", encoding="utf-8")

    list(
        agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20),
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "cosa c'e'?"}],
            system_prompt="SYS",
            env_header="ENV",
            max_steps=4,
            strip_thinking=True,
        )
    )

    second_call = json.dumps(handler.calls[1])
    assert "Devo prima guardare i file" not in second_call
    assert "<think>" not in second_call


# ---------------------------------------------------------------------------
# Regressione: Ollama vecchio non emette tool call in streaming
# ---------------------------------------------------------------------------


def test_old_ollama_falls_back_to_non_streaming(fake_ollama, tmp_path):
    """Il sintomo "il modello ha smesso di usare i tool", riprodotto e chiuso.

    Il server dichiara 0.6.8 e, se interrogato in streaming, restituisce il
    testo ma nessuna tool_calls. L'harness deve accorgersene dalla versione e
    passare da solo al non-streaming, dove le tool call arrivano.
    """
    url, handler = fake_ollama
    handler.version = "0.6.8"
    handler.drop_tool_calls_when_streaming = True
    (tmp_path / "hello.py").write_text("x\n", encoding="utf-8")

    backend = OllamaBackend(url, timeout_s=20)
    assert backend.streams_tool_calls() is False

    events = list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "cosa c'e' nel progetto?"}],
            system_prompt="SYS",
            env_header="ENV",
            max_steps=4,
        )
    )

    executed = [e for e in events if isinstance(e, agent_mod.ToolFinished)]
    assert executed, "senza il fallback il tool non verrebbe mai eseguito"
    assert executed[0].name == "list_files"
    # le richieste con tool sono partite in non-streaming
    assert handler.calls[0]["stream"] is False


def test_recent_ollama_keeps_streaming(fake_ollama, tmp_path):
    url, handler = fake_ollama
    handler.version = "0.12.0"
    backend = OllamaBackend(url, timeout_s=20)
    assert backend.streams_tool_calls() is True

    list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "ciao"}],
            system_prompt="SYS",
            env_header=None,
            max_steps=1,
        )
    )
    assert handler.calls[0]["stream"] is True


def test_manual_override_forces_non_streaming(fake_ollama, tmp_path):
    url, handler = fake_ollama
    handler.version = "0.12.0"
    backend = OllamaBackend(url, timeout_s=20, stream_with_tools=False)
    assert backend.streams_tool_calls() is False

    list(
        agent_mod.run_turn(
            backend=backend,
            params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA,
            tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "ciao"}],
            system_prompt="SYS",
            env_header=None,
            max_steps=1,
        )
    )
    assert handler.calls[0]["stream"] is False
def test_auto_nudge_when_model_answers_in_words(fake_ollama, tmp_path):
    """L'utente chiede un'azione, il modello risponde a parole: l'harness insiste."""
    url, _handler = fake_ollama
    # il modello non chiama mai tool: solo testo
    global SCRIPT
    original = SCRIPT
    SCRIPT = [[{"message": {"content": "Potresti creare tu il file config.py."}}]]
    try:
        ui_messages = [{"role": "user", "content": "crea il file config.py nel progetto"}]
        list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=2,
            )
        )
    finally:
        SCRIPT = original

    hidden = [m for m in ui_messages if m.get("hidden")]
    assert hidden, "il sollecito automatico non e' stato iniettato"
    assert "chiamando il tool" in hidden[0]["content"]
    # il sollecito non deve comparire nella chat dell'utente
    assert hidden[0]["role"] == "user" and hidden[0]["hidden"] is True


def test_version_is_queried_once_per_instance(fake_ollama):
    """La GET /api/version non deve ripetersi ad ogni passo agentico."""
    url, handler = fake_ollama
    backend = OllamaBackend(url, timeout_s=20)

    assert backend.version() == "0.12.0"
    handler.version = "9.9.9"          # cambia il server sotto i piedi
    assert backend.version() == "0.12.0", "la versione va letta dalla cache"
    assert backend.version(refresh=True) == "9.9.9"


def test_leaked_tool_call_is_executed_and_kept_out_of_chat(fake_ollama, tmp_path):
    """Riproduzione fedele della sessione reale.

    Il modello emette la write_file come testo JSON (con codice pieno di
    graffe nel content). Attesi tre comportamenti:
      1. il file viene davvero creato nel workspace;
      2. il turno e' marcato come 'recovered';
      3. il JSON non compare nel testo mostrato in chat.
    """
    url, _handler = fake_ollama
    leak = json.dumps(
        {
            "name": "write_file",
            "arguments": {
                "filepath": "shopping_lists.py",
                "content": "import json\n\nshopping_lists = {}\n\ndef add_item(n):\n    shopping_lists[n] = []\n",
            },
        },
        ensure_ascii=False,
    )

    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        # passo 1: la fuga di JSON, spezzata in chunk come in streaming vero
        [{"message": {"content": leak[:80]}}, {"message": {"content": leak[80:]}}],
        # passo 2: il modello conclude a parole
        [{"message": {"content": "Ho creato shopping_lists.py."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "procedi a creare il file con i tuoi tool"}]
        events = list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=4,
            )
        )
    finally:
        SCRIPT = original

    # 1. il file esiste davvero
    written = tmp_path / "shopping_lists.py"
    assert written.exists(), "la chiamata recuperata non ha prodotto il file"
    assert "def add_item" in written.read_text(encoding="utf-8")

    # 2. il turno e' segnalato come recuperato
    turns = [e for e in events if isinstance(e, agent_mod.AssistantTurn)]
    assert any(t.recovered for t in turns)

    # 3. il JSON non finisce nel contenuto mostrato in chat
    assert all("write_file" not in (t.content or "") for t in turns)
    assert all('"arguments"' not in (t.content or "") for t in turns)

    finished = events[-1]
    assert finished.reason == "completed"
def test_agent_suspends_on_question_and_resumes_with_answer(fake_ollama, tmp_path):
    url, handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        # passo 1: l'agente chiede
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "ask_user_question",
                                "arguments": {
                                    "question": "Quale formato per la configurazione?",
                                    "options": ["TOML", "JSON", "YAML"],
                                },
                            }
                        }
                    ],
                }
            }
        ],
        # passo 2 (dopo la risposta): conclude
        [{"message": {"content": "Fatto: creato config.toml.\nPoi: aggiungo un test."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "crea la configurazione del progetto"}]
        kwargs = {
            "backend": OllamaBackend(url, timeout_s=20),
            "params": GenParams(model="fake:latest"),
            "tools_schema": TOOLS_SCHEMA,
            "tool_ctx": ToolContext(workspace=str(tmp_path), sandbox="host"),
            "system_prompt": "SYS",
            "env_header": None,
            "max_steps": 4,
        }
        first = list(agent_mod.run_turn(ui_messages=ui_messages, **kwargs))

        # il turno si e' fermato in attesa
        asks = [e for e in first if isinstance(e, agent_mod.AwaitingUserInput)]
        assert len(asks) == 1
        assert asks[0].question == "Quale formato per la configurazione?"
        assert [o["label"] for o in asks[0].options] == ["TOML", "JSON", "YAML"]
        assert first[-1].reason == "awaiting_user"

        # lo stato pendente vive dentro ui_messages, quindi e' salvabile su disco
        pending = agent_mod.pending_question(ui_messages)
        assert pending is not None and pending["role"] == "pending_question"

        # ...e non raggiunge mai il modello
        api = agent_mod.build_api_messages(ui_messages, system_prompt="S", env_header=None)
        assert all(m["role"] != "pending_question" for m in api)

        # la risposta dell'utente riprende il turno
        assert agent_mod.resume_with_answer(ui_messages, "TOML") is True
        assert agent_mod.pending_question(ui_messages) is None
        second = list(agent_mod.run_turn(ui_messages=ui_messages, **kwargs))
    finally:
        SCRIPT = original

    assert second[-1].reason == "completed"
    # il modello ha ricevuto la risposta come risultato del tool
    last_request = json.dumps(handler.calls[-1])
    assert "TOML" in last_request
    assert "ask_user_question" in last_request


def test_question_is_asked_last_so_other_tools_complete(fake_ollama, tmp_path):
    """Se il modello mescola una domanda con altri tool, gli altri girano prima."""
    url, _handler = fake_ollama
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "ask_user_question",
                                      "arguments": {"question": "Sovrascrivo a.py?"}}},
                        {"function": {"name": "list_files", "arguments": {"subfolder": "."}}},
                    ],
                }
            }
        ]
    ]
    try:
        ui_messages = [{"role": "user", "content": "guarda e decidi"}]
        events = list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=2,
            )
        )
    finally:
        SCRIPT = original

    executed = [e for e in events if isinstance(e, agent_mod.ToolFinished)]
    assert [e.name for e in executed] == ["list_files"]
    assert any(isinstance(e, agent_mod.AwaitingUserInput) for e in events)
    # nessun tool_call_id resta scoperto tranne quello della domanda
    assert ui_messages[-1]["role"] == "pending_question"


def test_silent_turn_gets_a_forced_summary(fake_ollama, tmp_path):
    """Il turno usa un tool e finisce muto: l'harness chiede il riepilogo."""
    url, _handler = fake_ollama
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [{"message": {"content": "",
                      "tool_calls": [{"function": {"name": "list_files",
                                                   "arguments": {"subfolder": "."}}}]}}],
        [{"message": {"content": ""}}],                     # silenzio
        [{"message": {"content": "Fatto: letto l'albero.\nPoi: scrivo i test."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "guarda il progetto"}]
        events = list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=5,
            )
        )
    finally:
        SCRIPT = original

    hidden = [m for m in ui_messages if m.get("hidden")]
    assert hidden and "Fatto:" in hidden[-1]["content"]
    turns = [e for e in events if isinstance(e, agent_mod.AssistantTurn)]
    assert "Poi: scrivo i test." in turns[-1].content
    assert events[-1].reason == "completed"


def test_summary_not_forced_when_no_tool_was_used(fake_ollama, tmp_path):
    """Una risposta puramente conversazionale non va sollecitata."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [[{"message": {"content": "TCP garantisce l'ordine, UDP no."}}]]
    try:
        ui_messages = [{"role": "user", "content": "differenza fra TCP e UDP?"}]
        events = list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=3,
            )
        )
    finally:
        SCRIPT = original

    assert not [m for m in ui_messages if m.get("hidden")]
    assert events[-1].reason == "completed"


def test_faked_tool_response_does_not_suppress_the_summary(fake_ollama, tmp_path):
    """Il turno chiudeva in silenzio perche' <tool_response> contava come risposta."""
    url, _handler = fake_ollama
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [{"message": {"content": "", "tool_calls": [
            {"function": {"name": "list_files", "arguments": {"subfolder": "."}}}]}}],
        # il modello scrive un involucro vuoto invece del riepilogo
        [{"message": {"content": "<tool_response> </tool_response>"}}],
        [{"message": {"content": "Fatto: letto l'albero.\nPoi: scrivo i test."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "guarda il progetto"}]
        events = list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS", env_header=None, max_steps=5,
            )
        )
    finally:
        SCRIPT = original

    turns = [e for e in events if isinstance(e, agent_mod.AssistantTurn)]
    assert all("<tool_response>" not in (t.content or "") for t in turns)
    assert "Poi: scrivo i test." in turns[-1].content
    assert [m for m in ui_messages if m.get("hidden")], "il riepilogo non e' stato sollecitato"


def test_leak_nudge_fires_once_and_after_the_tool_results(fake_ollama, tmp_path):
    """Con qwen ogni chiamata e' recuperata: nudgiare ogni volta e' spreco puro.

    E soprattutto il sollecito non deve inserirsi fra il messaggio assistant
    con le tool_calls e i relativi risultati: molti chat template li vogliono
    contigui, e romperli confonde il modello.
    """
    url, _handler = fake_ollama
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    leak = json.dumps({"name": "list_files", "arguments": {"subfolder": "."}})
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [{"message": {"content": leak}}],
        [{"message": {"content": leak}}],
        [{"message": {"content": "Fatto: guardato.\nPoi: nulla."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "elenca i file del progetto"}]
        list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS", env_header=None, max_steps=5,
            )
        )
    finally:
        SCRIPT = original

    leaks = [m for m in ui_messages if m.get("hidden") and "function calling" in m["content"]]
    assert len(leaks) == 1, f"il sollecito e' stato iniettato {len(leaks)} volte"

    # nessun messaggio user fra un assistant con tool_calls e i suoi risultati
    for i, msg in enumerate(ui_messages[:-1]):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assert ui_messages[i + 1].get("role") == "tool", (
                "un sollecito si e' infilato fra la tool call e il suo risultato"
            )


# ---------------------------------------------------------------------------
# Ciclo di self-correction: rosso -> leggi -> correggi -> riesegui -> verde
# ---------------------------------------------------------------------------


def _cmd(command):
    return {"message": {"content": "", "tool_calls": [
        {"function": {"name": "run_command", "arguments": {"command": command}}}]}}


def test_failed_command_is_marked_red_and_carries_instructions(fake_ollama, tmp_path):
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [[_cmd('python -c "raise SystemExit(3)"')], [{"message": {"content": "Fatto."}}]]
    try:
        events = list(agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=[{"role": "user", "content": "lancia i test"}],
            system_prompt="SYS", env_header=None, max_steps=2,
        ))
    finally:
        SCRIPT = original

    tool = next(e for e in events if isinstance(e, agent_mod.ToolFinished))
    assert tool.ok is False, "la tendina del tool deve risultare rossa"
    payload = json.loads(tool.result)
    assert payload["esito"] == "FALLITO"
    assert payload["returncode"] == 3
    assert "riesegui" in payload["next_step"]


# Comando che fallisce finche' il file "riparazione" non esiste: e' il modo
# realistico di simulare il ciclo, perche' il punto e' rieseguire ESATTAMENTE
# lo stesso comando dopo aver corretto.
CHECK = "python -c \"import os,sys; sys.exit(0 if os.path.exists('fix.txt') else 1)\""


def _write_fix():
    return {"message": {"content": "", "tool_calls": [
        {"function": {"name": "write_file",
                      "arguments": {"filepath": "fix.txt", "content": "ok"}}}]}}


def test_agent_is_pushed_to_repair_a_red_verification(fake_ollama, tmp_path):
    """Il modello si accontenta e chiude: l'harness lo rimanda a lavorare."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [_cmd(CHECK)],                                    # rosso
        [{"message": {"content": "Fatto: tutto a posto."}}],   # si arrende
        [_write_fix()],                                   # dopo la spinta: corregge
        [_cmd(CHECK)],                                    # e riesegue lo stesso comando
        [{"message": {"content": "Fatto: corretto.\nVerifica: verde.\nPoi: nulla."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "lancia i test e sistemali"}]
        events = list(agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui_messages, system_prompt="SYS", env_header=None, max_steps=8,
        ))
    finally:
        SCRIPT = original

    pushes = [m for m in ui_messages if m.get("hidden") and "riesegui" in m["content"].lower()]
    assert pushes, "l'harness non ha insistito sulla verifica rossa"

    runs = [e for e in events if isinstance(e, agent_mod.ToolFinished) and e.name == "run_command"]
    assert len(runs) == 2, "il comando non e' stato rieseguito dopo la correzione"
    assert runs[0].ok is False and runs[1].ok is True, "il ciclo non si e' chiuso in verde"
    assert events[-1].reason == "completed"


def test_repeated_identical_failure_switches_to_a_different_advice(fake_ollama, tmp_path):
    """Ripetere lo stesso comando rotto non lo aggiusta: va detto."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    broken = _cmd('python -c "raise SystemExit(1)"')
    SCRIPT = [[broken], [broken], [broken], [{"message": {"content": "Mi arrendo."}}]]
    try:
        ui_messages = [{"role": "user", "content": "lancia i test"}]
        list(agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui_messages, system_prompt="SYS", env_header=None, max_steps=6,
        ))
    finally:
        SCRIPT = original

    hidden = " ".join(m["content"] for m in ui_messages if m.get("hidden"))
    assert "Cambia approccio" in hidden
    assert "ask_user_question" in hidden


def test_a_green_rerun_clears_the_red_and_lets_the_turn_close(fake_ollama, tmp_path):
    """Se il modello ripara da solo, l'harness non deve intromettersi."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [_cmd(CHECK)],                                    # rosso
        [_write_fix()],                                   # ripara di sua iniziativa
        [_cmd(CHECK)],                                    # riesegue: verde
        [{"message": {"content": "Fatto: sistemato.\nVerifica: verde.\nPoi: nulla."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "verifica"}]
        events = list(agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui_messages, system_prompt="SYS", env_header=None, max_steps=8,
        ))
    finally:
        SCRIPT = original

    assert not [m for m in ui_messages if m.get("hidden")], "spinta inutile: aveva gia' riparato"
    assert events[-1].reason == "completed"


def test_a_different_green_command_does_not_clear_the_red(fake_ollama, tmp_path):
    """Scelta di progetto: vale solo la rierecuzione dello STESSO comando.

    Lanciare un sotto-test che passa non dimostra che la suite completa passi,
    quindi la verifica rossa resta aperta e l'harness continua a insistere.
    """
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [_cmd(CHECK)],                                    # la suite fallisce
        [_cmd("python -c \"print('un pezzo passa')\"")],  # verde, ma su altro
        [{"message": {"content": "Fatto: a posto."}}],
        [{"message": {"content": "Fatto: non passa ancora."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "lancia i test"}]
        list(agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui_messages, system_prompt="SYS", env_header=None, max_steps=8,
        ))
    finally:
        SCRIPT = original

    hidden = " ".join(m["content"] for m in ui_messages if m.get("hidden"))
    assert CHECK.split()[0] in hidden or "fallito" in hidden.lower()


def test_giving_up_on_red_still_forces_an_honest_summary(fake_ollama, tmp_path):
    """Se resta rosso, il riepilogo deve dirlo invece di arrotondare."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    broken = _cmd('python -c "raise SystemExit(1)"')
    # il modello resta muto dopo le spinte: scatta il riepilogo, versione onesta
    SCRIPT = [[broken], [{"message": {"content": ""}}], [{"message": {"content": ""}}],
              [{"message": {"content": ""}}], [{"message": {"content": ""}}]]
    try:
        ui_messages = [{"role": "user", "content": "lancia i test"}]
        list(agent_mod.run_turn(
            backend=OllamaBackend(url, timeout_s=20), params=GenParams(model="fake:latest"),
            tools_schema=TOOLS_SCHEMA, tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
            ui_messages=ui_messages, system_prompt="SYS", env_header=None, max_steps=8,
        ))
    finally:
        SCRIPT = original

    hidden = " ".join(m["content"] for m in ui_messages if m.get("hidden"))
    assert "NON passa" in hidden, "il riepilogo poteva far credere che fosse tutto a posto"


def test_verification_tracker_forgets_a_command_once_it_goes_green():
    from core.agent import VerificationTracker

    tracker = VerificationTracker()
    red = json.dumps({"command": "pytest -q", "returncode": 1, "esito": "FALLITO"})
    green = json.dumps({"command": "pytest -q", "returncode": 0, "esito": "ok"})

    tracker.record("run_command", red)
    tracker.record("run_command", red)
    assert tracker.unresolved == ("pytest -q", 2, 1)

    tracker.record("run_command", green)
    assert tracker.unresolved is None

    # gli altri tool non entrano nel conteggio
    tracker.record("read_file", json.dumps({"filepath": "x.py"}))
    assert tracker.unresolved is None


def test_the_red_command_reaches_the_tools(fake_ollama, tmp_path):
    """Il guard sui test vive nei tool, ma solo il ciclo sa cosa e' rosso."""
    _url, handler = fake_ollama
    handler.step = 0
    ctx = ToolContext(workspace=str(tmp_path), timeout_s=10, sandbox="host")
    assert ctx.red_command is None

    # un run_command rosso deve lasciare traccia nel contesto dei tool
    agent_mod.dispatch(ctx, "run_command", {"command": "python -c \"import sys; sys.exit(1)\""})
    verification = agent_mod.VerificationTracker()
    verification.record(
        "run_command",
        agent_mod.dispatch(ctx, "run_command", {"command": "python -c \"import sys; sys.exit(1)\""}),
    )
    assert verification.unresolved is not None
    ctx.red_command = verification.unresolved[0]
    assert "sys.exit(1)" in ctx.red_command


def test_una_richiesta_analitica_blocca_la_scrittura_dal_ciclo(fake_ollama, tmp_path):
    """Il caso reale: 'Analizza... suggerisci come ottimizzarla' e il modello
    si mette a scrivere sul disco. Il divieto lo imposta il ciclo, non i tool:
    solo lui sa qual era la richiesta."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [{"message": {"content": "", "tool_calls": [{"function": {
            "name": "write_file",
            "arguments": {"filepath": "veloce.py", "content": "def veloce():\n    pass\n"},
        }}]}}],
        [{"message": {"content": "Hai ragione, era solo un'analisi."}}],
    ]
    try:
        ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
        list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ctx,
                ui_messages=[{"role": "user", "content":
                              "Analizza la complessità di media_mobile_pesata e "
                              "suggerisci come ottimizzarla."}],
                system_prompt="SYS",
                env_header=None,
                max_steps=3,
            )
        )
    finally:
        SCRIPT = original

    assert ctx.readonly_request is True
    assert not (tmp_path / "veloce.py").exists(), "il file non doveva essere scritto"


def test_una_funzione_nuova_senza_test_non_chiude_il_turno(fake_ollama, tmp_path):
    """La verifica vacua: pytest verde su codice che non e' quello nuovo."""
    url, _handler = fake_ollama
    global SCRIPT
    original = SCRIPT
    SCRIPT = [
        [{"message": {"content": "", "tool_calls": [{"function": {
            "name": "write_file",
            "arguments": {"filepath": "mod.py", "content": "def nuovissima():\n    return 1\n"},
        }}]}}],
        [{"message": {"content": "Fatto: aggiunta nuovissima(). Verifica: verde."}}],
        [{"message": {"content": "Hai ragione, mancano i test."}}],
    ]
    try:
        ui_messages = [{"role": "user", "content": "aggiungi una funzione nuovissima a mod.py"}]
        list(
            agent_mod.run_turn(
                backend=OllamaBackend(url, timeout_s=20),
                params=GenParams(model="fake:latest"),
                tools_schema=TOOLS_SCHEMA,
                tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
                ui_messages=ui_messages,
                system_prompt="SYS",
                env_header=None,
                max_steps=4,
            )
        )
    finally:
        SCRIPT = original

    solleciti = [m for m in ui_messages if m.get("hidden")]
    assert any("nuovissima" in m["content"] for m in solleciti), \
        "il turno si e' chiuso senza segnalare la funzione non coperta"
