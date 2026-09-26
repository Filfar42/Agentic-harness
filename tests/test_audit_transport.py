"""Adversarial transport tests: no tool action from a partial response."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from core import backend as transport
from core.backend import OllamaBackend, OpenAICompatBackend, StreamEvent, to_ollama_messages
from core.config import GenParams
from core.jsonsafe import JsonBoundaryError


class Wire(httpx.SyncByteStream):
    """A controllable wire that records resource release and can drop mid-stream."""

    def __init__(self, blocks: list[bytes | Exception]) -> None:
        self.blocks = blocks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for block in self.blocks:
            if isinstance(block, Exception):
                raise block
            yield block

    def close(self) -> None:
        self.closed = True


def ndjson(*chunks: Any) -> bytes:
    return b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)


def sse(*chunks: Any, done: bool = True) -> bytes:
    data = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
    return data + (b"data: [DONE]\n\n" if done else b"")


def choice(delta: dict[str, Any] | None = None, finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


OLLAMA_CALL = {"message": {"tool_calls": [{"function": {
    "name": "read_file", "arguments": {"filepath": "a.txt"},
}}]}}
OPENAI_CALL = choice({"tool_calls": [{"index": 0, "id": "call_a", "function": {
    "name": "read_file", "arguments": '{"filepath":"a.txt"}',
}}]})


@pytest.fixture()
def wire_client(monkeypatch):
    clients: list[httpx.Client] = []

    def install(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        monkeypatch.setattr(transport, "get_client", lambda: client)
        return client

    yield install
    for client in clients:
        client.close()


def backend(kind: str):
    if kind == "ollama":
        return OllamaBackend("http://local", timeout_s=2, stream_with_tools=True)
    return OpenAICompatBackend("http://local", "test-key", timeout_s=2)


def run(kind: str, **kwargs) -> list[StreamEvent]:
    return list(backend(kind).stream([], None, GenParams(model="test"), **kwargs))


def output(events: list[StreamEvent]) -> list[StreamEvent]:
    """Gli eventi con un effetto: senza i battiti del cruscotto.

    Un battito dice solo che il modello sta scrivendo gli argomenti di una
    chiamata (quanti caratteri): non porta testo ne' tool, e le garanzie di
    questi test -- niente tool prima della fine, niente tool parziali -- sono
    sugli eventi che portano qualcosa.
    """
    for event in events:
        if event.kind == "battito":
            assert not event.text and event.tool_call is None
    return [event for event in events if event.kind != "battito"]


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_tool_is_withheld_until_explicit_completion_and_wire_closes(kind, wire_client):
    raw = (ndjson(OLLAMA_CALL, {"done": True, "done_reason": "stop"}) if kind == "ollama"
           else sse(OPENAI_CALL, choice(finish="tool_calls")))
    wire = Wire([raw[i:i + 3] for i in range(0, len(raw), 3)])
    wire_client(lambda request: httpx.Response(200, stream=wire))
    events = output(run(kind))
    assert [event.kind for event in events] == ["usage", "tool_call"]
    assert events[-1].tool_call["name"] == "read_file"
    assert wire.closed


@pytest.mark.parametrize("raw,kind", [
    (ndjson(OLLAMA_CALL), "ollama"),
    (sse(OPENAI_CALL, done=False), "openai"),
    (sse(OPENAI_CALL), "openai"),
    (sse(OPENAI_CALL, choice(finish="tool_calls"), done=False), "openai"),
    (b'data: {"choices": []}', "openai"),
])
def test_eof_and_missing_terminal_signals_never_release_tools(raw, kind, wire_client):
    wire = Wire([raw])
    wire_client(lambda request: httpx.Response(200, stream=wire))
    events = run(kind)
    assert events[-1].kind == "error"
    assert not any(event.kind == "tool_call" for event in events)
    assert wire.closed


@pytest.mark.parametrize("bad", [b'{"message":', b"[]", b"null", b'{"done":true,"done":false}',
                                   b'{"eval_count":NaN}', b"```json\n{}\n```", b"\xff"])
@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_malformed_protocol_json_is_not_skipped(kind, bad, wire_client):
    raw = bad + b"\n" if kind == "ollama" else b"data: " + bad + b"\n\n"
    wire_client(lambda request: httpx.Response(200, content=raw))
    events = run(kind)
    assert len(events) == 1 and events[0].kind == "error"


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_retry_transient_rejection_before_output_then_succeed(kind, wire_client, monkeypatch):
    attempts: list[httpx.Request] = []
    waits: list[float] = []
    monkeypatch.setattr(transport, "_wait_retry", lambda delay, *_: waits.append(delay))
    monkeypatch.setattr(transport.random, "uniform", lambda lo, hi: hi)
    body = ndjson({"done": True}) if kind == "ollama" else sse(choice(finish="stop"))

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, content=b"busy")
        return httpx.Response(200, content=body)

    wire_client(handler)
    assert run(kind)[-1].kind == "usage"
    assert len(attempts) == 3 and waits == [1.0, 1.0]


@pytest.mark.parametrize("status,expected", [(401, 1), (400, 1), (503, 3), (429, 3)])
def test_retry_is_bounded_and_auth_failures_are_not_replayed(status, expected, wire_client, monkeypatch):
    attempts: list[httpx.Request] = []
    monkeypatch.setattr(transport, "_wait_retry", lambda *_: None)

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, content=b"failure")

    wire_client(handler)
    events = run("openai")
    assert len(attempts) == expected
    assert len(events) == 1 and events[0].kind == "error"


def test_retry_after_and_jitter_are_bounded(monkeypatch):
    ceilings: list[float] = []
    monkeypatch.setattr(transport.random, "uniform", lambda lo, hi: ceilings.append(hi) or hi)
    assert transport._retry_delay(0) == 0.25
    assert transport._retry_delay(1) == 0.5
    assert transport._retry_delay(20, "999999999") == 30.0
    assert transport._retry_delay(0, "not-a-date") == 0.25
    assert transport._retry_delay(0, "NaN") == 0.25
    assert transport._retry_delay(0, "Wed, 01 Jan 3000 00:00:00 GMT") == 30.0
    assert max(ceilings) <= transport.RETRY_CAP_S


def test_connect_error_can_retry_before_output(wire_client, monkeypatch):
    attempts = 0
    monkeypatch.setattr(transport, "_wait_retry", lambda *_: None)

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("socket hang-up")
        return httpx.Response(200, content=sse(choice(finish="stop")))

    wire_client(handler)
    assert run("openai")[-1].kind == "usage"
    assert attempts == 3


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_network_drop_after_visible_output_is_not_retried(kind, wire_client):
    attempts: list[httpx.Request] = []
    prefix = ndjson({"message": {"content": "hello"}}) if kind == "ollama" else sse(
        choice({"content": "hello"}), done=False)
    wire = Wire([prefix, httpx.ReadError("socket hang-up")])

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, stream=wire)

    wire_client(handler)
    assert [event.kind for event in run(kind)] == ["content", "error"]
    assert len(attempts) == 1 and wire.closed


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_cancel_before_connect_sends_no_request(kind, wire_client):
    requests: list[httpx.Request] = []
    wire_client(lambda request: requests.append(request) or httpx.Response(200))
    assert run(kind, should_stop=lambda: True)[-1].kind == "error"
    assert not requests


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_cancel_between_frames_closes_wire_and_discards_tool_buffer(kind, wire_client):
    raw = (ndjson({"message": {"content": "hello"}}, OLLAMA_CALL, {"done": True})
           if kind == "ollama" else sse(choice({"content": "hello"}), OPENAI_CALL,
                                        choice(finish="tool_calls")))
    wire = Wire([raw])
    wire_client(lambda request: httpx.Response(200, stream=wire))
    stopped = False
    stream = backend(kind).stream([], None, GenParams(model="test"), should_stop=lambda: stopped)
    assert next(stream).kind == "content"
    stopped = True
    assert [event.kind for event in stream] == ["error"]
    assert wire.closed


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_consumer_closing_generator_releases_response(kind, wire_client):
    raw = (ndjson({"message": {"content": "hello"}}, {"done": True}) if kind == "ollama"
           else sse(choice({"content": "hello"}), choice(finish="stop")))
    wire = Wire([raw])
    wire_client(lambda request: httpx.Response(200, stream=wire))
    stream = backend(kind).stream([], None, GenParams(model="test"))
    assert next(stream).kind == "content"
    stream.close()
    assert wire.closed


def test_cancellation_interrupts_retry_wait():
    with pytest.raises(transport.TransportCancelled):
        transport._wait_retry(10, lambda: True, float("inf"))


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_oversized_unterminated_frame_fails_without_unbounded_buffer(kind, wire_client, monkeypatch):
    monkeypatch.setattr(transport, "MAX_FRAME_BYTES", 64)
    wire = Wire([b"x" * 40, b"x" * 40])
    wire_client(lambda request: httpx.Response(200, stream=wire))
    assert run(kind)[-1].kind == "error"
    assert wire.closed


def test_sse_multiline_data_comments_and_split_crlf(wire_client):
    raw = b': comment\r\nevent: message\r\ndata: {"choices":\r\ndata: [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n'
    wire_client(lambda request: httpx.Response(200, stream=Wire([bytes([b]) for b in raw])))
    assert [event.kind for event in run("openai")] == ["usage"]


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_length_termination_never_releases_partial_tools(kind, wire_client):
    raw = (ndjson(OLLAMA_CALL, {"done": True, "done_reason": "length"}) if kind == "ollama"
           else sse(OPENAI_CALL, choice(finish="length")))
    wire_client(lambda request: httpx.Response(200, content=raw))
    events = output(run(kind))
    assert [event.kind for event in events] == ["usage"]
    assert events[0].usage["done_reason"] == "length"


@pytest.mark.parametrize("kind", ["ollama", "openai"])
def test_bad_usage_types_become_errors(kind, wire_client):
    raw = (ndjson({"done": True, "eval_duration": "nan"}) if kind == "ollama"
           else sse(choice(finish="stop"), {"usage": {"completion_tokens": []}}))
    wire_client(lambda request: httpx.Response(200, content=raw))
    assert run(kind)[-1].kind == "error"


def test_think_fallback_closes_first_response_before_second_request(wire_client):
    responses: list[Wire] = []
    payloads: list[dict[str, Any]] = []

    def handler(request):
        payloads.append(json.loads(request.content))
        if responses:
            assert responses[0].closed
            wire = Wire([ndjson({"done": True})])
            responses.append(wire)
            return httpx.Response(200, stream=wire)
        wire = Wire([b'unsupported value for "think"'])
        responses.append(wire)
        return httpx.Response(400, stream=wire)

    wire_client(handler)
    events = list(backend("ollama").stream([], None, GenParams(model="test", think="high")))
    assert events[-1].kind == "usage"
    assert [payload["think"] for payload in payloads] == ["high", True]
    assert all(response.closed for response in responses)


def test_blocking_ollama_still_requires_explicit_done(wire_client):
    wire_client(lambda request: httpx.Response(200, json=OLLAMA_CALL))
    events = list(backend("ollama")._blocking_chat([], None, GenParams(model="test")))
    assert events[-1].kind == "error"
    assert not any(event.kind == "tool_call" for event in events)


@pytest.mark.parametrize("arguments", ['{"path":1,}', '```json\n{}\n```', '[]',
                                        '{"path":1,"path":2}', '{"path":NaN}'])
def test_outbound_history_never_repairs_invalid_arguments(arguments):
    with pytest.raises(JsonBoundaryError):
        to_ollama_messages([{"role": "assistant", "tool_calls": [{
            "function": {"name": "read_file", "arguments": arguments},
        }]}])


def test_malformed_argument_string_is_preserved_for_dispatcher_self_correction(wire_client):
    call = choice({"tool_calls": [{"index": 0, "function": {
        "name": "read_file", "arguments": '{"filepath":"a",}',
    }}]})
    wire_client(lambda request: httpx.Response(200, content=sse(call, choice(finish="tool_calls"))))
    events = run("openai")
    assert events[-1].tool_call["arguments"] == '{"filepath":"a",}'


@pytest.mark.parametrize("kind", ["ollama", "openai", "llamacpp"])
def test_huge_json_integer_never_escapes_as_overflow_error(kind, wire_client):
    huge = 10**400
    if kind == "ollama":
        raw = ndjson({"done": True, "eval_duration": huge})
        selected = backend(kind)
    else:
        chunk = {"usage": {"completion_tokens": huge}}
        selected = backend(kind)
        if kind == "llamacpp":
            selected = transport.LlamaCppBackend("http://local", timeout_s=2)
            chunk = {"timings": {"prompt_ms": 1.0, "predicted_ms": huge}}
        raw = sse(choice(finish="stop"), chunk)
    wire_client(lambda request: httpx.Response(200, content=raw))
    events = list(selected.stream([], None, GenParams(model="test")))
    assert len(events) == 1 and events[0].kind == "error"


@pytest.mark.parametrize("second", [
    {"index": 1, "id": "call_b", "function": {"arguments": "{}"}},
    {"index": 1, "id": "call_a", "function": {"name": "read_file", "arguments": "{}"}},
])
def test_entire_tool_batch_is_validated_before_first_tool_event(second, wire_client):
    raw = sse(OPENAI_CALL, choice({"tool_calls": [second]}), choice(finish="tool_calls"))
    wire_client(lambda request: httpx.Response(200, content=raw))
    events = output(run("openai"))
    assert len(events) == 1 and events[0].kind == "error"


@pytest.mark.parametrize("payload", [[], "not-an-object", 12, None, {"capabilities": []}])
def test_bad_model_info_is_not_cached_as_an_invalid_object(payload, wire_client):
    transport.forget_model_info()
    wire_client(lambda request: httpx.Response(200, json=payload))
    selected = backend("ollama")
    assert isinstance(selected.model_info("test"), dict)
    assert selected.supports_tools("test") in (None, False)
    assert selected.supports_thinking("test") in (None, False)
    assert selected.supports_vision("test") in (None, False)
    transport.forget_model_info()


@pytest.mark.parametrize("payload", [{"models": 12}, {"models": None}, [], "bad"])
def test_bad_loaded_models_diagnostic_never_crashes(payload, wire_client):
    wire_client(lambda request: httpx.Response(200, json=payload))
    assert backend("ollama").loaded_models() == []


@pytest.mark.parametrize("kind,payload", [
    ("ollama", {"models": [12]}), ("ollama", {"models": {"name": "a"}}),
    ("openai", {"data": [12]}), ("openai", {"data": [{"id": 12}]}),
])
def test_bad_catalog_payload_is_a_diagnostic_failure(kind, payload, wire_client):
    wire_client(lambda request: httpx.Response(200, json=payload))
    online, detail, models = backend(kind).status()
    assert online is False and detail and models == []


def test_un_catalogo_grande_come_quello_di_openrouter_passa(wire_client):
    """``/v1/models`` di OpenRouter supera i 10.000 nodi del tetto di default.

    Centinaia di modelli, ognuno con prezzi, architettura e parametri
    supportati: con il tetto pensato per gli argomenti dei tool la sonda
    rispondeva "JSON exceeds the nesting or item limit" e l'endpoint
    risultava irraggiungibile.
    """
    modello = {
        "id": "",
        "name": "x",
        "description": "d" * 400,
        "architecture": {"modality": "text->text", "input_modalities": ["text", "image"],
                         "output_modalities": ["text"], "tokenizer": "Other"},
        "pricing": dict.fromkeys(("prompt", "completion", "request", "image",
                                             "web_search", "internal_reasoning"), "0.000001"),
        "top_provider": {"context_length": 131072, "max_completion_tokens": 8192,
                         "is_moderated": False},
        "supported_parameters": [f"p{i}" for i in range(20)],
    }
    catalogo = {"data": [dict(modello, id=f"vendor/model-{i}") for i in range(600)]}
    wire_client(lambda request: httpx.Response(200, json=catalogo))

    online, detail, models = backend("openai").status()

    assert online, detail
    assert len(models) == 600
