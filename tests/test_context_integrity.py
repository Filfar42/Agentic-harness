"""Regressioni operative di potatura, checkpoint e controllo del loop."""
from __future__ import annotations

import json
import hashlib

from core import agent
from core.backend import StreamEvent
from core.config import Budgets, GenParams, budgets_for
from core.context import deposit_references
from core.plan import Plan
from core.tools import ToolContext, TOOLS_SCHEMA, dispatch
from tests.test_compattazione import FintoBackend, cronologia, scrittura


def test_old_result_keeps_recovery_handles_and_exit_status():
    payload = {"returncode": 1, "stdout": "x" * 4000, "stderr": "failure" * 500,
               "stdout_deposito": ".deposito/out.txt", "stderr_deposito": ".deposito/err.txt",
               "deposito": ".deposito/search.txt", "sha256": "a" * 64}
    reduced = json.loads(agent._compact_tool_result(json.dumps(payload), full=False))
    for key in ("returncode", "stdout_deposito", "stderr_deposito", "deposito", "sha256"):
        assert reduced[key] == payload[key]
    assert len(reduced["stdout"]) < len(payload["stdout"])
    assert deposit_references([{"role": "tool", "content": json.dumps(reduced)}]) == {
        ".deposito/out.txt", ".deposito/err.txt", ".deposito/search.txt",
    }


def test_recent_file_uses_read_budget_and_keeps_valid_json():
    text = 'print("hello")\n' * 700
    raw = json.dumps({"filepath": "a.py", "range": "1-700", "content": text})
    reduced = json.loads(agent._compact_tool_result(raw, full=True, budgets=budgets_for(16384)))
    assert reduced["content"] == text
    assert reduced["range"] == "1-700"
    huge = json.loads(agent._compact_tool_result(
        json.dumps({"filepath": "a.py", "content": text * 20}), full=True,
        budgets=Budgets(read_file_max_chars=1000),
    ))
    assert huge["_context_truncated"] is True
    assert len(huge["content"]) <= 1000


def test_failed_write_preserves_attempt_and_success_refers_to_current_version():
    msgs = scrittura("failed", "a.py", "UNSAVED" * 200)
    msgs[-1].update(ok=False, content=json.dumps({"error": "permission denied"}))
    msgs += scrittura("success", "b.py", "OLD_VERSION" * 200)
    msgs += scrittura("recent", "b.py", "CURRENT" * 200)
    api = agent.build_api_messages(msgs, system_prompt="", env_header=None,
                                   budgets=Budgets(tool_result_full_window=1))
    calls = {c["id"]: json.loads(c["function"]["arguments"])
             for m in api for c in m.get("tool_calls", [])}
    assert calls["failed"]["content"] == "UNSAVED" * 200
    assert "versione attuale" in calls["success"]["content"]
    assert calls["recent"]["content"] == "CURRENT" * 200


def test_two_checkpoints_summarize_only_new_events_and_keep_user_constraints(tmp_path):
    msgs = cronologia(14)
    msgs[0]["content"] = "NON toccare legacy"
    msgs[1]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {"filepath": "OLD_UNIQUE.py", "content": "x" * 4000})
    backend = FintoBackend()
    kw = {"backend": backend, "params": GenParams(num_ctx=8192), "budgets": budgets_for(8192),
          "strip_thinking": True, "schedario": tmp_path}
    assert agent.compatta_cronologia(msgs, **kw)
    for i in range(14, 28):
        msgs += scrittura(f"c{i}", f"new_{i}.py", "x" * 4000)
    assert agent.compatta_cronologia(msgs, **kw)
    assert "OLD_UNIQUE" in backend.chiamate[0][0][1]["content"]
    assert "OLD_UNIQUE" not in backend.chiamate[1][0][1]["content"]
    summaries = [m for m in msgs if m["role"] == "summary"]
    assert "NON toccare legacy" in summaries[-1]["content"]
    assert summaries[-1]["source_start"] > summaries[0]["source_start"]
    assert (tmp_path / summaries[-1]["archive_path"]).is_file()
    assert agent.compatta_cronologia(msgs, **kw) is None


def test_missing_archive_preserves_previous_summary(tmp_path):
    msgs = cronologia(14)
    backend = FintoBackend(testo="SCOPERTO: IMPORTANT_PREVIOUS_FACT")
    kw = {"backend": backend, "params": GenParams(num_ctx=8192), "budgets": budgets_for(8192),
          "strip_thinking": True, "schedario": tmp_path}
    assert agent.compatta_cronologia(msgs, **kw)
    previous = next(m for m in msgs if m["role"] == "summary")
    (tmp_path / previous["archive_path"]).unlink()
    for i in range(14, 28):
        msgs += scrittura(f"c{i}", f"new_{i}.py", "x" * 4000)
    assert agent.compatta_cronologia(msgs, **kw)
    assert "IMPORTANT_PREVIOUS_FACT" in backend.chiamate[1][0][1]["content"]


class ScriptBackend:
    def __init__(self, script):
        self.script = iter(script)
        self.params = []

    def stream(self, messages, tools, params):
        self.params.append(params)
        yield from next(self.script)


def call(name, args, ident):
    return StreamEvent("tool_call", tool_call={
        "id": ident, "name": name, "arguments": json.dumps(args),
    })


def run(backend, ctx, **kwargs):
    msgs = [{"role": "user", "content": "esegui il lavoro"}]
    events = list(agent.run_turn(
        backend=backend, params=kwargs.pop("params", GenParams(think="high")),
        tools_schema=TOOLS_SCHEMA, tool_ctx=ctx, ui_messages=msgs,
        system_prompt="SYS", env_header=None, require_summary=False, require_plan=False,
        enable_nudge=False, estratto_pensiero=False, plan_gate=False,
        compact_history=False, **kwargs,
    ))
    return msgs, events


def test_new_plan_item_restores_reasoning_budget(tmp_path):
    plan = Plan()
    plan.set_steps(["primo", "secondo"])
    plan.start("1")
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host", plan=plan)
    backend = ScriptBackend([
        [call("list_files", {}, "a")],
        [call("manage_plan", {"action": "complete", "step_id": "1", "note": "ispezionato"}, "b")],
        [StreamEvent("content", text="terminato")],
    ])
    _, events = run(backend, ctx, max_steps=3, think_watchdog=False)
    assert [p.think for p in backend.params] == ["high", "medium", "high"]
    telemetry = next(e.telemetry for e in events if isinstance(e, agent.TurnFinished))
    assert telemetry["totals"]["calls"] == 3


def test_failed_tool_restores_reasoning_budget(tmp_path):
    plan = Plan()
    plan.set_steps(["primo"])
    plan.start("1")
    backend = ScriptBackend([
        [call("list_files", {}, "a")],
        [call("read_file", {"filepath": "missing.py"}, "b")],
        [StreamEvent("content", text="file assente")],
    ])
    run(backend, ToolContext(workspace=str(tmp_path), sandbox="host", plan=plan),
        max_steps=3, think_watchdog=False)
    assert [p.think for p in backend.params] == ["high", "medium", "high"]


def test_watchdog_after_exhausted_retries_never_reports_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda _: None)
    backend = ScriptBackend([
        [StreamEvent("error", text="connection reset")],
        [StreamEvent("error", text="connection reset")],
        [StreamEvent("reasoning", text="ragionamento " * 3000)],
    ])
    _, events = run(backend, ToolContext(workspace=str(tmp_path), sandbox="host"),
                    max_steps=6, params=GenParams(max_tokens=1000, think="high"))
    finished = next(e for e in events if isinstance(e, agent.TurnFinished))
    assert finished.reason == "reasoning_budget"
    assert finished.telemetry["totals"]["calls"] == 3


def test_written_versions_have_different_hashes(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), sandbox="host")
    first = json.loads(dispatch(ctx, "write_file", {
        "filepath": "a.txt", "content": "before\r\nseconda riga: è\n",
    }))
    assert first["sha256"] == hashlib.sha256((tmp_path / "a.txt").read_bytes()).hexdigest()
    second = json.loads(dispatch(ctx, "edit_file", {
        "filepath": "a.txt", "old_string": "before", "new_string": "after",
    }))
    assert len(first["sha256"]) == 64
    assert first["sha256"] != second["sha256"]
    assert second["sha256"] == hashlib.sha256((tmp_path / "a.txt").read_bytes()).hexdigest()
