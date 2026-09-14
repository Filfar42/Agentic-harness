"""Offline telemetry contracts: no content, complete totals, bounded details."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core import telemetry
from core.backend import StreamEvent
from core.config import GenParams
from core.textutils import estimate_messages_tokens, estimate_tokens


class ScriptBackend:
    supports_cancellation = True

    def __init__(self, events=()):
        self.events = events
        self.received = []
        self.closed = False

    def stream(self, messages, tools, params, **kwargs):
        self.received.append((messages, tools, params, kwargs))
        try:
            yield from self.events
        finally:
            self.closed = True


def test_transparent_idempotent_shared_scopes():
    backend = ScriptBackend([StreamEvent("content", text="ok")])
    wrapped = telemetry.track_backend(backend)
    assert telemetry.track_backend(wrapped) is wrapped
    assert wrapped.supports_cancellation is True
    auxiliary = wrapped.scope("compaction")
    assert telemetry.track_backend(auxiliary) is auxiliary
    assert auxiliary.collector is wrapped.collector
    params = GenParams()
    messages, schema, stop = [], [], lambda: False
    events = list(auxiliary.stream(messages, schema, params, should_stop=stop))
    assert events == backend.events
    assert backend.received == [(messages, schema, params, {"should_stop": stop})]
    assert backend.closed
    result = wrapped.collector.snapshot()
    assert result["totals"]["calls"] == 1
    assert result["calls"][0]["purpose"] == "compaction"
    assert result["totals"]["by_purpose"]["compaction"]["calls"] == 1


def test_timing_estimates_and_private_data(monkeypatch):
    ticks = iter((10.0, 10.125, 10.5))
    monkeypatch.setattr(telemetry.time, "perf_counter", lambda: next(ticks))
    backend = ScriptBackend([
        StreamEvent("reasoning", text="private reasoning"),
        StreamEvent("content", text="private answer"),
        StreamEvent("usage", usage={"prompt_tokens": 50, "completion_tokens": 7,
                                    "done_reason": "stop", "private_error": "SECRET"}),
    ])
    backend.api_key = "PRIVATE_KEY"
    backend.base_url = "https://private:password@host"
    backend.reasoning_control = lambda params: {
        "requested": params.think, "support": "llama.cpp", "verified": False,
        "api_key": "PRIVATE_KEY",
        "payload": {"reasoning_effort": "low", "api_key": "PRIVATE_KEY",
                    "chat_template_kwargs": {"enable_thinking": True, "prompt": "PRIVATE"}},
    }
    params = SimpleNamespace(model="fake", think="low", api_key="PRIVATE_KEY")
    messages = [{"role": "user", "content": "PRIVATE_PROMPT"}]
    schema = [{"name": "private_tool_schema"}]
    wrapped = telemetry.track_backend(backend)
    list(wrapped.stream(messages, schema, params))
    call = wrapped.collector.snapshot()["calls"][0]
    assert call["first_output_ms"] == 125
    assert call["wall_time_ms"] == 500
    assert call["input_tokens_estimated"] == (
        estimate_messages_tokens(messages) + estimate_tokens(json.dumps(schema))
    )
    assert call["output_tokens_estimated"] == estimate_tokens("private reasoningprivate answer")
    assert call["reasoning_control"]["payload"] == {
        "reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": True},
    }
    serialized = json.dumps(call)
    for private in ("PRIVATE", "password", "private reasoning", "private answer", "private_tool_schema"):
        assert private not in serialized


@pytest.mark.parametrize("usage,expected", [
    ({"done_reason": "stop"}, (None, None, None, 0, 1)),
    ({"prompt_tokens": 0}, (0, None, None, 1, 0)),
    ({"prompt_tokens": 10, "completion_tokens": 3}, (10, 3, 13, 1, 0)),
])
def test_missing_usage_is_not_zero(usage, expected):
    wrapped = telemetry.track_backend(ScriptBackend([StreamEvent("usage", usage=usage)]))
    list(wrapped.stream([], None, GenParams()))
    result = wrapped.collector.totals()
    assert tuple(result[key] for key in (
        "usage_input_tokens", "usage_output_tokens", "usage_total_tokens",
        "usage_reported_calls", "usage_missing_calls",
    )) == expected
    assert result["prompt_tokens"] == expected[0]
    assert result["completion_tokens"] == expected[1]


def test_repeated_usage_is_cumulative_and_legacy_timings_survive():
    backend = ScriptBackend([
        StreamEvent("usage", usage={"prompt_tokens": 100, "completion_tokens": 1}),
        StreamEvent("usage", usage={"prompt_tokens": 100, "completion_tokens": 2,
                                    "prompt_eval_ms": 250, "draft_n": 12, "draft_accepted": 10}),
    ])
    wrapped = telemetry.track_backend(backend)
    list(wrapped.stream([], None, GenParams()))
    result = wrapped.collector.totals()
    assert result["prompt_tokens"] == 100
    assert result["completion_tokens"] == 2
    assert result["prompt_eval_ms"] == 250
    assert result["draft_n"] == 12
    assert result["draft_accepted"] == 10
    assert result["eval_ms"] is None


def test_reasoning_control_preserves_initial_and_applied_after_backend_fallback():
    backend = ScriptBackend([StreamEvent("content", text="answer")])
    backend.reasoning_control = lambda params: {
        "requested": params.think, "payload": {"think": True if backend.closed else params.think},
        "support": "boolean" if backend.closed else "level", "verified": False,
    }
    wrapped = telemetry.track_backend(backend)
    list(wrapped.stream([], None, GenParams(think="high")))
    call = wrapped.collector.snapshot()["calls"][0]
    assert call["reasoning_control_initial"]["payload"] == {"think": "high"}
    assert call["reasoning_control"]["payload"] == {"think": True}
    assert call["reasoning_control"]["requested"] == "high"


def test_invalid_usage_values_do_not_break_generation():
    backend = ScriptBackend([StreamEvent("usage", usage={
        "prompt_tokens": float("nan"), "completion_tokens": -2, "total_tokens": 10**400,
        "prompt_eval_ms": True,
    })])
    wrapped = telemetry.track_backend(backend)
    list(wrapped.stream([], None, GenParams()))
    assert wrapped.collector.totals()["usage_missing_calls"] == 1


def test_configuration_preserves_valid_negative_sampling_values():
    wrapped = telemetry.track_backend(ScriptBackend([]))
    list(wrapped.stream([], None, GenParams(presence_penalty=-0.5, seed=-1)))
    config = wrapped.collector.snapshot()["calls"][0]["config"]
    assert config["presence_penalty"] == -0.5
    assert config["seed"] == -1


def test_closed_stream_records_interruption_and_closes_backend():
    backend = ScriptBackend([StreamEvent("content", text="first"), StreamEvent("content", text="second")])
    wrapped = telemetry.track_backend(backend)
    stream = wrapped.stream([], None, GenParams())
    next(stream)
    stream.close()
    assert backend.closed
    assert wrapped.collector.totals()["interrupted"] == 1


def test_stream_error_event_and_exception_are_recorded_without_error_text():
    backend = ScriptBackend([StreamEvent("error", text="private error detail")])
    wrapped = telemetry.track_backend(backend)
    list(wrapped.stream([], None, GenParams()))
    assert wrapped.collector.totals()["error"] == 1
    assert "private error" not in json.dumps(wrapped.collector.snapshot())

    def broken(*args, **kwargs):
        raise RuntimeError("private raised error")
    backend.stream = broken
    with pytest.raises(RuntimeError, match="private raised error"):
        list(wrapped.stream([], None, GenParams()))
    assert wrapped.collector.totals()["error"] == 2
    assert "private raised error" not in json.dumps(wrapped.collector.snapshot())


def test_cancelled_backend_is_interrupted_even_if_it_returns_an_error():
    wrapped = telemetry.track_backend(ScriptBackend([StreamEvent("error", text="cancelled")]))
    list(wrapped.stream([], None, GenParams(), should_stop=lambda: True))
    result = wrapped.collector.totals()
    assert result["interrupted"] == 1
    assert result["error"] == 0


def test_exact_offset_totals_outlive_detail_eviction_and_snapshot_is_detached():
    collector = telemetry.TelemetryCollector(max_calls=2)
    backend = ScriptBackend([StreamEvent("usage", usage={"prompt_tokens": 5, "completion_tokens": 2})])
    wrapped = telemetry.TrackedBackend(backend, collector)
    list(wrapped.stream([], None, GenParams()))
    offset = collector.offset()
    for _ in range(10):
        list(wrapped.scope("delegation").stream([], None, GenParams()))
    snapshot = collector.snapshot(since=offset)
    assert snapshot["totals"]["calls"] == 10
    assert snapshot["totals"]["prompt_tokens"] == 50
    assert snapshot["totals"]["usage_total_tokens"] == 70
    assert snapshot["calls_truncated"] == 8
    assert len(snapshot["calls"]) == 2
    assert snapshot["since_offset"] == 1 and snapshot["end_offset"] == 11
    snapshot["calls"][0]["usage"]["prompt_tokens"] = 99999
    assert collector.snapshot()["calls"][0]["usage"]["prompt_tokens"] == 5
    assert collector.totals(since=collector.offset())["prompt_tokens"] is None
    with pytest.raises(ValueError, match="another collector"):
        collector.totals(since=telemetry.TelemetryCollector().offset())


def test_parallel_scopes_share_counts_without_cross_contamination():
    wrapped = telemetry.track_backend(ScriptBackend([StreamEvent("content", text="ok")]))
    offset = wrapped.collector.offset()
    def generate(index):
        return list(wrapped.scope(f"job_{index % 2}").stream([], None, GenParams()))
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(generate, range(20)))
    result = wrapped.collector.totals(since=offset)
    assert result["calls"] == 20
    assert result["by_purpose"]["job_0"]["calls"] == 10
    assert result["by_purpose"]["job_1"]["calls"] == 10
