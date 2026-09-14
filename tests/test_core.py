"""Test di regressione sui bug corretti in questa revisione.

    python -m pytest tests -q
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent import (
    build_api_messages,
    looks_like_unexecuted_action,
    parse_text_tool_call,
)
from core.textutils import (
    ThinkStreamParser,
    estimate_messages_tokens,
    smart_truncate,
    split_think,
    strip_think,
)
from core.tools import (
    ToolContext,
    WorkspaceError,
    dispatch,
    resolve_path,
)


# ---------------------------------------------------------------------------
# Bug 2.1 -- parsing del pensiero durante lo streaming
# ---------------------------------------------------------------------------


def _stream(parser: ThinkStreamParser, chunks: list[str]) -> None:
    for chunk in chunks:
        parser.feed(chunk)
    parser.finish()


def test_think_tag_split_across_chunks():
    """Il caso che rompeva la UI: il tag arriva spezzato fra due chunk."""
    parser = ThinkStreamParser()
    _stream(parser, ["<thi", "nk>penso", " ancora</thi", "nk>Ecco la risposta."])
    assert parser.reasoning == "penso ancora"
    assert parser.answer == "Ecco la risposta."


def test_think_tag_one_char_at_a_time():
    text = "<think>ragiono</think>risposta"
    parser = ThinkStreamParser()
    _stream(parser, list(text))
    assert parser.reasoning == "ragiono"
    assert parser.answer == "risposta"


def test_multiple_think_blocks_are_concatenated():
    """Il vecchio regex catturava solo il primo blocco e perdeva il resto."""
    parser = ThinkStreamParser()
    _stream(parser, ["<think>uno</think>A", "<think>due</think>B"])
    assert parser.reasoning == "unodue"
    assert parser.answer == "AB"


def test_unclosed_think_block_is_not_lost():
    """Stream troncato da max_tokens: il pensiero deve restare visibile."""
    parser = ThinkStreamParser()
    _stream(parser, ["<think>ragionamento interrotto a meta"])
    assert parser.reasoning == "ragionamento interrotto a meta"
    assert parser.answer == ""


def test_no_think_tag_is_pure_answer():
    parser = ThinkStreamParser()
    _stream(parser, ["Solo ", "testo ", "normale."])
    assert parser.reasoning == ""
    assert parser.answer == "Solo testo normale."
    assert not parser.saw_think_tag


def test_open_angle_bracket_in_code_is_not_swallowed():
    parser = ThinkStreamParser()
    _stream(parser, ["if a <", " b and c ", "< d: pass"])
    assert parser.answer == "if a < b and c < d: pass"


def test_native_reasoning_channel():
    parser = ThinkStreamParser()
    parser.feed_reasoning("pensiero nativo ")
    parser.feed_reasoning("di Ollama")
    parser.feed("risposta")
    parser.finish()
    assert parser.reasoning == "pensiero nativo di Ollama"
    assert parser.answer == "risposta"


def test_strip_think_variants():
    assert strip_think("<think>x</think>ciao") == "ciao"
    assert strip_think("<thinking>x</thinking>ciao") == "ciao"
    assert strip_think("<think>mai chiuso") == ""
    assert strip_think("nessun tag") == "nessun tag"


def test_split_think_roundtrip():
    reasoning, answer = split_think("<think>piano</think>\nfatto")
    assert reasoning == "piano"
    assert answer == "fatto"


# ---------------------------------------------------------------------------
# Fase 3 -- troncamento e contesto
# ---------------------------------------------------------------------------


def test_smart_truncate_keeps_head_and_tail():
    text = "\n".join(f"riga {i}" for i in range(2000))
    out = smart_truncate(text, 1000, label="stdout")
    assert "riga 0" in out          # la testa sopravvive
    assert "riga 1999" in out       # la coda pure: qui vive il traceback
    assert "omessi" in out
    assert len(out) < len(text)


def test_smart_truncate_noop_when_short():
    assert smart_truncate("breve", 1000) == "breve"


def test_build_api_messages_strips_think():
    ui_messages = [
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "<think>lungo ragionamento</think>Fatto."},
    ]
    api = build_api_messages(
        ui_messages, system_prompt="SYS", env_header=None, strip_thinking=True
    )
    joined = json.dumps(api)
    assert "lungo ragionamento" not in joined
    assert "Fatto." in joined


def test_build_api_messages_compacts_old_tool_results():
    big = json.dumps({"status": "ok", "content": "X" * 20_000})
    ui_messages: list[dict] = [{"role": "user", "content": "vai"}]
    for i in range(6):
        ui_messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"c{i}", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        ui_messages.append(
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": big}
        )

    compacted = build_api_messages(
        ui_messages, system_prompt="SYS", env_header=None, compact_old_tools=True
    )
    verbose = build_api_messages(
        ui_messages, system_prompt="SYS", env_header=None, compact_old_tools=False
    )
    assert estimate_messages_tokens(compacted) < estimate_messages_tokens(verbose)

    tool_msgs = [m for m in compacted if m["role"] == "tool"]
    assert len(tool_msgs) == 6
    # I 3 piu' recenti restano integrali (troncati solo al cap globale)...
    for msg in tool_msgs[-3:]:
        assert "_compacted" not in msg["content"]
        assert len(msg["content"]) > 3_000
    # ...i 3 piu' vecchi sono ridotti a un sommario.
    for msg in tool_msgs[:3]:
        assert '"_compacted": true' in msg["content"]
        assert len(msg["content"]) < 1_000


def test_env_header_is_deterministic_for_caching():
    """Il prefisso del prompt deve restare byte-identico fra due passi."""
    from core.prompts import build_env_header

    a = build_env_header(".")
    b = build_env_header(".")
    assert a == b, "un header non deterministico invalida il KV cache ad ogni passo"


# ---------------------------------------------------------------------------
# Bug 2.3 -- fallback parser sicuro
# ---------------------------------------------------------------------------


def test_fallback_parses_valid_fenced_call():
    text = '```json\n{"name": "run_command", "arguments": {"command": "pytest -q"}}\n```'
    call = parse_text_tool_call(text)
    assert call is not None
    assert call["name"] == "run_command"
    assert json.loads(call["arguments"])["command"] == "pytest -q"


def test_fallback_rejects_unknown_tool():
    assert parse_text_tool_call('{"name": "delete_everything", "arguments": {}}') is None


def test_fallback_rejects_missing_required_args():
    """Il vecchio parser inventava filepath='output.txt' e creava file spuri."""
    assert parse_text_tool_call('{"name": "write_file", "arguments": {}}') is None


def test_fallback_ignores_prose_containing_the_word_name():
    prose = 'La variabile si chiama "name": deve essere una stringa non vuota.'
    assert parse_text_tool_call(prose) is None


def test_fallback_handles_nested_function_form():
    text = '{"function": {"name": "list_files", "arguments": {"subfolder": "src"}}}'
    call = parse_text_tool_call(text)
    assert call is not None and call["name"] == "list_files"


def test_unexecuted_action_heuristic():
    assert looks_like_unexecuted_action("Ora scrivo il file di configurazione per te.")
    assert not looks_like_unexecuted_action("Ho creato config.py e i test passano.")


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def test_resolve_path_blocks_traversal(tmp_path):
    with pytest.raises(WorkspaceError):
        resolve_path(tmp_path, "../../etc/passwd")


def test_resolve_path_blocks_sibling_prefix(tmp_path):
    """Il bug di startswith: '/work' non deve autorizzare '/work-altrui'."""
    base = tmp_path / "work"
    base.mkdir()
    sibling = tmp_path / "work-altrui"
    sibling.mkdir()
    with pytest.raises(WorkspaceError):
        resolve_path(base, str(sibling))


def test_resolve_path_accepts_relative(tmp_path):
    assert resolve_path(tmp_path, "src/main.py") == (tmp_path / "src" / "main.py").resolve()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path):
    return ToolContext(workspace=str(tmp_path), timeout_s=15, sandbox="host")


def test_write_read_edit_cycle(ctx):
    written = json.loads(dispatch(ctx, "write_file", {"filepath": "a/b.py", "content": "x = 1\n"}))
    assert written["status"] == "ok"

    read = json.loads(dispatch(ctx, "read_file", {"filepath": "a/b.py"}))
    assert read["content"] == "x = 1\n"

    edited = json.loads(
        dispatch(ctx, "edit_file", {"filepath": "a/b.py", "old_string": "x = 1", "new_string": "x = 2"})
    )
    assert edited["status"] == "ok"
    assert "x = 2" in json.loads(dispatch(ctx, "read_file", {"filepath": "a/b.py"}))["content"]
    assert "a/b.py" in ctx.touched_files


def test_edit_file_refuses_ambiguous_match(ctx):
    dispatch(ctx, "write_file", {"filepath": "d.py", "content": "a=1\na=1\n"})
    result = json.loads(dispatch(ctx, "edit_file", {"filepath": "d.py", "old_string": "a=1", "new_string": "a=2"}))
    assert "error" in result and "2 volte" in result["error"]


def test_missing_file_returns_actionable_hint(ctx):
    result = json.loads(dispatch(ctx, "read_file", {"filepath": "assente.py"}))
    assert "error" in result and "list_files" in result["hint"]


def test_unknown_args_are_rejected_before_execution(ctx):
    dispatch(ctx, "write_file", {"filepath": "e.py", "content": "1"})
    result = json.loads(
        dispatch(ctx, "read_file", {"filepath": "e.py", "encoding": "utf-16", "foo": 1})
    )
    assert result["error_code"] == "invalid_arguments"
    assert {d["path"] for d in result["details"]} == {"$.encoding", "$.foo"}
    assert "content" not in result


def test_dangerous_command_is_blocked(ctx):
    result = json.loads(dispatch(ctx, "run_command", {"command": "rm -rf /"}))
    assert "error" in result and "guard-rail" in result["error"]


def test_run_command_captures_exit_code(ctx):
    result = json.loads(dispatch(ctx, "run_command", {"command": "python -c \"import sys; sys.exit(3)\""}))
    assert result["returncode"] == 3


def test_search_files_finds_definition(ctx):
    dispatch(ctx, "write_file", {"filepath": "m.py", "content": "def target():\n    pass\n"})
    result = json.loads(dispatch(ctx, "search_files", {"pattern": r"def target", "glob": "*.py"}))
    assert result["match_count"] == 1
    assert "m.py:1" in result["matches"][0]


def test_unknown_tool_lists_alternatives(ctx):
    result = json.loads(dispatch(ctx, "inesistente", {}))
    assert "error" in result and "list_files" in result["hint"]


def test_manage_memory_roundtrip(ctx):
    assert json.loads(dispatch(ctx, "manage_memory", {"action": "add", "content": "usa uv"}))["status"] == "ok"
    assert "usa uv" in json.loads(dispatch(ctx, "manage_memory", {"action": "list"}))["memories"][0]
    assert json.loads(dispatch(ctx, "manage_memory", {"action": "remove", "content": "usa uv"}))["status"] == "ok"


def test_list_files_ignores_noise(ctx):
    dispatch(ctx, "write_file", {"filepath": "node_modules/pkg/index.js", "content": "//"})
    dispatch(ctx, "write_file", {"filepath": "src/app.py", "content": "#"})
    tree = json.loads(dispatch(ctx, "list_files", {}))["tree"]
    assert "src/" in tree
    assert "node_modules" not in tree


# ---------------------------------------------------------------------------
# Revisione 2.1 — nudge automatico e formato messaggi Ollama
# ---------------------------------------------------------------------------


def test_user_expects_tool_use_detects_workspace_requests():
    from core.agent import user_expects_tool_use

    assert user_expects_tool_use("crea un file config.py con i default")
    assert user_expects_tool_use("elenca i file del progetto")
    assert user_expects_tool_use("dove sta definita la funzione resolve_path?")
    assert user_expects_tool_use("lancia i test")
    # conversazione pura: nessun nudge
    assert not user_expects_tool_use("ciao, come stai?")
    assert not user_expects_tool_use("spiegami la differenza fra TCP e UDP")


def test_una_domanda_di_chiarimento_non_e_un_azione_rimandata():
    """Osservato in sessione: qwen si fermava a chiedere -- la cosa giusta -- e
    il TOOL_NUDGE lo rimproverava con 'esegui adesso l'azione', spingendolo a
    inventarsi la specifica invece di aspettarla."""
    from core.agent import looks_like_clarifying_question

    assert looks_like_clarifying_question(
        "Prima di procedere ho bisogno di sapere cosa intendi per valore anomalo."
    )
    assert looks_like_clarifying_question(
        "La finestra deve produrre risultati parziali all'inizio? "
        "E i pesi vanno normalizzati sulla loro somma?"
    )
    assert looks_like_clarifying_question("Vuoi che usi la deviazione campionaria o quella di popolazione?")
    # Non sono domande di chiarimento: sono azioni annunciate e non fatte.
    assert not looks_like_clarifying_question("Ora scrivo il file di configurazione per te.")
    assert not looks_like_clarifying_question("Ho creato config.py e i test passano.")


def test_last_user_request_skips_hidden_nudges():
    from core.agent import last_user_request

    msgs = [
        {"role": "user", "content": "crea main.py"},
        {"role": "assistant", "content": "certo"},
        {"role": "user", "content": "SOLLECITO INTERNO", "hidden": True},
    ]
    assert last_user_request(msgs) == "crea main.py"


def test_ollama_message_format_matches_spec():
    from core.backend import to_ollama_messages

    converted = to_ollama_messages(
        [
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "vai"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"filepath": "x.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"},
        ]
    )

    # i system multipli sono fusi in uno solo, in testa
    assert converted[0] == {"role": "system", "content": "A\n\nB"}
    assert sum(1 for m in converted if m["role"] == "system") == 1

    call = converted[2]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["index"] == 0
    # arguments come oggetto, non come stringa JSON
    assert call["function"]["arguments"] == {"filepath": "x.py"}

    tool_msg = converted[3]
    assert tool_msg == {"role": "tool", "tool_name": "read_file", "content": "ok"}


def test_version_parsing_and_streaming_threshold():
    from core.backend import MIN_VERSION_STREAMING_TOOLS, parse_version

    assert parse_version("0.12.3") == (0, 12, 3)
    assert parse_version("0.8.0") >= MIN_VERSION_STREAMING_TOOLS
    assert parse_version("0.6.8") < MIN_VERSION_STREAMING_TOOLS
    assert parse_version("0.11.4-rc1") == (0, 11, 4)
    assert parse_version("") == (0,)


def test_think_clause_added_only_when_needed():
    from core.prompts import build_system_prompt

    base = "Regole di base."
    assert "<think>" in build_system_prompt(base, native_think=False)
    assert "<think>" not in build_system_prompt(base, native_think=True)
    # se il prompt personalizzato parla gia' di <think>, non si duplica
    custom = "Usa <think> come vuoi."
    assert build_system_prompt(custom, native_think=False) == custom


# ---------------------------------------------------------------------------
# Regressione reale: tool call stampata come testo, con codice nel content
# ---------------------------------------------------------------------------

# Payload osservato in una sessione vera: il modello "intende" chiamare
# write_file ma emette il JSON nel canale testuale. Il content contiene codice
# Python con graffe ({} , dict letterali), che e' esattamente cio' che faceva
# fallire il vecchio parser a regex.
_REAL_LEAK = json.dumps(
    {
        "name": "write_file",
        "arguments": {
            "filepath": "shopping_lists.py",
            "content": (
                "import json\n\nshopping_lists = {}\n\n"
                "def add_item(list_name, item, quantity, bought):\n"
                "    if list_name not in shopping_lists:\n"
                "        shopping_lists[list_name] = []\n"
                "    shopping_lists[list_name].append({\n"
                "        'item': item,\n        'quantity': quantity,\n"
                "        'bought': bought\n    })\n\n"
                "if __name__ == '__main__':\n    load_shopping_lists()\n"
            ),
        },
    },
    ensure_ascii=False,
)


def test_recovers_write_file_with_braces_in_content():
    """Il caso che rompeva tutto: graffe dentro la stringa content."""
    from core.agent import parse_text_tool_calls

    calls, leftover = parse_text_tool_calls(_REAL_LEAK)
    assert len(calls) == 1
    assert calls[0]["name"] == "write_file"
    args = json.loads(calls[0]["arguments"])
    assert args["filepath"] == "shopping_lists.py"
    assert "shopping_lists = {}" in args["content"]
    assert leftover == "", "il JSON deve sparire dal testo mostrato in chat"


def test_recovered_call_actually_writes_the_file(tmp_path):
    """Non basta interpretarla: il file deve comparire sul disco."""
    from core.agent import parse_text_tool_calls
    from core.tools import ToolContext, dispatch

    calls, _ = parse_text_tool_calls(_REAL_LEAK)
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    result = json.loads(dispatch(ctx, calls[0]["name"], json.loads(calls[0]["arguments"])))

    assert result["status"] == "ok"
    written = tmp_path / "shopping_lists.py"
    assert written.exists()
    assert "def add_item" in written.read_text(encoding="utf-8")


def test_trailing_garbage_after_json_is_tolerated():
    """L'output reale aveva una graffa di troppo in coda."""
    from core.agent import parse_text_tool_calls

    calls, _ = parse_text_tool_calls(_REAL_LEAK + "}")
    assert len(calls) == 1 and calls[0]["name"] == "write_file"


def test_prose_around_the_json_is_preserved():
    from core.agent import parse_text_tool_calls

    text = f"Ecco il file richiesto.\n\n{_REAL_LEAK}\n\nFammi sapere se va bene."
    calls, leftover = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert "Ecco il file richiesto." in leftover
    assert "Fammi sapere" in leftover
    assert "write_file" not in leftover


def test_multiple_leaked_calls_are_all_recovered():
    from core.agent import parse_text_tool_calls

    a = json.dumps({"name": "list_files", "arguments": {"subfolder": "."}})
    b = json.dumps({"name": "run_command", "arguments": {"command": "pytest -q"}})
    calls, leftover = parse_text_tool_calls(f"{a}\n{b}")
    assert [c["name"] for c in calls] == ["list_files", "run_command"]
    assert leftover == ""


def test_fenced_json_with_nested_braces():
    from core.agent import parse_text_tool_calls

    calls, _ = parse_text_tool_calls(f"```json\n{_REAL_LEAK}\n```")
    assert len(calls) == 1 and calls[0]["name"] == "write_file"


def test_json_in_prose_is_not_mistaken_for_a_call():
    from core.agent import parse_text_tool_calls

    text = 'Il formato di config e\' {"name": "mio-progetto", "version": "1.0"}.'
    calls, leftover = parse_text_tool_calls(text)
    assert calls == []
    assert leftover == text


def test_looks_like_raw_tool_json_guard():
    from core.agent import looks_like_raw_tool_json

    assert looks_like_raw_tool_json(_REAL_LEAK)
    assert not looks_like_raw_tool_json("Ho creato il file e i test passano.")
    assert not looks_like_raw_tool_json('{"name": "qualcosa-che-non-e-un-tool"}')


def test_streaming_guard_detects_json_prefix():
    from core.agent import is_streaming_tool_json

    assert is_streaming_tool_json('{"name": "write_f')
    assert is_streaming_tool_json('```json\n{"name": "wri')
    assert not is_streaming_tool_json("Ho creato il file shopping_lists.py.")
    assert not is_streaming_tool_json("")


# ---------------------------------------------------------------------------
# Regressioni emerse dai transcript reali di qwen2.5-coder
# ---------------------------------------------------------------------------


def test_strips_hallucinated_tool_response():
    """Il modello finge che un tool abbia risposto: va scartato, non mostrato."""
    from core.textutils import strip_tool_wrappers

    text = (
        '<tool_response> {"status": "error", "message": "Non ho informazioni su '
        "un'applicazione esistente\"} </tool_response>"
    )
    clean, dropped = strip_tool_wrappers(text)
    assert clean == ""                       # niente da mostrare all'utente
    assert "Non ho informazioni" in dropped  # ...ma la prova resta, per il log


def test_strips_empty_tool_response_so_summary_can_fire():
    """<tool_response> </tool_response> non deve valere come 'ha risposto'."""
    from core.textutils import strip_tool_wrappers

    clean, dropped = strip_tool_wrappers("<tool_response> </tool_response>")
    assert clean == "" and dropped == ""


def test_tool_wrappers_do_not_touch_normal_text():
    from core.textutils import strip_tool_wrappers

    text = "Fatto: creato app.py.\nPoi: aggiungo i test?"
    assert strip_tool_wrappers(text) == (text, "")


def test_tool_call_wrapper_is_stripped_but_json_survives_for_recovery():
    """Qwen incarta la chiamata in <tool_call>: il JSON dentro resta recuperabile."""
    from core.agent import parse_text_tool_calls

    text = '<tool_call>\n{"name": "list_files", "arguments": {"subfolder": "."}}\n</tool_call>'
    calls, _ = parse_text_tool_calls(text)
    assert len(calls) == 1 and calls[0]["name"] == "list_files"


# ---------------------------------------------------------------------------
# Abitudine fissa: guardare il workspace prima di rispondere e di agire
# ---------------------------------------------------------------------------


def test_env_header_shows_the_tree_not_just_the_root(tmp_path):
    """L'agente deve conoscere la struttura senza spendere un giro di modello."""
    from core.prompts import build_env_header

    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "core" / "sub").mkdir()
    (tmp_path / "core" / "sub" / "deep.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# titolo\n", encoding="utf-8")

    header = build_env_header(str(tmp_path))
    assert "core/" in header
    assert "agent.py" in header      # secondo livello
    assert "deep.py" in header       # terzo livello
    assert "README.md" in header
    assert "read_file" in header     # dice che il contenuto non lo conosce


def test_env_header_hides_noise_and_stays_deterministic(tmp_path):
    from core.prompts import build_env_header

    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()

    header = build_env_header(str(tmp_path))
    assert "node_modules" not in header and ".git" not in header
    assert "src/" in header
    # deterministico: e' la condizione per il riuso del KV cache
    assert header == build_env_header(str(tmp_path))


def test_env_header_warns_when_the_project_is_too_big(tmp_path):
    from core.tools import SNAPSHOT_MAX_ENTRIES, workspace_snapshot

    for i in range(SNAPSHOT_MAX_ENTRIES + 30):
        (tmp_path / f"file{i:03d}.py").write_text("x\n", encoding="utf-8")
    snapshot = workspace_snapshot(str(tmp_path))
    assert "troncato" in snapshot and "search_files" in snapshot


def test_write_file_refuses_to_overwrite_an_unread_file(tmp_path):
    """Il guard-rail che rende obbligatorio 'guarda prima di agire'."""
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    target = tmp_path / "esistente.py"
    target.write_text("lavoro prezioso\n", encoding="utf-8")

    blocked = json.loads(
        dispatch(ctx, "write_file", {"filepath": "esistente.py", "content": "nuovo"})
    )
    assert "error" in blocked
    assert "read_file" in blocked["hint"] and "edit_file" in blocked["hint"]
    assert target.read_text(encoding="utf-8") == "lavoro prezioso\n", "il file e' stato toccato!"

    # dopo la lettura la scrittura passa
    dispatch(ctx, "read_file", {"filepath": "esistente.py"})
    written = json.loads(
        dispatch(ctx, "write_file", {"filepath": "esistente.py", "content": "nuovo"})
    )
    assert written["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "nuovo"


def test_creating_a_new_file_needs_no_read(tmp_path):
    """Il guard-rail non deve intralciare la creazione di file nuovi."""
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    result = json.loads(
        dispatch(ctx, "write_file", {"filepath": "nuovo.py", "content": "x = 1\n"})
    )
    assert result["status"] == "ok" and result["action"] == "creato"

    # e riscriverlo subito dopo e' lecito: il contenuto lo conosce, l'ha scritto lui
    again = json.loads(
        dispatch(ctx, "write_file", {"filepath": "nuovo.py", "content": "x = 2\n"})
    )
    assert again["status"] == "ok" and again["action"] == "sovrascritto"


def test_edit_file_also_marks_the_file_as_known(tmp_path):
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    (tmp_path / "m.py").write_text("a = 1\n", encoding="utf-8")
    dispatch(ctx, "read_file", {"filepath": "m.py"})
    dispatch(ctx, "edit_file", {"filepath": "m.py", "old_string": "a = 1", "new_string": "a = 2"})
    assert "m.py" in ctx.known_files


def test_thinking_tristate_resolution():
    from core.config import resolve_tristate

    assert resolve_tristate("auto", True) is True
    assert resolve_tristate("auto", False) is False
    assert resolve_tristate("auto", None) is False     # in dubbio, spento
    assert resolve_tristate("si", None) is True
    assert resolve_tristate("no", True) is False
    assert resolve_tristate(True, None) is True        # retrocompatibile


# --- il gestore password del browser non deve toccare la UI -----------------
# Un <input type="password"> nel pannello impostazioni faceva credere a Chrome
# che l'app avesse un login: offriva di salvare la "credenziale", la
# autocompilava con password dell'utente e mostrava l'avviso "password
# compromessa" (il default era la stringa "ollama", presente nei data breach).

def _ui_html() -> str:
    """index.html senza i commenti, che citano il bug e ne ripetono le stringhe."""
    raw = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S)


def test_no_password_inputs_in_the_ui():
    assert 'type="password"' not in _ui_html()


def test_the_api_key_field_is_masked_but_not_a_credential():
    html = _ui_html()
    css = (Path(__file__).resolve().parents[1] / "web" / "style.css").read_text(
        encoding="utf-8"
    )
    field = next(line for line in html.splitlines() if 'id="s-api-key"' in line)
    assert 'class="masked"' in field and 'autocomplete="off"' in field
    assert "-webkit-text-security" in css


def test_api_key_default_is_empty():
    from core.config import DEFAULTS

    assert DEFAULTS["api_key"] == ""


# --- efficienza: rileggere lo stesso file non ricosta 6.000 caratteri -------
# E' il caso piu' frequente del ciclo leggi -> modifica -> verifica -> rileggi.


def test_rereading_an_unchanged_file_costs_almost_nothing(tmp_path):
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    (tmp_path / "grande.py").write_text("x = 1\n" * 800, encoding="utf-8")

    first = dispatch(ctx, "read_file", {"filepath": "grande.py"})
    second = dispatch(ctx, "read_file", {"filepath": "grande.py"})

    assert json.loads(second)["status"] == "invariato"
    assert len(second) < len(first) / 10


def test_a_changed_file_is_read_again_in_full(tmp_path):
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    target = tmp_path / "m.py"
    target.write_text("a = 1\n", encoding="utf-8")
    dispatch(ctx, "read_file", {"filepath": "m.py"})

    target.write_text("a = 2\n", encoding="utf-8")
    again = json.loads(dispatch(ctx, "read_file", {"filepath": "m.py"}))
    assert "a = 2" in again["content"]


def test_the_cache_expires_when_the_content_leaves_the_context(tmp_path):
    """Dopo la compattazione 'usa quello di prima' sarebbe una bugia."""
    from core.config import TOOL_RESULT_FULL_WINDOW
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    (tmp_path / "m.py").write_text("a = 1\n", encoding="utf-8")
    dispatch(ctx, "read_file", {"filepath": "m.py"})

    ctx.step += TOOL_RESULT_FULL_WINDOW          # il risultato e' stato compattato
    again = json.loads(dispatch(ctx, "read_file", {"filepath": "m.py"}))
    assert again.get("status") != "invariato"
    assert "a = 1" in again["content"]


def test_a_partial_read_is_never_answered_from_the_cache(tmp_path):
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    (tmp_path / "m.py").write_text("\n".join(f"riga {i}" for i in range(50)), encoding="utf-8")
    dispatch(ctx, "read_file", {"filepath": "m.py"})

    partial = json.loads(
        dispatch(ctx, "read_file", {"filepath": "m.py", "start_line": 10, "end_line": 12})
    )
    assert partial.get("status") != "invariato"
    assert "riga 10" in partial["content"]


# --- schemi dei tool snelli -------------------------------------------------


def test_every_tool_has_a_lean_description():
    """Un tool nuovo senza descrizione snella tornerebbe muto a quella lunga."""
    from core.tools import LEAN_TOOL_DESCRIPTIONS, TOOL_NAMES

    assert set(LEAN_TOOL_DESCRIPTIONS) == set(TOOL_NAMES)


def test_lean_schemas_keep_the_contract_and_cut_only_the_prose():
    from core.tools import TOOLS_SCHEMA, TOOLS_SCHEMA_LEAN
    from core.textutils import estimate_tokens

    full = {t["function"]["name"]: t["function"] for t in TOOLS_SCHEMA}
    lean = {t["function"]["name"]: t["function"] for t in TOOLS_SCHEMA_LEAN}
    assert set(full) == set(lean)
    for name in full:
        # i parametri sono il contratto: non si toccano mai
        assert full[name]["parameters"] == lean[name]["parameters"]

    # Il risparmio si misura sul totale, non tool per tool: ask_user_question
    # e' volutamente piu' lungo nella versione snella (vedi il test dopo).
    risparmio = estimate_tokens(json.dumps(TOOLS_SCHEMA)) - estimate_tokens(
        json.dumps(TOOLS_SCHEMA_LEAN)
    )
    assert risparmio > 300


def test_ask_user_question_is_deliberately_not_shortened():
    """L'unico tool che il modello non chiama mai spontaneamente.

    Osservato con qwen3.5: con la descrizione ridotta a una riga il tool
    spariva di fatto dal repertorio, e il modello decideva da solo anche
    quando nel suo ragionamento scriveva "devo decidere cosa considerare
    anomalo". Qui i token si spendono apposta.
    """
    from core.tools import ASK_USER_TOOL, LEAN_TOOL_DESCRIPTIONS, TOOLS_SCHEMA

    completa = next(
        t["function"]["description"]
        for t in TOOLS_SCHEMA
        if t["function"]["name"] == ASK_USER_TOOL
    )
    snella = LEAN_TOOL_DESCRIPTIONS[ASK_USER_TOOL]
    assert len(snella) >= len(completa)
    assert "devo scegliere" in snella.lower()


# --- il test e' la specifica ------------------------------------------------
# Osservato con qwen3.5: davanti a una verifica rossa il modello ragiona "devo
# correggere il test o la funzione" e sceglie il test, riscrivendolo in una
# tautologia. La barra torna verde e non misura piu' niente.


def test_a_test_file_is_recognised_by_the_usual_conventions():
    from core.tools import looks_like_test_file

    for path in ("test_cose.py", "cose_test.py", "conftest.py",
                 "tests/qualsiasi.py", "pacchetto/tests/dati.json"):
        assert looks_like_test_file(path), path
    for path in ("statistiche.py", "src/protest.py", "latest.py", "contest.py"):
        assert not looks_like_test_file(path), path


def _red_ctx(tmp_path):
    from core.tools import ToolContext

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    ctx.red_command = "python -m pytest -v"
    return ctx


def test_with_a_red_verification_a_test_file_cannot_be_edited(tmp_path):
    from core.tools import dispatch

    target = tmp_path / "test_cose.py"
    target.write_text("def test_x():\n    assert calcola() == 3\n", encoding="utf-8")
    ctx = _red_ctx(tmp_path)

    out = json.loads(
        dispatch(ctx, "edit_file", {
            "filepath": "test_cose.py",
            "old_string": "== 3",
            "new_string": "is not None",      # l'indebolimento classico
        })
    )
    assert "error" in out
    assert "ask_user_question" in out["hint"]
    # e soprattutto: il file sul disco non e' cambiato
    assert "== 3" in target.read_text(encoding="utf-8")


def test_the_same_guard_covers_write_file(tmp_path):
    from core.tools import dispatch

    target = tmp_path / "test_cose.py"
    target.write_text("def test_x():\n    assert calcola() == 3\n", encoding="utf-8")
    ctx = _red_ctx(tmp_path)
    ctx.known_files.add("test_cose.py")       # anche gia' letto, resta bloccato

    out = json.loads(
        dispatch(ctx, "write_file", {"filepath": "test_cose.py", "content": "def test_x():\n    pass\n"})
    )
    assert "error" in out
    assert "== 3" in target.read_text(encoding="utf-8")


def test_the_code_can_still_be_fixed_while_red(tmp_path):
    """Il guard deve bloccare la scorciatoia, non il lavoro."""
    from core.tools import dispatch

    (tmp_path / "codice.py").write_text("def calcola():\n    return 2\n", encoding="utf-8")
    ctx = _red_ctx(tmp_path)
    dispatch(ctx, "read_file", {"filepath": "codice.py"})

    out = json.loads(
        dispatch(ctx, "edit_file", {
            "filepath": "codice.py", "old_string": "return 2", "new_string": "return 3",
        })
    )
    assert "error" not in out
    assert "return 3" in (tmp_path / "codice.py").read_text(encoding="utf-8")


def test_with_everything_green_the_tests_are_editable_again(tmp_path):
    """Aggiungere test per una funzione nuova deve restare libero."""
    from core.tools import ToolContext, dispatch

    target = tmp_path / "test_cose.py"
    target.write_text("def test_x():\n    assert calcola() == 3\n", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")   # nessun rosso
    dispatch(ctx, "read_file", {"filepath": "test_cose.py"})

    out = json.loads(
        dispatch(ctx, "edit_file", {
            "filepath": "test_cose.py",
            "old_string": "assert calcola() == 3",
            "new_string": "assert calcola() == 3\n\n\ndef test_y():\n    assert altro() == 1",
        })
    )
    assert "error" not in out
    assert "test_y" in target.read_text(encoding="utf-8")


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Semantica di shell POSIX: cmd.exe non usa 127 per 'comando "
        "inesistente'. Su Windows la strada supportata e' la sandbox Docker, "
        "dove la shell e' quella del container."
    ),
)
def test_a_missing_command_is_not_reported_as_a_failed_verification(tmp_path):
    """127 dice che il comando non esiste, non che il codice e' rotto."""
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    out = json.loads(dispatch(ctx, "run_command", {"command": "comando-che-non-esiste-xyz"}))
    assert out["returncode"] == 127
    assert out["esito"] == "COMANDO NON ESEGUIBILE"
    assert "non e' un test fallito" in out["next_step"].lower()


def test_una_verifica_verde_porta_con_se_l_istruzione_di_chiudere(tmp_path):
    """Il riepilogo di fine turno costava un'intera chiamata al modello: qwen
    finiva i tool e taceva, e SUMMARY_NUDGE doveva richiamarlo (3 volte su 3
    turni nelle sessioni misurate). L'istruzione ora viaggia con il risultato."""
    from core.tools import ToolContext, dispatch

    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    # sys.executable e non "python": quale interprete risponda a quel nome
    # dipende dal PATH della macchina, e sulla postazione Windows erano tre
    # (3.10, 3.12 del venv, 3.14 di sistema) con pytest installato in uno solo.
    out = json.loads(
        dispatch(ctx, "run_command", {"command": f'"{sys.executable}" -m pytest -q'})
    )
    assert out["esito"] == "ok"
    passo = out["next_step"].lower()
    assert "fatto:" in passo and "verifica:" in passo and "poi:" in passo
    # Condizionale: verde non vuol dire che il lavoro chiesto sia finito.
    assert "se il lavoro chiesto e' finito" in passo


def test_un_comando_che_ispeziona_non_e_una_verifica(tmp_path):
    """`ls` torna 0 anche quando non ha misurato niente: se lo trattassimo come
    verifica verde, il modello chiuderebbe il turno a meta' lavoro."""
    from core.tools import ToolContext, dispatch, looks_like_verification

    assert looks_like_verification("python -m pytest test_x.py -v")
    assert looks_like_verification("cd /work && ruff check .")
    assert looks_like_verification("npm test")
    assert not looks_like_verification("ls -la")
    assert not looks_like_verification("tail -5 telemetria.py")
    assert not looks_like_verification('grep -n "def analizza" telemetria.py')

    # Un comando che ispeziona e basta, scritto nella lingua della shell che
    # c'e': `ls` su Windows non esiste e tornava FALLITO, facendo sembrare
    # rotto il codice invece del comando.
    ispeziona = "cmd /c dir" if os.name == "nt" else "ls"
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    out = json.loads(dispatch(ctx, "run_command", {"command": ispeziona}))
    assert out["esito"] == "ok"
    assert "next_step" not in out


def test_an_unrunnable_command_does_not_stay_red_forever():
    """Il bug: un comando malformato, poi giustamente abbandonato, restava
    rosso nel tracker e faceva scattare un sollecito assurdo a lavoro finito."""
    from core.agent import VerificationTracker

    tracker = VerificationTracker()
    tracker.record("run_command", json.dumps({
        "command": "cd /work && pytest", "returncode": 127, "esito": "COMANDO NON ESEGUIBILE"}))
    assert tracker.unresolved is None

    # una verifica vera che fallisce invece resta rossa
    tracker.record("run_command", json.dumps({
        "command": "pytest -q", "returncode": 1, "esito": "FALLITO"}))
    assert tracker.unresolved is not None
    assert tracker.unresolved[0] == "pytest -q"


def test_la_versione_e_la_stessa_in_pyproject():
    """Le due fonti della versione devono dire lo stesso numero.

    Non lo dicevano da un pezzo: ``core.config.APP_VERSION`` era arrivata a
    2.27.2 (2.30.0 nel fork) mentre ``pyproject.toml`` era rimasto a 2.11.0,
    sedici minori indietro. Nessuno se n'era accorto perche' niente le
    confrontava, ed e' esattamente il tipo di cosa che un test scopre subito e
    una persona non scopre mai.
    """
    from core.config import APP_VERSION

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    testo = pyproject.read_text(encoding="utf-8")
    dichiarata = re.search(r'^version\s*=\s*"([^"]+)"', testo, re.M)
    assert dichiarata, "pyproject.toml non dichiara nessuna version"
    assert dichiarata.group(1) == APP_VERSION


def test_le_dipendenze_sono_le_stesse_in_requirements():
    """Le stesse dipendenze, dichiarate due volte.

    ``requirements.txt`` esiste perche' ``Dockerfile.sandbox`` installa da li':
    la sandbox e' un'altra macchina e non legge il pyproject di questo
    progetto. Il commento in testa al file chiede di toccarle insieme, ed e'
    tutto cio' che le teneva allineate -- cioe' niente, come per la versione
    qui sopra. Se divergono, l'agente lavora dentro un container con
    dipendenze diverse da quelle con cui gira l'harness, e lo scopre da un
    ImportError a meta' turno.

    Il confronto e' sulla riga intera, vincolo compreso: due ``httpx`` con
    minimi diversi sono gia' una divergenza.
    """
    import tomllib

    radice = Path(__file__).resolve().parents[1]
    dati = tomllib.loads((radice / "pyproject.toml").read_text(encoding="utf-8"))
    dichiarate = {d.strip() for d in dati["project"]["dependencies"]}
    righe = (radice / "requirements.txt").read_text(encoding="utf-8").splitlines()
    installate = {r.strip() for r in righe if r.strip() and not r.startswith("#")}
    assert installate == dichiarate, (
        "pyproject.toml e requirements.txt non dichiarano le stesse dipendenze -- "
        f"solo nel pyproject: {sorted(dichiarate - installate)}; "
        f"solo in requirements: {sorted(installate - dichiarate)}"
    )


# ---------------------------------------------------------------------------
# Correzioni dell'audit del 30/08/2026
# ---------------------------------------------------------------------------


def test_una_memoria_non_si_cancella_per_sottostringa():
    """Il ripiego cancellava la prima memoria che *conteneva* il testo citato.

    Con "preferisce le risposte brevi" e "preferisce le risposte brevi nei
    riepiloghi", un remove sul testo della seconda cancellava la prima. Sono i
    fatti stabili dell'utente, accumulati per mesi, e non c'e' un annulla.
    """
    from core.memory import MemoriaAmbigua, add_memory, remove_memory

    memorie: list[dict[str, str]] = []
    add_memory(memorie, "preferisce le risposte brevi")
    add_memory(memorie, "preferisce le risposte brevi nei riepiloghi")

    # il testo esatto toglie quella giusta
    assert remove_memory(memorie, "preferisce le risposte brevi nei riepiloghi")
    assert [m["text"] for m in memorie] == ["preferisce le risposte brevi"]

    # e una citazione che ne combacia due si rifiuta invece di indovinare
    add_memory(memorie, "preferisce le risposte brevi nei riepiloghi")
    with pytest.raises(MemoriaAmbigua):
        remove_memory(memorie, "preferisce le risposte")
    assert len(memorie) == 2


def test_un_termine_di_skill_non_combacia_a_meta_parola():
    """Il docstring prometteva il confine di parola, il regex ne aveva uno solo.

    ``(?<!\\w)test`` controlla che prima non ci sia una lettera, non che dopo la
    parola finisca: cercando "test" si prendeva "testo", e una skill caricata a
    sproposito costa il doppio -- i suoi token piu' l'attenzione che ruba.
    """
    from core.skills import Skill

    s = Skill(nome="prove", descrizione="", termini=("test",), corpo="...",
              origine="harness")
    assert s.combacia("lancia i test del progetto")
    assert not s.combacia("il testo del file e' cambiato")
    assert not s.combacia("questo e' il contesto")


def test_una_skill_lunga_non_nasconde_quelle_dopo():
    """``break`` sul tetto faceva sparire tutte le procedure successive."""
    from core.skills import MAX_TOTALE_CHARS, Skill, scegli

    # Piu' grande del tetto: da sola non ci sta, e prima ``break`` faceva
    # sparire anche tutte quelle che venivano dopo.
    lunga = Skill(nome="lunga", descrizione="", termini=("deploy",),
                  corpo="x" * (MAX_TOTALE_CHARS + 1), origine="harness")
    corta = Skill(nome="corta", descrizione="", termini=("deploy",), corpo="breve",
                  origine="harness")
    scelte = [s.nome for s in scegli([lunga, corta], "fai il deploy")]
    assert scelte == ["corta"]


def test_il_conto_dei_token_non_dimentica_le_immagini():
    """Contarle zero faceva credere libero uno spazio occupato."""
    from core.textutils import TOKEN_PER_IMMAGINE, estimate_messages_tokens

    senza = [{"role": "user", "content": "guarda"}]
    con = [{"role": "user", "content": "guarda", "images": ["b64", "b64"]}]
    assert estimate_messages_tokens(con) - estimate_messages_tokens(senza) == (
        2 * TOKEN_PER_IMMAGINE
    )


def test_il_conto_dei_token_include_i_campi_del_template():
    """``name`` e ``tool_call_id`` stanno su ogni risultato di tool."""
    from core.textutils import estimate_messages_tokens

    nudo = [{"role": "tool", "content": "ok"}]
    vestito = [{"role": "tool", "content": "ok", "name": "read_file",
                "tool_call_id": "call_abc123def456"}]
    assert estimate_messages_tokens(vestito) > estimate_messages_tokens(nudo)


def test_il_piano_non_perde_il_punto_in_corso_in_silenzio():
    """Un ``set`` che non lo rinomina lo lasciava fuori senza dire niente."""
    from core.plan import Plan, PlanError

    piano = Plan()
    piano.set_steps(["leggere il modulo", "scrivere il test", "lanciare la suite"])
    piano.avanza()
    assert piano.current is not None

    with pytest.raises(PlanError) as errore:
        piano.set_steps(["tutt'altro piano", "senza il punto aperto"])
    assert piano.current.id in str(errore.value)

    # ...ma riscriverlo tenendolo dentro resta legittimo
    piano.set_steps([piano.current.text, "un punto nuovo"])
    assert piano.current is not None


def test_un_comando_lungo_riuscito_non_si_colora_di_rosso():
    """``'"esito": "ok"' not in result[:200]`` sbagliava in due modi.

    Il campo ``esito`` viene dopo ``command`` e ``stdout`` nella busta di
    ``run_command``: con un comando lungo, o un output che comincia subito, a
    200 caratteri non ci si arriva e ogni comando **riuscito** veniva contato
    come fallito -- tendina rossa su un `pytest` verde. E al contrario, un
    output che contiene quella stringa per conto suo faceva passare per
    riuscito un comando fallito.
    """
    from core.agent import _comando_riuscito

    lungo = json.dumps(
        {"command": "pytest " + "tests/test_x.py " * 20, "stdout": "." * 400,
         "returncode": 0, "esito": "ok"}
    )
    assert _comando_riuscito(lungo) is True
    assert _comando_riuscito(json.dumps({"esito": "FALLITO"})) is False
    bugiardo = json.dumps({"esito": "FALLITO", "stdout": '"esito": "ok"'})
    assert _comando_riuscito(bugiardo) is False


def test_le_impostazioni_hanno_una_regola_sola_sui_tipi():
    """La rotta e la rilettura all'avvio devono accettare le stesse cose.

    Ne avevano due: la copia in ``server/main`` rifiutava un ``true`` su un
    campo intero, ``load_settings`` lo accettava (``isinstance(True, int)`` è
    vero). Le due porte della stessa casa con due serrature diverse -- e quella
    che decide davvero è la seconda, perché è quella che parla al prossimo
    avvio.
    """
    from core import settings as settings_mod
    from server import main as server_main

    assert server_main._tipo_compatibile is settings_mod.tipo_compatibile
    assert settings_mod.tipo_compatibile(True, 0) is False
    assert settings_mod.tipo_compatibile(1, 0.5) is True
    assert settings_mod.tipo_compatibile("grande", 0) is False
    assert settings_mod.tipo_compatibile(True, False) is True


def test_read_file_conta_le_righe_come_le_conta_un_editor(tmp_path):
    """``text.count("\\n") + 1`` conta una riga in più su ogni file che finisce
    con un a capo -- cioè su quasi tutti.

    È il numero su cui il modello calcola ``start_line``/``end_line`` per la
    lettura successiva: sbagliarlo di uno significa chiedere una riga che non
    esiste e ricevere un intervallo vuoto, senza che niente spieghi perché.
    """
    from core.tools import ToolContext, dispatch

    f = tmp_path / "tre_righe.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    esito = json.loads(dispatch(ctx, "read_file", {"filepath": "tre_righe.py"}))
    assert esito["total_lines"] == 3


def test_un_comando_che_contiene_observe_non_e_un_server():
    """``"serve "`` come sottostringa combacia con «observe », «preserve ».

    Il comando veniva rifiutato da ``run_command`` come se fosse un server che
    non finisce mai: un no a un comando legittimo, e senza spiegazione.
    """
    from core.tools import looks_like_server

    assert looks_like_server("npm run serve") is True
    assert looks_like_server("npx serve -s build") is True
    assert looks_like_server("grep preserve core/tools.py") is False
    assert looks_like_server("python observe.py") is False
