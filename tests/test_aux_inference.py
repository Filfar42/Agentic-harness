"""Service summaries never commit partial output and stay cheap on large contexts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import compaction, delega, pensiero
from core.backend import StreamEvent
from core.config import GenParams
from core.inference import service_text
from core.telemetry import track_backend
from core.textutils import estimate_tokens


class ControlledBackend:
    supports_cancellation = True

    def __init__(self, ending="stop"):
        self.ending = ending
        self.calls = []
        self.closed = False
        self.stopped = False

    def stream(self, messages, tools, params, *, should_stop=None):
        self.calls.append((messages, tools, params, should_stop))
        try:
            yield StreamEvent("content", text="SCOPERTO: prima parte")
            if self.ending == "raise":
                raise RuntimeError("private server detail")
            if self.ending == "cancel":
                self.stopped = True
                yield StreamEvent("content", text=" coda non valida")
            elif self.ending == "error":
                yield StreamEvent("error", text="private server detail")
            elif self.ending == "tool_call":
                yield StreamEvent("tool_call", tool_call={"name": "read_file"})
            else:
                yield StreamEvent("usage", usage={"done_reason": self.ending,
                                                  "prompt_tokens": 12, "completion_tokens": 5})
        finally:
            self.closed = True
            if self.ending == "close_cancel":
                self.stopped = True
            if self.ending == "close_raise":
                raise RuntimeError("private close detail")


def invoke_service(which, backend, *, params=None, should_stop=None, text=None):
    params = params or GenParams()
    if which == "pensiero":
        return pensiero.estrai(
            [text or "ragionamento verificato " * 200], punto="1", backend=backend,
            params=params, should_stop=should_stop,
        )
    if which == "compaction":
        return compaction.costruisci_riassunto(
            text or "[AGENTE] lavoro svolto", backend=backend,
            params=params, should_stop=should_stop,
        )
    return delega.referto_di_chiusura(
        [{"role": "user", "content": "dove si trova?"}], backend=backend, params=params,
        build_messages=lambda messages, **kwargs: [
            {"role": "system", "content": kwargs["system_prompt"]}, *messages,
        ], budgets=None, should_stop=should_stop,
    )


@pytest.mark.parametrize("which", ["pensiero", "compaction", "delega"])
@pytest.mark.parametrize("ending", ["error", "raise", "length", "max_tokens", "content_filter",
                                     "tool_call", "cancel", "cancelled", "interrupted",
                                     "close_cancel", "close_raise"])
def test_service_callers_discard_partial_results_and_close(which, ending):
    backend = ControlledBackend(ending)

    def stop():
        return backend.stopped

    assert invoke_service(which, backend, should_stop=stop) == ""
    assert backend.closed
    assert backend.calls[0][3] is stop
    assert backend.calls[0][1] is None and backend.calls[0][2].think is False


@pytest.mark.parametrize("which", ["pensiero", "compaction", "delega"])
def test_already_cancelled_services_never_start_generation(which):
    backend = ControlledBackend()
    assert invoke_service(which, backend, should_stop=lambda: True) == ""
    assert backend.calls == []


@pytest.mark.parametrize("which", ["pensiero", "compaction", "delega"])
def test_completed_service_results_survive_and_streams_close(which):
    backend = ControlledBackend()
    assert invoke_service(which, backend) == "SCOPERTO: prima parte"
    assert backend.closed


def test_legacy_backend_without_should_stop_remains_compatible():
    class Legacy:
        def stream(self, messages, tools, params):
            yield StreamEvent("content", text="<think>hidden</think> completo ")

    assert service_text(Legacy(), [], GenParams(), should_stop=lambda: False) == "completo"


def test_cancellation_between_legacy_chunks_still_closes_stream():
    state = {"stop": False, "closed": False}

    class Legacy:
        def stream(self, messages, tools, params):
            try:
                yield StreamEvent("content", text="parziale")
                state["stop"] = True
                yield StreamEvent("content", text=" scartato")
                pytest.fail("service consumed output after cancellation")
            finally:
                state["closed"] = True

    assert service_text(Legacy(), [], GenParams(), should_stop=lambda: state["stop"]) == ""
    assert state["closed"]


@pytest.mark.parametrize("which, ceiling, fraction", [("pensiero", 4096, 0.25),
                                                       ("compaction", 8192, 0.5)])
@pytest.mark.parametrize("window", [0, 4096, 8192, 131072, 1048576])
def test_service_input_respects_both_fractional_and_absolute_budgets(which, ceiling, fraction, window):
    backend = ControlledBackend()
    source = "prima evidenza\n" + ("lavoro intermedio\n" * 10000) + "ultima evidenza"
    invoke_service(which, backend, params=GenParams(num_ctx=window), text=source)
    messages = backend.calls[0][0]
    budget = min(ceiling, window * fraction) if window > 0 else ceiling
    assert estimate_tokens(messages[1]["content"]) <= budget
    assert "prima evidenza" in messages[1]["content"]
    assert "omessi" in messages[1]["content"]


def test_service_usage_is_recorded_by_the_original_telemetry_wrapper():
    wrapped = track_backend(ControlledBackend()).scope("compaction")
    assert invoke_service("compaction", wrapped) == "SCOPERTO: prima parte"
    call = wrapped.collector.snapshot()["calls"][-1]
    assert call["purpose"] == "compaction"
    assert call["usage"]["prompt_tokens"] == 12
    assert call["usage"]["completion_tokens"] == 5


def test_delegate_forwards_cancellation_to_its_closing_report(tmp_path):
    from core.agent import StepStarted
    from core.tools import ToolContext

    backend = ControlledBackend("cancel")

    def stop():
        return backend.stopped

    def child(**kwargs):
        kwargs["ui_messages"].append(
            {"role": "tool", "name": "read_file", "args": {"filepath": "core/config.py"}}
        )
        yield StepStarted(step=1, total=1)

    result = delega.esegui(
        "Trova la funzione", backend=backend, params=GenParams(), tools_schema=[],
        tool_ctx=ToolContext(workspace=str(tmp_path), sandbox="host"), env_header=None,
        run_turn=child, build_messages=lambda messages, **kwargs: [
            {"role": "system", "content": kwargs["system_prompt"]}, *messages,
        ], should_stop=stop, registra_esiti=False,
    )
    assert "referto" not in result
    assert backend.calls[0][3] is stop
    assert backend.closed
