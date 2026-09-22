"""Bounded, content-free accounting shared by main and auxiliary model calls.

Offsets capture cumulative counters, so totals remain exact even after old call
details have been evicted. They are opaque in-process tokens: obtain one before
a turn and pass it back unchanged. Wall time is the sum of call durations (which
can overlap), not the elapsed time of the entire agent turn. Token estimates are
kept separate from provider usage; unavailable counters are never invented.
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .textutils import _CHARS_PER_TOKEN, estimate_messages_tokens, estimate_tokens

MAX_CALL_DETAILS = 512
MAX_PURPOSES = 32
_CONFIG_FIELDS = (
    "temperature", "top_p", "top_k", "presence_penalty", "repetition_penalty",
    "max_tokens", "num_ctx", "num_gpu", "think", "seed",
)
_USAGE_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
    "reasoning_tokens", "prompt_eval_ms", "eval_ms", "total_ms", "load_ms",
    "draft_n", "draft_accepted", "prompt_processed_tokens",
)

_NUMERIC_TOTALS = (
    "calls", "completed", "error", "interrupted", "input_tokens_estimated",
    "schema_tokens_estimated", "output_tokens_estimated", "wall_time_ms",
    "usage_reported_calls", "usage_missing_calls", "usage_input_tokens",
    "usage_output_tokens", "usage_total_tokens", "usage_input_reported_calls",
    "usage_output_reported_calls", "usage_total_reported_calls", *_USAGE_FIELDS,
    *(f"{key}_reported_calls" for key in _USAGE_FIELDS),
)


def _number(value: Any) -> bool:
    if type(value) is int:
        return 0 <= value <= 2**53
    return type(value) is float and math.isfinite(value) and 0 <= value <= 2**53


def _scalars(value: Any, *, max_fields: int = 24) -> dict[str, Any]:
    """Keep only small diagnostic values, never objects or arbitrary content."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:max_fields]:
        if not isinstance(key, str) or len(key) > 64:
            continue
        if item is None or type(item) is bool or (type(item) in (int, float) and _number(abs(item))):
            result[key] = item
        elif isinstance(item, str):
            result[key] = item[:128]
    return result


def _usage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result = {key: raw[key] for key in _USAGE_FIELDS if _number(raw.get(key))}
    if isinstance(raw.get("done_reason"), str):
        result["done_reason"] = raw["done_reason"][:64]
    return result


def _reasoning_control(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result = _scalars({key: raw[key] for key in ("requested", "support", "verified", "status")
                       if key in raw})
    payload = raw.get("payload")
    if isinstance(payload, dict):
        result["payload"] = _scalars({key: payload[key] for key in ("reasoning_effort", "think")
                                      if key in payload})
        template = payload.get("chat_template_kwargs")
        if isinstance(template, dict):
            result["payload"]["chat_template_kwargs"] = _scalars({
                key: template[key] for key in ("enable_thinking", "thinking", "reasoning_effort")
                if key in template
            })
    return result


def _empty_totals() -> dict[str, int | float]:
    return dict.fromkeys(_NUMERIC_TOTALS, 0)


def _call_totals(call: dict[str, Any]) -> dict[str, int | float]:
    result = _empty_totals()
    result["calls"] = 1
    result[call["outcome"]] = 1
    for key in ("input_tokens_estimated", "schema_tokens_estimated",
                "output_tokens_estimated", "wall_time_ms"):
        result[key] = call[key]
    usage = call["usage"]
    for field in _USAGE_FIELDS:
        if field in usage:
            result[field] = usage[field]
            result[f"{field}_reported_calls"] = 1
    present = "prompt_tokens" in usage or "completion_tokens" in usage or "total_tokens" in usage
    result["usage_reported_calls"] = int(present)
    result["usage_missing_calls"] = int(not present)
    for source, target in (("prompt_tokens", "input"), ("completion_tokens", "output")):
        if source in usage:
            result[f"usage_{target}_tokens"] = usage[source]
            result[f"usage_{target}_reported_calls"] = 1
    total = usage.get("total_tokens")
    if total is None and "prompt_tokens" in usage and "completion_tokens" in usage:
        total = usage["prompt_tokens"] + usage["completion_tokens"]
    if total is not None:
        result["usage_total_tokens"] = total
        result["usage_total_reported_calls"] = 1
    return result


def _subtract(current: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    result = {key: current[key] - before.get(key, 0) for key in _NUMERIC_TOTALS}
    result["wall_time_ms"] = round(result["wall_time_ms"], 3)
    for part in ("input", "output", "total"):
        if not result[f"usage_{part}_reported_calls"]:
            result[f"usage_{part}_tokens"] = None
    for field in _USAGE_FIELDS:
        if not result[f"{field}_reported_calls"]:
            result[field] = None
    return result


@dataclass(frozen=True, slots=True)
class TelemetryOffset:
    """Opaque checkpoint, independent from the bounded detail buffer."""

    owner: object
    sequence: int
    counters: dict[str, Any]
    purposes: dict[str, dict[str, Any]]


class TelemetryCollector:
    def __init__(self, *, max_calls: int = MAX_CALL_DETAILS) -> None:
        self._lock = threading.RLock()
        self._owner = object()
        self._sequence = 0
        self._calls: deque[dict[str, Any]] = deque(maxlen=max(1, min(max_calls, MAX_CALL_DETAILS)))
        self._counters = _empty_totals()
        self._purposes: dict[str, dict[str, Any]] = {}

    def offset(self) -> TelemetryOffset:
        with self._lock:
            return TelemetryOffset(self._owner, self._sequence, dict(self._counters),
                                   copy.deepcopy(self._purposes))

    def _before(self, since: TelemetryOffset | None) -> TelemetryOffset:
        if since is None:
            return TelemetryOffset(self._owner, 0, _empty_totals(), {})
        if not isinstance(since, TelemetryOffset) or since.owner is not self._owner:
            raise ValueError("Telemetry offset belongs to another collector")
        return since

    def record(self, call: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            call = {**call, "sequence": self._sequence}
            self._calls.append(call)
            delta = _call_totals(call)
            purpose = call["purpose"]
            if purpose not in self._purposes and len(self._purposes) >= MAX_PURPOSES - 1:
                purpose = "other"
            bucket = self._purposes.setdefault(purpose, _empty_totals())
            for key, value in delta.items():
                self._counters[key] += value
                bucket[key] += value

    def totals(self, *, since: TelemetryOffset | None = None) -> dict[str, Any]:
        with self._lock:
            before = self._before(since)
            totals = _subtract(self._counters, before.counters)
            totals["by_purpose"] = {
                purpose: delta for purpose, values in self._purposes.items()
                if (delta := _subtract(values, before.purposes.get(purpose, {})))["calls"]
            }
            return totals

    def snapshot(self, *, since: TelemetryOffset | None = None) -> dict[str, Any]:
        with self._lock:
            before = self._before(since)
            calls = [copy.deepcopy(call) for call in self._calls if call["sequence"] > before.sequence]
            totals = self.totals(since=since)
            return {
                "version": 1,
                "since_offset": before.sequence,
                "end_offset": self._sequence,
                "calls": calls,
                "calls_truncated": totals["calls"] - len(calls),
                "totals": totals,
            }


class TrackedBackend:
    """Forward the backend API, sharing one collector across purpose scopes."""

    def __init__(self, backend: Any, collector: TelemetryCollector | None = None,
                 purpose: str = "main") -> None:
        self._backend = backend
        self.collector = collector if collector is not None else TelemetryCollector()
        self._purpose = str(purpose or "main")[:64]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def scope(self, purpose: str) -> TrackedBackend:
        return TrackedBackend(self._backend, self.collector, purpose)

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
               params: Any, **kwargs: Any) -> Iterator[Any]:
        start = time.perf_counter()
        schema_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
        config = _scalars({key: getattr(params, key, None) for key in _CONFIG_FIELDS})
        control = getattr(self._backend, "reasoning_control", None)
        reasoning: dict[str, Any] = {}
        if callable(control):
            try:
                reasoning = _reasoning_control(control(params))
            except Exception:  # noqa: BLE001 - diagnostics must not prevent a model call
                reasoning = {"status": "unavailable"}
        call: dict[str, Any] = {
            "purpose": self._purpose,
            "backend": type(self._backend).__name__,
            "model": str(getattr(params, "model", ""))[:256],
            "config": config,
            "reasoning_control": reasoning,
            "reasoning_control_initial": reasoning,
            "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "input_tokens_estimated": estimate_messages_tokens(messages) + schema_tokens,
            "schema_tokens_estimated": schema_tokens,
            "output_tokens_estimated": 0,
            "first_output_ms": None,
            "wall_time_ms": 0,
            "outcome": "interrupted",
            "usage": {},
        }
        output_chars = 0
        stream = None
        slot_per = getattr(self._backend, "slot_per", None)
        if getattr(self._backend, "supports_id_slot", False) is True and callable(slot_per):
            slot = slot_per(self._purpose)
            if slot is not None:
                kwargs["id_slot"] = slot
                call["config"]["id_slot"] = slot
        try:
            stream = self._backend.stream(messages, tools, params, **kwargs)

            for event in stream:
                kind = getattr(event, "kind", "")
                if kind in ("content", "reasoning", "tool_call"):
                    text = getattr(event, "text", "")
                    tool_call = getattr(event, "tool_call", None)
                    has_output = bool(text or tool_call)
                    if has_output and call["first_output_ms"] is None:
                        call["first_output_ms"] = round((time.perf_counter() - start) * 1000, 3)
                    output_chars += len(text) if isinstance(text, str) else 0
                    if tool_call:
                        output_chars += len(json.dumps(tool_call, ensure_ascii=False))
                elif kind == "usage":
                    # Stream usage is cumulative: replace counters, do not sum
                    # repeated snapshots from the same generation.
                    call["usage"].update(_usage(getattr(event, "usage", None)))
                elif kind == "error":
                    call["outcome"] = "error"
                yield event
            if call["outcome"] != "error":
                call["outcome"] = "completed"
        except GeneratorExit:
            # Consumers close immediately after an error event. That cleanup
            # must not turn an observed backend failure into a user stop.
            if call["outcome"] != "error":
                call["outcome"] = "interrupted"
            raise
        except KeyboardInterrupt:
            call["outcome"] = "interrupted"
            raise
        except BaseException:
            call["outcome"] = "error"
            raise
        finally:
            try:
                if stream is not None and callable(getattr(stream, "close", None)):
                    stream.close()
            except BaseException:
                call["outcome"] = "error"
                raise
            finally:
                if callable(control):
                    try:
                        call["reasoning_control"] = _reasoning_control(control(params))
                    except Exception:  # noqa: BLE001 - preserve initial diagnostics on failure
                        call["reasoning_control"] = {"status": "unavailable"}
                stop = kwargs.get("should_stop")
                if callable(stop):
                    try:
                        if stop():
                            call["outcome"] = "interrupted"
                    except Exception:  # noqa: BLE001 - don't replace the original stream error
                        pass
                call["wall_time_ms"] = round((time.perf_counter() - start) * 1000, 3)
                call["output_tokens_estimated"] = (
                    int(output_chars / _CHARS_PER_TOKEN) + 1 if output_chars else 0
                )
                self.collector.record(call)


def track_backend(backend: Any) -> TrackedBackend:
    """Wrap once; scope wrappers already share their parent's collector."""
    return backend if isinstance(backend, TrackedBackend) else TrackedBackend(backend)
