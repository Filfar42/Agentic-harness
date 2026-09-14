"""Telemetry survives reopening, without breaking old or oversized sessions."""

from __future__ import annotations

import json

import pytest

from core import agent, session
from core.backend import StreamEvent
from core.config import GenParams
from core.telemetry import track_backend
from tests.test_server import client, current_session, fake_ollama, read_sse  # noqa: F401


def _snapshot():
    class Backend:
        def stream(self, *args):
            yield StreamEvent("content", text="private answer")
            yield StreamEvent("usage", usage={"prompt_tokens": 42, "completion_tokens": 8})
    wrapped = track_backend(Backend())
    list(wrapped.scope("main").stream([], None, GenParams()))
    list(wrapped.scope("compaction").stream([], None, GenParams()))
    return wrapped.collector.snapshot()


def test_turn_telemetry_round_trip_and_new_session_reset():
    state = {}
    sid = session.new_session(state)
    snapshot = _snapshot()
    session.record_turn_telemetry(state, snapshot, reason="completed", steps=1)
    state["messages"] = [{"role": "user", "content": "request"}]
    assert session.save_session(state, force=True)
    loaded = {}
    assert session.load_session(loaded, sid)
    result = loaded["turn_telemetry"]
    assert len(result) == 1
    assert result[0]["turn_reason"] == "completed"
    assert result[0]["steps"] == 1
    assert result[0]["totals"]["prompt_tokens"] == 84
    assert result[0]["totals"]["by_purpose"]["compaction"]["calls"] == 1
    assert "private answer" not in json.dumps(result)
    session.new_session(loaded)
    assert loaded["turn_telemetry"] == []


@pytest.mark.parametrize("extra", [{}, {"turn_telemetry": None}, {"turn_telemetry": "broken"},
                                  {"turn_telemetry": [False, 17, "broken"]}])
def test_legacy_or_invalid_optional_telemetry_does_not_break_chat(extra):
    session.ensure_dirs()
    (session.DATA_DIR / "legacy.json").write_text(json.dumps({
        "messages": [{"role": "user", "content": "old chat"}], **extra,
    }), encoding="utf-8")
    state = {"turn_telemetry": [{"stale": True}]}
    assert session.load_session(state, "legacy")
    assert state["messages"][0]["content"] == "old chat"
    assert state["turn_telemetry"] == []


def test_keep_only_last_fifty_turns():
    state = {"turn_telemetry": []}
    for index in range(65):
        session.record_turn_telemetry(state, {"version": 1, "totals": {"calls": index}},
                                      reason="completed", steps=1)
    turns = state["turn_telemetry"]
    assert len(turns) == 50
    assert turns[0]["totals"]["calls"] == 15
    assert turns[-1]["totals"]["calls"] == 64


def test_saved_byte_budget_counts_utf8_and_indentation():
    state = {}
    sid = session.new_session(state)
    state["turn_telemetry"] = [
        {"version": 1, "index": index, "calls": [{"diagnostic": "è" * 25000}], "totals": {"calls": 1}}
        for index in range(50)
    ]
    assert session.save_session(state, force=True)
    raw = json.loads(session.telemetry_path(sid).read_text(encoding="utf-8"))
    turns = raw["turn_telemetry"]
    serialized = json.dumps({"turn_telemetry": turns}, indent=2, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= session.MAX_TELEMETRY_BYTES
    assert 1 < len(turns) < 50
    assert turns[-1]["index"] == 49


def test_intermediate_saves_do_not_reserialize_unchanged_telemetry(monkeypatch):
    state = {}
    sid = session.new_session(state)
    session.record_turn_telemetry(state, _snapshot(), reason="completed", steps=1)
    assert session.save_session(state, force=True)
    original = session.telemetry_path(sid).read_bytes()
    metadata = json.loads((session.DATA_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    assert "turn_telemetry" not in metadata

    def unexpected(*args, **kwargs):
        pytest.fail("Unchanged telemetry was sanitized during a tool save")
    monkeypatch.setattr(session, "bounded_turn_telemetry", unexpected)
    original_write = session._atomic_write_json
    def write_metadata_only(path, payload):
        assert not path.name.endswith(".telemetry.json")
        original_write(path, payload)
    monkeypatch.setattr(session, "_atomic_write_json", write_metadata_only)
    for index in range(3):
        state["messages"].append({"role": "assistant", "content": f"step {index}"})
        assert session.save_session(state, force=True)
    assert session.telemetry_path(sid).read_bytes() == original


def test_sidecar_failure_preserves_chat_and_retries(monkeypatch):
    state = {}
    sid = session.new_session(state)
    session.record_turn_telemetry(state, {"totals": {"calls": 1}}, reason="completed", steps=1)
    assert session.save_session(state, force=True)
    session.record_turn_telemetry(state, _snapshot(), reason="completed", steps=1)
    state["messages"] = [{"role": "user", "content": "preserve this"}]
    write = session._atomic_write_json
    def fail_sidecar(path, payload):
        if path.name.endswith(".telemetry.json"):
            raise OSError("sidecar unavailable")
        write(path, payload)
    monkeypatch.setattr(session, "_atomic_write_json", fail_sidecar)
    assert not session.save_session(state, force=True)
    assert "sidecar unavailable" in state["save_error"]
    restored = {}
    assert session.load_session(restored, sid)
    assert restored["messages"] == state["messages"]
    assert len(restored["turn_telemetry"]) == 2
    assert restored["turn_telemetry"][-1]["totals"]["calls"] == 2
    monkeypatch.setattr(session, "_atomic_write_json", write)
    assert session.save_session(state, force=True)
    assert "save_error" not in state
    assert session.telemetry_path(sid).exists()


def test_sidecar_is_not_a_chat_and_is_deleted_with_its_session():
    state = {}
    sid = session.new_session(state)
    state["messages"] = [{"role": "user", "content": "unique_chat_marker"}]
    session.record_turn_telemetry(state, _snapshot(), reason="completed", steps=1)
    assert session.save_session(state, force=True)
    assert [entry["id"] for entry in session.list_sessions()] == [sid]
    assert [entry["id"] for entry in session.search_sessions("unique_chat_marker")] == [sid]
    session.delete_session(sid)
    assert not session.telemetry_path(sid).exists()
    with pytest.raises(ValueError, match="Invalid session id"):
        session.telemetry_path("../escape")


def test_valid_inline_telemetry_migrates_without_becoming_a_chat():
    session.ensure_dirs()
    original = [{"version": 1, "totals": {"calls": 7}}]
    path = session.DATA_DIR / "legacy.json"
    path.write_text(json.dumps({"messages": [], "turn_telemetry": original}), encoding="utf-8")
    state = {}
    assert session.load_session(state, "legacy")
    assert state["turn_telemetry"] == original
    assert session.save_session(state, force=True)
    assert "turn_telemetry" not in json.loads(path.read_text(encoding="utf-8"))
    reopened = {}
    assert session.load_session(reopened, "legacy")
    assert reopened["turn_telemetry"] == original


def test_one_oversized_turn_retains_totals_but_drops_call_details():
    turns = session.bounded_turn_telemetry([{
        "version": 1, "totals": {"calls": 3}, "calls_truncated": 1,
        "calls": [{"detail": "a" * session.MAX_TELEMETRY_BYTES}, {"detail": "b"}],
    }])
    assert len(turns) == 1
    assert turns[0]["totals"] == {"calls": 3}
    assert turns[0]["calls"] == []
    assert turns[0]["calls_truncated"] == 3


def test_non_json_telemetry_is_discarded_without_affecting_history():
    state = {}
    sid = session.new_session(state)
    state["messages"] = [{"role": "user", "content": "keep me"}]
    state["turn_telemetry"] = [{"version": 1}, {"invalid": object()}, {"invalid": float("nan")}]
    assert session.save_session(state, force=True)
    restored = {}
    assert session.load_session(restored, sid)
    assert restored["messages"] == state["messages"]
    assert restored["turn_telemetry"] == [{"version": 1}]


def test_server_persists_before_done_and_exposes_reopened_telemetry(client, monkeypatch):
    sid = current_session(client)
    snapshot = _snapshot()
    def finish(**kwargs):
        yield agent.TurnFinished(reason="completed", steps=1, telemetry=snapshot)
    monkeypatch.setattr(client.server.agent_mod, "run_turn", finish)
    emitted = []
    original_emit = client.server.TurnRunner.emit
    def emit(runner, frame):
        if '"type": "done"' in frame:
            stored = {}
            assert session.load_session(stored, sid)
            assert stored["turn_telemetry"][-1]["totals"]["calls"] == 2
            emitted.append(True)
        original_emit(runner, frame)
    monkeypatch.setattr(client.server.TurnRunner, "emit", emit)
    started = client.post("/api/chat", json={"session_id": sid, "prompt": "task"})
    assert started.status_code == 200
    events = read_sse(client.get(f"/api/stream/{sid}"))
    done = [event for event in events if event["type"] == "done"]
    assert done[0]["telemetry"]["totals"]["calls"] == 2
    assert emitted
    client.server.STATE.drop(sid)
    response = client.get(f"/api/sessions/{sid}")
    assert response.status_code == 200
    assert response.json()["turn_telemetry"][-1]["totals"]["prompt_tokens"] == 84
