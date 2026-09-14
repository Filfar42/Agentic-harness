"""Adversarial regression tests for HTTP trust, replay and session admission.

All endpoints use in-process ASGI transports; no external account, Docker
daemon or user data is required. Thread races use explicit synchronization.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI, Request

from server import runner as runner_mod
from server import security
from tests.test_server import client, fake_ollama, current_session, read_sse  # noqa: F401


def guarded_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(security.LocalAccessGuard)

    @app.api_route("/api/probe", methods=["GET", "POST"])
    async def probe(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    return app


def guarded_request(
    *, peer: str = "127.0.0.1", host: str = "127.0.0.1:8123",
    headers: dict[str, str] | None = None, body: bytes | AsyncIterator[bytes] = b"",
) -> httpx.Response:
    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=guarded_app(), client=(peer, 43000))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as session:
            return await session.post("/api/probe", headers=headers, content=body)
    return asyncio.run(exercise())


@pytest.mark.parametrize("origin", [
    "http://evil.invalid", "http://127.0.0.1:8124", "null",
    "https://127.0.0.1:8123", "http://user@127.0.0.1:8123",
    "http://127.0.0.1:8123/path", "http://127.0.0.1:8123?x=1",
    "http://[invalid",
])
def test_browser_origin_rejected_before_side_effects(origin: str) -> None:
    response = guarded_request(headers={"Origin": origin})
    assert response.status_code == 403


def test_matching_origin_and_default_port_are_supported() -> None:
    assert security.same_origin("http://localhost", "http", "localhost:80")
    assert security.same_origin("https://[::1]", "https", "[::1]:443")
    response = guarded_request(headers={"Origin": "http://127.0.0.1:8123"}, body=b"ok")
    assert response.status_code == 200
    assert response.json() == {"size": 2}


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_fetch_metadata_rejects_foreign_browser_without_origin(site: str) -> None:
    assert guarded_request(headers={"Sec-Fetch-Site": site}).status_code == 403


def test_dns_rebinding_rejected_even_for_loopback_peer() -> None:
    assert guarded_request(host="evil.invalid:8123").status_code == 403


def test_forwarded_headers_cannot_turn_remote_peer_into_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(security.API_TOKEN_ENV, raising=False)
    response = guarded_request(peer="192.0.2.9", headers={
        "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost",
    })
    assert response.status_code == 403


def test_remote_access_needs_configured_secret_and_origin_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(security.API_TOKEN_ENV, "test-secret")
    assert guarded_request(peer="192.0.2.9").status_code == 403
    valid = {security.API_TOKEN_HEADER: "test-secret"}
    assert guarded_request(peer="192.0.2.9", headers=valid).status_code == 200
    assert guarded_request(peer="192.0.2.9", headers={**valid, "Origin": "null"}).status_code == 403


def test_declared_and_chunked_body_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "MAX_REQUEST_BYTES", 8)
    assert guarded_request(body=b"123456789").status_code == 413

    async def chunked() -> AsyncIterator[bytes]:
        yield b"12345"
        yield b"67890"

    response = guarded_request(body=chunked())
    assert response.status_code == 413, response.text


def test_security_headers_are_present() -> None:
    response = guarded_request()
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_slow_subscriber_is_disconnected_and_replay_is_lossless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_SUBSCRIBER_QUEUE_MAX", 3)
    runner = runner_mod.TurnRunner("slow", [])
    _, slow = runner.subscribe()
    expected = [f'data: {{"type":"content","text":"{i}"}}\n\n' for i in range(12)]
    expected.append('data: {"type":"done","reason":"completed"}\n\n')
    for frame in expected:
        runner.emit(frame)
    runner.close()
    assert slow.qsize() <= 3
    drained = []
    while True:
        try:
            drained.append(slow.get_nowait())
        except queue.Empty:
            break
    assert None in drained, "A slow browser must reconnect, not silently lose tool/done frames."
    assert list(runner.stream()) == expected


def test_async_replay_preserves_finish_during_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "SPEGNIMENTO", threading.Event())

    async def exercise() -> None:
        runner = runner_mod.TurnRunner("race", [])
        first = 'data: {"type":"start"}\n\n'
        last = 'data: {"type":"done"}\n\n'
        runner.emit(first)
        stream = runner.async_stream()
        assert await anext(stream) == first
        runner.emit(last)
        runner.close()
        assert [frame async for frame in stream] == [last]
        assert runner._subscribers == []

    asyncio.run(exercise())


def test_many_async_streams_do_not_consume_request_worker_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "SPEGNIMENTO", threading.Event())

    async def exercise() -> None:
        runner = runner_mod.TurnRunner("many", [])
        streams = [runner.async_stream() for _ in range(64)]
        waits = [asyncio.create_task(anext(stream)) for stream in streams]
        await asyncio.sleep(0)
        frame = 'data: {"type":"done"}\n\n'
        await asyncio.wait_for(asyncio.to_thread(runner.emit, frame), 2)
        assert await asyncio.wait_for(asyncio.gather(*waits), 2) == [frame] * 64
        runner.close()
        for stream in streams:
            await stream.aclose()
        assert runner._subscribers == []

    asyncio.run(exercise())


def test_replay_overflow_has_bounded_memory_and_terminal_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_MAX_REPLAY_BYTES", 100)
    runner = runner_mod.TurnRunner("limit", [])
    runner.emit('data: {"type":"start"}\n\n')
    runner.emit("x" * 101)
    size_after_limit = len(runner.frames)
    for _ in range(100):
        runner.emit("x")
    assert runner.cancelled.is_set()
    assert len(runner.frames) == size_after_limit
    assert any('"reason":"event_limit"' in frame for frame in runner.frames)


def test_worker_exception_is_observable_and_terminal() -> None:
    registry = runner_mod.RunnerRegistry()

    def fail(_runner: runner_mod.TurnRunner) -> None:
        raise RuntimeError("injected failure")

    runner = registry.start("failure", [], fail)
    assert runner.finished.wait(2)
    events = [json.loads(frame[6:]) for frame in runner.frames if frame.startswith("data: ")]
    assert any(event["type"] == "error" for event in events)
    assert any(event["type"] == "done" for event in events)
    assert not registry.is_running("failure")


def test_invalid_settings_batch_has_no_partial_application(client) -> None:
    settings = client.server.STATE.settings
    original = settings["num_ctx"]
    response = client.post("/api/settings", json={"values": {
        "num_ctx": original + 1, "timeout_seconds": "invalid",
    }})
    assert response.status_code == 400
    assert settings["num_ctx"] == original


def test_concurrent_prompt_admission_appends_exactly_one_message(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_agent_loop as fake

    session_id = current_session(client)
    monkeypatch.setattr(fake._Handler, "ritardo_primo_passo", 0.4)
    barrier = threading.Barrier(2)

    def submit() -> int:
        barrier.wait(timeout=2)
        return client.post("/api/chat", json={"session_id": session_id, "prompt": "concurrent"}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit), pool.submit(submit)]
        statuses = sorted(future.result(timeout=10) for future in futures)
    assert statuses == [200, 409]
    read_sse(client.get(f"/api/stream/{session_id}"))
    prompts = [message for message in client.server.STATE.messages(session_id)
               if message.get("role") == "user" and message.get("content") == "concurrent"]
    assert len(prompts) == 1


def test_failed_prompt_persistence_never_starts_work(client, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = current_session(client)
    monkeypatch.setattr(client.server.session_mod, "save_session", lambda *_args, **_kwargs: False)
    response = client.post("/api/chat", json={"session_id": session_id, "prompt": "must persist"})
    assert response.status_code == 507
    assert not client.server.RUNNERS.is_running(session_id)
    assert client.server.STATE.messages(session_id) == []
