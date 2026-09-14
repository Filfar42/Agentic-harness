"""Regression attacks against the model/action and durable state boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import agent, session
from core.backend import StreamEvent
from core.config import GenParams
from core.jsonsafe import JsonBoundaryError, loads_object
from core.tools import TOOLS_SCHEMA, ToolContext


class ScriptBackend:
    """Finite deterministic model transcript, including stream cleanup tracking."""

    def __init__(self, steps: list[list[StreamEvent]]) -> None:
        self.steps = iter(steps)
        self.closed = 0
        self.requests: list[Any] = []

    def stream(self, messages: Any, tools: Any, params: Any):
        self.requests.append(messages)
        try:
            yield from next(self.steps, [StreamEvent("content", text="Terminato.")])
        finally:
            self.closed += 1


def call(name: str, args: Any, ident: str = "c1") -> StreamEvent:
    return StreamEvent("tool_call", tool_call={
        "id": ident, "name": name,
        "arguments": args if isinstance(args, str) else json.dumps(args),
    })


def run(tmp_path: Path, steps: list[list[StreamEvent]], **options: Any):
    backend = ScriptBackend(steps)
    messages = [{"role": "user", "content": "Crea i file richiesti."}]
    kwargs = {"backend": backend, "params": GenParams(model="fake"), "tools_schema": TOOLS_SCHEMA,
              "tool_ctx": ToolContext(workspace=str(tmp_path), sandbox="host"),
              "ui_messages": messages, "system_prompt": "SYS", "env_header": None,
              "max_steps": 3, "enable_nudge": False, "require_plan": False,
              "require_summary": False, "compact_history": False, "auto_preview": False,
              "estratto_pensiero": False, "libreria_attiva": False}
    kwargs.update(options)
    return list(agent.run_turn(**kwargs)), messages, backend


@pytest.mark.parametrize("raw", [
    '[]', 'null', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}',
    '{"a":"\\ud800"}', '{"a":1,}', '```json\n{}\n```',
    '{"a":"line\nline"}', '{"a":' * 40 + '0' + '}' * 40,
])
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(JsonBoundaryError):
        loads_object(raw)


def test_valid_multiline_and_braces_are_unchanged():
    value = {"content": 'print("{x}")\npath = "C:\\tmp"\n'}
    assert loads_object(json.dumps(value)) == value


def test_text_example_never_becomes_an_action(tmp_path):
    text = json.dumps({"name": "write_file", "arguments": {"filepath": "oops.txt", "content": "bad"}})
    run(tmp_path, [[StreamEvent("content", text=text)]])
    assert not (tmp_path / "oops.txt").exists()


def test_absent_schema_tool_cannot_execute(tmp_path):
    events, messages, _ = run(tmp_path, [[call("write_file", {"filepath": "oops.txt", "content": "bad"})]], tools_schema=[])
    assert not (tmp_path / "oops.txt").exists()
    assert json.loads(next(m["content"] for m in messages if m["role"] == "tool"))["error_code"] == "tool_not_allowed"
    assert events[-1].reason == "completed"


def test_invalid_args_self_correct_without_session_failure(tmp_path):
    events, messages, backend = run(tmp_path, [
        [call("write_file", '{"filepath":"bad.txt","filepath":"oops.txt","content":"bad"}')],
        [call("write_file", {"filepath": "good.txt", "content": "correct"}, "c2")],
        [StreamEvent("content", text="Fatto e verificato.")],
    ])
    assert not (tmp_path / "oops.txt").exists()
    assert (tmp_path / "good.txt").read_text() == "correct"
    assert events[-1].reason == "completed"
    first_error = next(m for m in messages if m["role"] == "tool")
    assert json.loads(first_error["content"])["error_code"] == "invalid_json"
    historical = next(m for m in backend.requests[1] if m.get("tool_calls"))
    assert historical["tool_calls"][0]["function"]["arguments"] == "{}"
    assert "oops.txt" in messages[1]["tool_calls"][0]["function"]["arguments"]


def test_multiple_questions_have_no_orphan_call_after_resume(tmp_path):
    question = {"question": "Scegli il formato", "options": ["A", "B"]}
    events, messages, _ = run(tmp_path, [[call("ask_user_question", question), call("ask_user_question", question, "c2")]])
    assert events[-1].reason == "awaiting_user"
    agent.resume_with_answer(messages, "A")
    results = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert sorted(results) == ["c1", "c2"]


def test_error_event_closes_generator(tmp_path):
    events, _, backend = run(tmp_path, [[StreamEvent("error", text="permanent failure")]])
    assert events[-1].reason == "error"
    assert backend.closed == 1


def test_bad_envelope_never_executes_partial_batch(tmp_path):
    events, _, _ = run(tmp_path, [[
        call("write_file", {"filepath": "bad.txt", "content": "bad"}),
        call("write_file", {"filepath": "other.txt", "content": "bad"}),
    ]])
    assert events[-1].reason == "error"
    assert not (tmp_path / "bad.txt").exists()


def test_child_turn_does_not_delete_parent_scratch(tmp_path):
    scratch = tmp_path / ".analisi"
    scratch.mkdir()
    probe = scratch / "valuable.txt"
    probe.write_text("parent work")
    run(tmp_path, [[StreamEvent("content", text="Fine.")]], initialize_workspace=False)
    assert probe.read_text() == "parent work"


def test_storage_failure_is_observable_and_retry_not_debounced(tmp_path, monkeypatch):
    state = {"current_session_id": "audit", "messages": [{"role": "user", "content": "persist me"}]}
    real = session._atomic_write_json
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(session, "_atomic_write_json", fail)
    assert session.save_session(state, force=True) is False
    assert "disk full" in state["save_error"]
    monkeypatch.setattr(session, "_atomic_write_json", real)
    assert session.save_session(state) is True
    assert "save_error" not in state


@pytest.mark.parametrize("raw", ['[]', '{"n_messages":"x"}', '{"messages":[1]}'])
def test_corrupt_metadata_does_not_crash_index_or_restore(tmp_path, raw):
    session.ensure_dirs()
    (session.DATA_DIR / "bad.json").write_text(raw)
    assert session.load_session({}, "bad") is False
    assert not session.list_sessions()


def test_session_path_cannot_escape_storage(tmp_path):
    assert session.load_session({}, "../outside") is False
    with pytest.raises(ValueError):
        session.delete_session("../outside")
