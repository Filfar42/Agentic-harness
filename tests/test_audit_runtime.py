"""Regression tests for process budgets, final acknowledgements and previews."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import agent, atomic, process, tools
from server import previewhost
from tests.test_server import client, current_session, fake_ollama, read_sse  # noqa: F401


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_process_budget_never_starts(timeout, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Popen must not be called")
    monkeypatch.setattr(process.subprocess, "Popen", unexpected)
    with pytest.raises(ValueError):
        process.run_bounded([sys.executable, "-c", "pass"], timeout=timeout)


def test_command_timeout_is_bounded():
    with pytest.raises(subprocess.TimeoutExpired):
        process.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.15)


def test_noisy_command_does_not_return_unlimited_output():
    with pytest.raises(process.OutputLimitExceeded):
        process.run_bounded([sys.executable, "-c", "print('x' * 100000)"], timeout=10, max_output_bytes=1000)


def test_exit_code_and_quoted_arguments_survive_shell(tmp_path: Path):
    script = tmp_path / "file with spaces.py"
    script.write_text("import sys\nprint(sys.argv[1])\nsys.exit(3)\n", encoding="utf-8")
    result = process.run_bounded(f'"{sys.executable}" "{script}" "hello world"', timeout=10, shell=True)
    assert result.returncode == 3
    assert result.stdout.strip() == "hello world"


def test_atomic_replace_retries_windows_sharing_violation(tmp_path: Path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    original_replace = atomic.os.replace
    attempts = []
    def busy(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        original_replace(source, destination)
    monkeypatch.setattr(atomic.os, "replace", busy)
    monkeypatch.setattr(atomic.time, "sleep", lambda _delay: None)
    atomic.write_text(target, "new")
    assert target.read_text() == "new" and len(attempts) == 3
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("manifest", [[], None, {"scripts": 3}, {"dependencies": []},
                                        {"scripts": "dev", "devDependencies": "vite"}])
def test_preview_backend_rejects_wrong_manifest_shapes(tmp_path: Path, manifest):
    (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert tools.preview_backend(tmp_path, 8200) is None


def test_preview_directory_index_cannot_escape_root(tmp_path: Path):
    root = tmp_path / "public"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    try:
        (root / "index.html").symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks require Windows Developer Mode or privileges")
    previewhost.set_root(root)
    with TestClient(previewhost.app) as browser:
        response = browser.get("/")
    assert response.status_code == 403
    assert "private" not in response.text


def test_schema_budget_reduces_space_without_losing_system():
    messages = [{"role": "system", "content": "essential"}, {"role": "user", "content": "task"}]
    baseline = agent.tetto_per_la_finestra(messages, 4096, 4096)
    reserved = agent.tetto_per_la_finestra(messages, 4096, 4096, reserved_tokens=1000)
    assert baseline[1] - reserved[1] == 1000
    assert messages[0]["content"] == "essential"


def test_done_is_emitted_only_after_final_save(client, monkeypatch):
    sid = current_session(client)
    def finish(**kwargs):
        yield agent.TurnFinished(reason="completed", steps=1)
    monkeypatch.setattr(client.server.agent_mod, "run_turn", finish)
    original_save = client.server.STATE.save
    persisted = []
    def save(session_id, **kwargs):
        original_save(session_id, **kwargs)
        if kwargs.get("riscrivi"):
            persisted.append(True)
    original_emit = client.server.TurnRunner.emit
    def emit(runner, frame):
        if '"type": "done"' in frame:
            assert persisted, "Completion cannot precede final persistence"
        original_emit(runner, frame)
    monkeypatch.setattr(client.server.STATE, "save", save)
    monkeypatch.setattr(client.server.TurnRunner, "emit", emit)
    response = client.post("/api/chat", json={"session_id": sid, "prompt": "task"})
    assert response.status_code == 200
    events = read_sse(client.get(f"/api/stream/{sid}"))
    assert [e["reason"] for e in events if e["type"] == "done"] == ["completed"]


def test_final_save_failure_is_terminal_error(client, monkeypatch):
    sid = current_session(client)
    def finish(**kwargs):
        yield agent.TurnFinished(reason="completed", steps=1)
    monkeypatch.setattr(client.server.agent_mod, "run_turn", finish)
    original_save = client.server.STATE.save
    def save(session_id, **kwargs):
        if kwargs.get("riscrivi"):
            raise OSError("disk full")
        original_save(session_id, **kwargs)
    monkeypatch.setattr(client.server.STATE, "save", save)
    assert client.post("/api/chat", json={"session_id": sid, "prompt": "task"}).status_code == 200
    events = read_sse(client.get(f"/api/stream/{sid}"))
    assert [e["reason"] for e in events if e["type"] == "done"] == ["persistence_error"]
    assert any(e["type"] == "error" and "disk full" in e["message"] for e in events)
