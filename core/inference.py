"""Small service generations: publish only a completed, uncancelled result."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .textutils import strip_think


def service_text(
    backend: Any,
    messages: list[dict[str, Any]],
    params: Any,
    *,
    should_stop: Callable[[], bool] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Collect a tool-free generation, discarding partial or failed output.

    Capability-gated forwarding keeps legacy backends compatible. Regardless
    of native support, cancellation is checked before and between events and
    after closing the stream. Passing a telemetry wrapper through unchanged
    preserves its usage collection and close/finalization hooks.

    ``tools`` si passa solo quando la richiesta **continua** una conversazione
    (la memoria del progetto a fine turno): gli schemi fanno parte del
    prefisso che il template rende, e toglierli invaliderebbe la cache dal
    primo token. La risposta resta senza tool: una tool call invalida l'esito.
    """
    stream = None
    pieces: list[str] = []
    failed = False
    try:
        if should_stop is not None and should_stop():
            return ""
        kwargs = {}
        if should_stop is not None and getattr(backend, "supports_cancellation", False):
            kwargs["should_stop"] = should_stop
        stream = backend.stream(messages, tools or None, params, **kwargs)
        for event in stream:
            if should_stop is not None and should_stop():
                failed = True
                break
            if event.kind == "content":
                pieces.append(event.text)
            elif event.kind in {"error", "tool_call"}:
                failed = True
                break
            elif event.kind == "usage":
                usage = getattr(event, "usage", None) or {}
                if usage.get("done_reason", usage.get("finish_reason")) in {
                    "length", "max_tokens", "content_filter", "error",
                    "cancelled", "canceled", "interrupted", "abort", "aborted",
                }:
                    failed = True
                    break
    except Exception:  # noqa: BLE001 - a failed service must not break the main task
        failed = True
    finally:
        try:
            if stream is not None and callable(getattr(stream, "close", None)):
                stream.close()
        except Exception:  # noqa: BLE001 - close failures also invalidate the partial result
            failed = True
    try:
        if failed or (should_stop is not None and should_stop()):
            return ""
        return strip_think("".join(pieces)).strip()
    except Exception:  # noqa: BLE001 - malformed service output cannot become durable state
        return ""
