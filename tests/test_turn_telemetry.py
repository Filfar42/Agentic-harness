"""Model-call accounting across real turns, service generations and resumes."""

from __future__ import annotations

import json
import threading

import pytest

from core import agent, libreria, session
from core.backend import StreamEvent
from core.config import GenParams
from core.plan import Plan
from core.telemetry import track_backend
from core.tools import ToolContext


def _call(name, arguments):
    return StreamEvent("tool_call", tool_call={
        "id": "call_1", "name": name, "arguments": json.dumps(arguments),
    })


def _usage(prompt, completion, **extra):
    return StreamEvent("usage", usage={
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion, "done_reason": "stop", **extra,
    })


class ScriptBackend:
    supports_cancellation = True

    def __init__(self, scripts, *, cancel_call=None):
        self.scripts = scripts
        self.cancel_call = cancel_call
        self.cancelled = threading.Event()
        self.requests = []
        self.closed = []

    def stream(self, messages, tools, params, **kwargs):
        index = len(self.requests)
        self.requests.append({"messages": messages, "tools": tools, "params": params, **kwargs})
        try:
            for position, event in enumerate(self.scripts[index]):
                if self.cancel_call == index and position == 1:
                    self.cancelled.set()
                yield event
        finally:
            self.closed.append(index)


def _options(tmp_path, backend, *, tool_names=(), ctx=None, messages=None, **extra):
    options = {
        "backend": backend,
        "params": GenParams(model="fake", num_ctx=16384, max_tokens=2048),
        "tools_schema": [{"type": "function", "function": {"name": name, "parameters": {}}}
                         for name in tool_names],
        "tool_ctx": ctx if ctx is not None else ToolContext(workspace=str(tmp_path), sandbox="host"),
        "ui_messages": messages if messages is not None else [{"role": "user", "content": "procedi"}],
        "system_prompt": "SYS", "env_header": None, "max_steps": 1,
        "enable_nudge": False, "require_summary": False, "require_plan": False,
        "plan_gate": False, "compact_history": False, "auto_preview": False,
        "estratto_pensiero": False, "libreria_attiva": False, "spec_delega": False,
        "think_watchdog": False, "initialize_workspace": False,
    }
    options.update(extra)
    return options


def _combined(tmp_path, *, cancel_call=None, failed_service=None, native_cancel=True):
    plan = Plan()
    plan.set_steps(["Verificare il modulo", "Integrare il risultato"])
    plan.avanza()
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", plan=plan)
    scripts = [
        [StreamEvent("reasoning", text="Verificato nel sorgente: la funzione sta in core.baz. " * 40),
         _call("manage_plan", {"action": "complete", "step_id": "1", "note": "verificato"}),
         _usage(100, 10, prompt_eval_ms=5)],
        [StreamEvent("content", text="SCOPERTO: la funzione verificata sta in core.baz."), _usage(20, 2)],
        [StreamEvent("content", text="Ho verificato il modulo, resta l'integrazione."), _usage(30, 3)],
    ]
    if failed_service is not None:
        scripts[failed_service].insert(1, StreamEvent("error", text="permanent backend failure"))
    backend = ScriptBackend(scripts, cancel_call=cancel_call)
    backend.supports_cancellation = native_cancel
    options = _options(tmp_path, backend, tool_names=("manage_plan",), ctx=ctx,
                       require_summary=True, libreria_attiva=True, estratto_pensiero=True,
                       should_stop=backend.cancelled.is_set)
    events = list(agent.run_turn(**options))
    return events, options, backend


def test_main_memory_and_final_summary_usage_is_totalled_and_persisted(tmp_path):
    events, options, backend = _combined(tmp_path)
    finished = events[-1]
    assert isinstance(finished, agent.TurnFinished)
    assert finished.reason == "max_steps"
    assert backend.closed == [0, 1, 2]
    telemetry = finished.telemetry
    assert [call["purpose"] for call in telemetry["calls"]] == ["main", "memory_extract", "final_summary"]
    assert telemetry["totals"]["calls"] == 3
    assert telemetry["totals"]["usage_input_tokens"] == 150
    assert telemetry["totals"]["usage_output_tokens"] == 15
    assert telemetry["totals"]["usage_total_tokens"] == 165
    assert finished.usage["prompt_tokens"] == 150
    assert finished.usage["completion_tokens"] == 15
    assert finished.usage["total_tokens"] == 165
    assert finished.usage["model_wall_ms"] == telemetry["totals"]["wall_time_ms"]
    assert finished.usage["total_ms"] == telemetry["turn_wall_ms"]
    assert all(request["should_stop"]() is False for request in backend.requests)
    assert len(libreria.voci(tmp_path)) == 1
    assert options["ui_messages"][-1]["forzato"] is True

    state = {"messages": options["ui_messages"], "current_session_id": "combined"}
    session.record_turn_telemetry(state, telemetry, reason=finished.reason, steps=finished.steps)
    assert session.save_session(state, force=True)
    reopened = {}
    assert session.load_session(reopened, "combined")
    saved = reopened["turn_telemetry"][0]
    assert saved["totals"]["by_purpose"]["memory_extract"]["usage_total_tokens"] == 22
    assert saved["totals"]["by_purpose"]["final_summary"]["usage_total_tokens"] == 33
    assert saved["turn_reason"] == "max_steps"


@pytest.mark.parametrize("cancel_call", [0, 1, 2])
@pytest.mark.parametrize("native_cancel", [True, False])
def test_stop_closes_active_main_or_auxiliary_call_and_drops_partial_service(tmp_path, cancel_call, native_cancel):
    events, options, backend = _combined(tmp_path, cancel_call=cancel_call, native_cancel=native_cancel)
    finished = events[-1]
    assert finished.reason == "stopped"
    assert backend.closed == list(range(cancel_call + 1))
    assert finished.telemetry["totals"]["calls"] == cancel_call + 1
    assert finished.telemetry["calls"][-1]["outcome"] == "interrupted"
    assert not any(message.get("forzato") for message in options["ui_messages"])
    assert len(libreria.voci(tmp_path)) == (1 if cancel_call == 2 else 0)


def test_already_stopped_turn_has_no_model_call_to_bill(tmp_path):
    backend = ScriptBackend([])
    backend.cancelled.set()
    finished = list(agent.run_turn(**_options(tmp_path, backend, should_stop=backend.cancelled.is_set)))[-1]
    assert finished.reason == "stopped"
    assert backend.requests == []
    assert finished.telemetry["totals"]["calls"] == 0
    assert finished.telemetry["totals"]["usage_total_tokens"] is None


@pytest.mark.parametrize("failed_service", [1, 2])
def test_auxiliary_error_is_counted_as_error_even_when_consumer_closes_early(tmp_path, failed_service):
    events, options, backend = _combined(tmp_path, failed_service=failed_service)
    finished = events[-1]
    assert backend.closed == [0, 1, 2]
    assert finished.telemetry["calls"][failed_service]["outcome"] == "error"
    assert finished.telemetry["totals"]["error"] == 1
    assert finished.telemetry["totals"]["usage_missing_calls"] == 1
    assert len(libreria.voci(tmp_path)) == (0 if failed_service == 1 else 1)
    assert any(message.get("forzato") for message in options["ui_messages"]) is (failed_service == 1)


def test_main_error_is_not_mislabelled_as_user_interruption(tmp_path):
    backend = ScriptBackend([[StreamEvent("error", text="permanent backend failure")]])
    events = list(agent.run_turn(**_options(tmp_path, backend)))
    assert events[-1].reason == "error"
    assert events[-1].telemetry["totals"]["error"] == 1
    assert events[-1].telemetry["totals"]["interrupted"] == 0
    assert backend.closed == [0]


def test_consumer_closing_turn_releases_model_and_records_interruption(tmp_path):
    backend = ScriptBackend([[StreamEvent("content", text="prima parte"), _usage(10, 2)]])
    tracked = track_backend(backend)
    turn = agent.run_turn(**_options(tmp_path, tracked))
    for event in turn:
        if isinstance(event, agent.ContentDelta):
            break
    turn.close()
    assert backend.closed == [0]
    assert tracked.collector.totals()["calls"] == 1
    assert tracked.collector.totals()["interrupted"] == 1


def test_reusing_collector_excludes_previous_turn_usage(tmp_path):
    backend = ScriptBackend([
        [StreamEvent("content", text="prima risposta"), _usage(10, 1)],
        [StreamEvent("content", text="seconda risposta"), _usage(30, 3)],
    ])
    tracked = track_backend(backend)
    options = _options(tmp_path, tracked)
    first = list(agent.run_turn(**options))[-1]
    options["ui_messages"].append({"role": "user", "content": "continua"})
    second = list(agent.run_turn(**options))[-1]
    assert first.usage["prompt_tokens"] == 10
    assert second.usage["prompt_tokens"] == 30
    assert first.telemetry["totals"]["calls"] == second.telemetry["totals"]["calls"] == 1
    assert second.telemetry["since_offset"] == first.telemetry["end_offset"]
    assert tracked.collector.totals()["prompt_tokens"] == 40


@pytest.mark.parametrize("tool_name", ["esplora", "wiki_search"])
def test_reused_tool_context_binds_auxiliary_calls_to_current_turn_backend(tmp_path, monkeypatch, tool_name):
    def service(*args, **kwargs):
        output = list(kwargs["backend"].stream([], None, kwargs["params"]))
        return {"referto": next(event.text for event in output if event.kind == "content")}
    monkeypatch.setattr(agent.delega_mod, "esegui", service)
    monkeypatch.setattr(agent.wiki_search_mod, "cerca_nella_wiki", service)
    arguments = ({"compito": "Trova la funzione nel workspace"} if tool_name == "esplora"
                 else {"progetto": "documenti", "query": "Trova la funzione nella wiki"})
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    messages = [{"role": "user", "content": "procedi con la ricerca"}]
    backends, finished = [], []
    for number in (1, 2):
        backend = ScriptBackend([
            [_call(tool_name, arguments), _usage(10 * number, number)],
            [StreamEvent("content", text=f"referto {number}"), _usage(20 * number, number)],
            [StreamEvent("content", text="Fatto."), _usage(30 * number, number)],
        ])
        backends.append(backend)
        finished.append(list(agent.run_turn(**_options(
            tmp_path, backend, tool_names=(tool_name,), ctx=ctx, messages=messages, max_steps=2,
        )))[-1])
        messages.append({"role": "user", "content": "ripeti la ricerca con il nuovo modello"})
    assert [len(backend.requests) for backend in backends] == [3, 3]
    purpose = "delegate" if tool_name == "esplora" else "wiki_search"
    assert [turn.telemetry["totals"]["calls"] for turn in finished] == [3, 3]
    assert [turn.usage["prompt_tokens"] for turn in finished] == [60, 120]
    assert [turn.telemetry["totals"]["by_purpose"][purpose]["prompt_tokens"]
            for turn in finished] == [20, 40]
