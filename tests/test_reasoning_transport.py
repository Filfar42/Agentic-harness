"""Reasoning controls must reach the wire without overstating backend support."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.backend as backend_mod
from core.backend import LlamaCppBackend, OllamaBackend, OpenAICompatBackend
from core.config import GenParams


@pytest.fixture()
def wire(monkeypatch):
    """Capture real transport requests, with configurable server metadata/usage."""
    state = {"requests": [], "props": {}, "usage": None, "native_usage": {}}

    def respond(request):
        state["requests"].append(request)
        if request.url.path == "/props":
            return httpx.Response(200, json=state["props"])
        if request.url.path == "/api/chat":
            chunk = {"message": {"content": "ok"}, "done": True,
                     "done_reason": "stop", **state["native_usage"]}
            return httpx.Response(200, content=json.dumps(chunk).encode() + b"\n")
        assert request.url.path == "/v1/chat/completions"
        chunks = [{"choices": [{"index": 0, "delta": {"content": "ok"},
                                "finish_reason": "stop"}]}]
        if state["usage"] is not None:
            chunks.append({"choices": [], "usage": state["usage"]})
        content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(200, content=content + "data: [DONE]\n\n")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(backend_mod, "get_client", lambda: client)
        yield state


def generate(backend, params, wire):
    events = list(backend.stream([{"role": "user", "content": "ciao"}], None, params))
    assert not [event for event in events if event.kind == "error"]
    return json.loads(wire["requests"][-1].content), events


@pytest.mark.parametrize("think", [False, True, "low", "medium", "high", "max"])
def test_native_think_including_false_reaches_http(wire, think):
    backend = OllamaBackend("http://test", stream_with_tools=True)
    params = GenParams(think=think)
    report = backend.reasoning_control(params)
    assert wire["requests"] == []
    body, _ = generate(backend, params, wire)
    assert body["think"] == think
    assert report == {"requested": think, "payload": {"think": think},
                      "support": "native_unverified", "verified": False}
    assert body["options"]["repeat_penalty"] == 1.0


def test_native_diagnostic_reports_learned_boolean_fallback(wire):
    backend = OllamaBackend("http://test", stream_with_tools=True)
    assert backend._livello_rifiutato({"think": "low"}, "unsupported think level")
    params = GenParams(think="low")
    report = backend.reasoning_control(params)
    body, _ = generate(backend, params, wire)
    assert body["think"] is True
    assert report["payload"] == {"think": True}
    assert report["requested"] == "low" and report["support"] == "boolean_only"
    assert report["verified"] is False
    assert backend.reasoning_control(GenParams(think=False))["payload"] == {"think": False}


@pytest.mark.parametrize("think", ["low", "medium", "high", "max"])
def test_explicit_effort_reaches_openai_compatible_http(wire, think):
    backend = OpenAICompatBackend("http://test", "")
    params = GenParams(model="custom-model-alias", think=think)
    body, _ = generate(backend, params, wire)
    assert body["reasoning_effort"] == think
    assert "chat_template_kwargs" not in body
    assert backend.reasoning_control(params)["verified"] is False


@pytest.mark.parametrize(("model", "key"), [
    ("Qwen/Qwen3-8B", "enable_thinking"),
    ("Qwen/Qwen3.5-9B", "enable_thinking"),
    ("google/gemma-4-26B", "enable_thinking"),
    ("deepseek-ai/DeepSeek-V3.1", "thinking"),
    ("ibm-granite/granite-3.2-8b-instruct", "thinking"),
])
@pytest.mark.parametrize("think", [False, True])
def test_known_hybrid_template_switches_reach_http(wire, model, key, think):
    backend = OpenAICompatBackend("http://test", "")
    params = GenParams(model=model, think=think)
    body, _ = generate(backend, params, wire)
    assert body["chat_template_kwargs"] == {key: think}
    assert "think" not in body
    assert "reasoning_effort" not in body
    assert backend.reasoning_control(params)["support"] == "template_unverified"


@pytest.mark.parametrize("model", ["custom", "Qwen2.5-7B", "DeepSeek-R1-Distill-Qwen3-8B",
                                    "Qwen3-235B-A22B-Instruct-2507", "Qwen3-32B-Thinking"])
def test_unknown_boolean_capability_does_not_invent_a_switch(wire, model):
    backend = OpenAICompatBackend("http://test", "")
    params = GenParams(model=model, think=False)
    report = backend.reasoning_control(params)
    assert report["payload"] == {} and report["support"] == "unknown"
    assert wire["requests"] == []
    body, _ = generate(backend, params, wire)
    assert "chat_template_kwargs" not in body and "reasoning_effort" not in body


def test_llama_effort_is_a_template_kwarg_and_uses_cached_props(wire):
    wire["props"] = {"chat_template": "{% if enable_thinking %}{{ reasoning_effort }}{% endif %}"}
    backend = LlamaCppBackend("http://test")
    backend.props()
    for think in ("low", "high", "max"):
        params = GenParams(model="alias", think=think)
        report = backend.reasoning_control(params)
        body, _ = generate(backend, params, wire)
        assert body["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": think}
        assert report["payload"] == {"chat_template_kwargs": body["chat_template_kwargs"]}
        assert "reasoning_effort" not in body
        assert report["verified"] is False
    assert sum(request.url.path == "/props" for request in wire["requests"]) == 1


@pytest.mark.parametrize("key", ["enable_thinking", "thinking"])
def test_llama_boolean_template_does_not_pretend_it_accepts_effort(wire, key):
    wire["props"] = {"chat_template": "{% if " + key + " %}<think>{% endif %}"}
    backend = LlamaCppBackend("http://test")
    backend.props()
    params = GenParams(think="low")
    body, _ = generate(backend, params, wire)
    assert body["chat_template_kwargs"] == {key: True}
    assert "reasoning_effort" not in body
    assert backend.reasoning_control(params)["support"] == "boolean_only"


@pytest.mark.parametrize("template", ["", "Thinking <think>reasoning_effort</think>",
                                      "{{ 'thinking enable_thinking reasoning_effort' }}",
                                      "{# enable_thinking reasoning_effort #}{{ messages }}"])
def test_llama_template_prose_is_not_a_control_and_aliases_are_not_evidence(wire, template):
    wire["props"] = {"chat_template": template, "chat_template_caps": {"supports_reasoning": True}}
    backend = LlamaCppBackend("http://test")
    backend.props()
    params = GenParams(model="Qwen3-8B", think="high")
    report = backend.reasoning_control(params)
    body, _ = generate(backend, params, wire)
    assert report["payload"] == {} and report["support"] == "unknown"
    assert "reasoning_effort" not in body and "chat_template_kwargs" not in body


def test_llama_false_uses_documented_none_even_without_props(wire):
    backend = LlamaCppBackend("http://test")
    params = GenParams(think=False)
    body, _ = generate(backend, params, wire)
    assert body["reasoning_effort"] == "none"
    assert body["repeat_penalty"] == 1.0
    report = backend.reasoning_control(params)
    assert report["support"] == "disable_unverified" and report["verified"] is False
    assert len(wire["requests"]) == 1


@pytest.mark.parametrize("backend_class", [OllamaBackend, OpenAICompatBackend, LlamaCppBackend])
def test_unrecognized_think_is_omitted_without_http_diagnostics(wire, backend_class):
    backend = backend_class("http://test", "") if backend_class is OpenAICompatBackend else backend_class("http://test")
    params = GenParams(think="invalid")
    report = backend.reasoning_control(params)
    assert report["payload"] == {} and report["support"] == "omitted"
    assert wire["requests"] == []


@pytest.mark.parametrize("data", [{}, {"prompt_tokens": None, "completion_tokens": None},
                                  {"prompt_tokens": 0}, {"completion_tokens": 7}])
def test_openai_usage_preserves_missing_and_real_zero_counters(wire, data):
    wire["usage"] = data
    _, events = generate(OpenAICompatBackend("http://test", ""), GenParams(), wire)
    usage = next(event.usage for event in events if event.kind == "usage")
    assert usage == {"done_reason": "stop", **{k: v for k, v in data.items() if v is not None}}


@pytest.mark.parametrize("data", [{}, {"prompt_eval_count": None, "eval_count": None},
                                  {"prompt_eval_count": 0}, {"eval_count": 7}])
def test_native_usage_preserves_missing_and_real_zero_counters(wire, data):
    wire["native_usage"] = data
    _, events = generate(OllamaBackend("http://test"), GenParams(), wire)
    usage = next(event.usage for event in events if event.kind == "usage")
    expected = {"done_reason": "stop"}
    for source, target in (("prompt_eval_count", "prompt_tokens"), ("eval_count", "completion_tokens")):
        if data.get(source) is not None:
            expected[target] = data[source]
    assert usage == expected
