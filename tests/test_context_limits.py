"""Limiti reali del payload e interruzioni all'ultimo passo."""
from __future__ import annotations

import json

import pytest

from core import agent
from core.backend import StreamEvent
from core.config import Budgets, GenParams
from core.context import compact_result
from core.tools import ToolContext


@pytest.mark.parametrize("full", [False, True])
def test_freeform_metadata_cannot_bypass_result_budget(full):
    payload = {"command": "python " + "x" * 30_000, "source": "s" * 30_000,
               "version": {"message": "v" * 30_000}, "status": "bad" * 10_000,
               "returncode": 1, "stdout": "completed step one", "stderr": "failed step two",
               "stdout_deposito": ".deposito/full.txt"}
    result = compact_result(json.dumps(payload), full=full,
                            budgets=Budgets(tool_result_max_chars=1200))
    reduced = json.loads(result)
    assert len(result) < 2500
    assert reduced["returncode"] == 1
    assert reduced["stderr"] == payload["stderr"]
    assert reduced["stdout_deposito"] == payload["stdout_deposito"]
    assert reduced["_context_truncated"] is True


def test_nested_long_keys_cannot_bypass_budget():
    raw = json.dumps({"matches": [{"x" * 30_000: "value"}]})
    reduced = compact_result(raw, full=True, budgets=Budgets(tool_result_max_chars=1200))
    assert len(reduced) < 1400
    assert json.loads(reduced)["_context_truncated"] is True


def test_old_human_clarification_keeps_exact_constraints():
    answer = "vincoli specifici " * 900 + "NON modificare legacy.sql"
    payload = json.dumps({"user_answer": answer, "question": "Quali vincoli?"})
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "ask", "type": "function", "function": {
                "name": "ask_user_question", "arguments": '{"question":"Quali vincoli?"}',
            },
        }]},
        {"role": "tool", "name": "ask_user_question", "tool_call_id": "ask",
         "answered": True, "content": payload},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "ls", "type": "function", "function": {
                "name": "list_files", "arguments": "{}",
            },
        }]},
        {"role": "tool", "name": "list_files", "tool_call_id": "ls", "content": "{}"},
    ]
    api = agent.build_api_messages(messages, system_prompt="SYS", env_header=None,
                                   budgets=Budgets(tool_result_full_window=1))
    question = next(m for m in api if m.get("tool_call_id") == "ask")
    assert json.loads(question["content"])["user_answer"] == answer


def test_tool_schema_pressure_triggers_compaction_before_generation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "compatta_cronologia", lambda messages, **kw: calls.append(kw))

    class Backend:
        def stream(self, messages, tools, params):
            yield StreamEvent("content", text="ok")

    events = list(agent.run_turn(
        backend=Backend(), params=GenParams(num_ctx=8192),
        tools_schema=[{"type": "function", "function": {
            "name": "example", "description": "x" * 23_000, "parameters": {},
        }}],
        tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"),
        ui_messages=[{"role": "user", "content": "ciao"}],
        system_prompt="SYS", env_header=None, max_steps=1,
        require_plan=False, require_summary=False, plan_gate=False, enable_nudge=False,
        estratto_pensiero=False, libreria_attiva=False, think_watchdog=False,
    ))
    assert len(calls) == 1
    assert callable(calls[0]["should_stop"])
    assert any(isinstance(e, agent.TurnFinished) for e in events)


@pytest.mark.parametrize("ending", ["length", "error", "stop_requested"])
def test_forced_summary_never_publishes_partial_service_output(ending):
    closed = []
    cancelled = [False]

    class Backend:
        supports_cancellation = True

        def stream(self, messages, tools, params, *, should_stop):
            try:
                yield StreamEvent("content", text="partial summary")
                if ending == "stop_requested":
                    cancelled[0] = True
                    yield StreamEvent("content", text="ignored")
                elif ending == "error":
                    yield StreamEvent("error", text="disconnected")
                else:
                    yield StreamEvent("usage", usage={"done_reason": "length"})
            finally:
                closed.append(True)

    result = agent.riepilogo_finale(
        [{"role": "user", "content": "finish"}], backend=Backend(),
        params=GenParams(), budgets=Budgets(), strip_thinking=True,
        should_stop=lambda: cancelled[0],
    )
    assert result == ""
    assert closed == [True]
