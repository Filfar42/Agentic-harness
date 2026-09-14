"""Adversarial tool-boundary tests: reject ambiguity before any side effect."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from core import atomic, spec_delega, tools
from core.tools import ToolContext, dispatch, validate_tool_arguments


@pytest.mark.parametrize(("name", "args"), [
    ("edit_file", {"filepath": "a.txt", "old_string": "old", "new_string": "new",
                   "replace_all": "false"}),
    ("run_command", {"command": "echo unused", "timeout_sec": True}),
    ("run_command", {"command": "echo unused", "timeout_sec": 0}),
    ("run_command", {"command": "echo unused", "timeout_sec": -1}),
    ("run_command", {"command": "echo unused", "timeout_sec": 3601}),
    ("run_command", {"command": "echo\0unused"}),
    ("write_file", {"filepath": "a.txt"}),
    ("write_file", {"filepath": "a.txt", "content": None}),
    ("write_file", {"filepath": "a.txt", "content": "text", "append": True}),
    ("write_file", {"filepath": "a.txt", "content": "bad\ud800"}),
    ("read_file", {"filepath": []}),
    ("read_file", {"filepath": "a.txt", "start_line": 20, "end_line": 2}),
    ("manage_plan", {"action": "set", "steps": [{"text": "unexpected object"}]}),
    ("manage_plan", {"action": "complete", "step_id": "1", "ignore_red": "false"}),
    ("manage_notes", {"action": "invented"}),
    ("read_file", []),
    ("read_file", None),
    ("read_file", "{}"),
])
def test_invalid_arguments_never_reach_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, args: Any
) -> None:
    calls: list[dict[str, Any]] = []

    def implementation(ctx: ToolContext, **kwargs: Any) -> str:
        calls.append(kwargs)
        return '{"status":"unexpected"}'

    monkeypatch.setitem(tools.TOOL_IMPLS, name, implementation)
    result = json.loads(dispatch(ToolContext(workspace=str(tmp_path)), name, args))
    assert result["error_code"] == "invalid_arguments"
    assert result["retryable"] is False
    assert result["details"]
    assert result["hint"]
    assert not calls


def test_boolean_string_cannot_replace_all_occurrences(tmp_path: Path) -> None:
    path = tmp_path / "content.txt"
    path.write_text("old old", encoding="utf-8")
    result = json.loads(dispatch(ToolContext(workspace=str(tmp_path)), "edit_file", {
        "filepath": path.name, "old_string": "old", "new_string": "new",
        "replace_all": "false",
    }))
    assert result["error_code"] == "invalid_arguments"
    assert path.read_text(encoding="utf-8") == "old old"


def test_question_arguments_use_the_same_strict_boundary() -> None:
    assert validate_tool_arguments("ask_user_question", {
        "question": "Quale formato?", "options": ["JSON", "CSV"], "allow_multiple": False,
    }) is None
    assert validate_tool_arguments("ask_user_question", {
        "question": "Quale formato?", "allow_multiple": "false",
    })["error_code"] == "invalid_arguments"


def test_empty_file_and_empty_replacement_are_valid() -> None:
    assert validate_tool_arguments("write_file", {"filepath": "empty", "content": ""}) is None
    assert validate_tool_arguments("edit_file", {
        "filepath": "empty", "old_string": "remove", "new_string": "",
    }) is None


def test_concurrent_uploads_never_overwrite_each_other(tmp_path: Path) -> None:
    workers = 12
    barrier = Barrier(workers)

    def upload(index: int) -> dict[str, Any]:
        barrier.wait(timeout=10)
        return tools.store_attachment(tmp_path, "same.txt", f"payload-{index}".encode())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(upload, range(workers)))
    assert len({entry["path"] for entry in results}) == workers
    assert {(tmp_path / entry["path"]).read_text() for entry in results} == {
        f"payload-{index}" for index in range(workers)
    }


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Filesystem cannot create symbolic links: {exc}")


def test_upload_does_not_follow_dangling_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    attachment_dir = workspace / "allegati"
    attachment_dir.mkdir(parents=True)
    external = tmp_path / "outside.txt"
    _symlink_or_skip(attachment_dir / "same.txt", external)
    result = tools.store_attachment(workspace, "same.txt", b"safe")
    assert result["path"] == "allegati/same-1.txt"
    assert not external.exists()


def test_search_cannot_read_file_symlink_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("private-audit-marker", encoding="utf-8")
    _symlink_or_skip(workspace / "link.txt", external)
    result = json.loads(dispatch(ToolContext(workspace=str(workspace)), "search_files", {
        "pattern": "private-audit-marker",
    }))
    assert "private-audit-marker" not in json.dumps(result.get("matches", []))
    assert result["files_scanned"] == 0


def test_atomic_edit_does_not_modify_external_hardlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("before", encoding="utf-8")
    linked = workspace / "content.txt"
    try:
        os.link(external, linked)
    except OSError as exc:
        pytest.skip(f"Filesystem cannot create hard links: {exc}")
    result = json.loads(dispatch(ToolContext(workspace=str(workspace)), "edit_file", {
        "filepath": linked.name, "old_string": "before", "new_string": "after",
    }))
    assert result["status"] == "ok"
    assert linked.read_text() == "after"
    assert external.read_text() == "before"


def test_replace_failure_preserves_existing_file_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "content.txt"
    target.write_text("before", encoding="utf-8")

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(atomic.os, "replace", fail_replace)
    result = json.loads(dispatch(ToolContext(workspace=str(tmp_path)), "edit_file", {
        "filepath": target.name, "old_string": "before", "new_string": "after",
    }))
    assert "error" in result
    assert target.read_text() == "before"
    assert set(tmp_path.iterdir()) == {target}


def test_concurrent_edits_preserve_all_independent_updates(tmp_path: Path) -> None:
    target = tmp_path / "content.txt"
    count = 12
    target.write_text("\n".join(f"old-{i:02}" for i in range(count)), encoding="utf-8")
    barrier = Barrier(count)

    def edit(index: int) -> dict[str, Any]:
        barrier.wait(timeout=10)
        return json.loads(dispatch(ToolContext(workspace=str(tmp_path)), "edit_file", {
            "filepath": target.name, "old_string": f"old-{index:02}",
            "new_string": f"new-{index:02}",
        }))

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(edit, range(count)))
    assert all(result.get("status") == "ok" for result in results)
    assert target.read_text().splitlines() == [f"new-{i:02}" for i in range(count)]


def test_file_size_limit_precedes_unbounded_read(tmp_path: Path) -> None:
    target = tmp_path / "oversized.txt"
    with target.open("wb") as stream:
        stream.truncate(tools.MAX_TEXT_FILE_BYTES + 1)
    result = json.loads(dispatch(ToolContext(workspace=str(tmp_path)), "read_file", {
        "filepath": target.name,
    }))
    assert "error" in result
    assert "content" not in result


def test_delegation_fact_transactions_do_not_lose_concurrent_updates(tmp_path: Path) -> None:
    count = 40

    def record(index: int) -> None:
        spec_delega.registra(tmp_path, domanda=f"question-{index}", passi=2, riuscito=True)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(record, range(count)))
    facts = spec_delega.leggi(tmp_path)
    assert {fact["domanda"] for fact in facts} == {f"question-{i}" for i in range(count)}
    assert len(facts) == count


def test_delegation_fact_storage_rejects_wrong_types(tmp_path: Path) -> None:
    directory = tmp_path / ".memoria"
    directory.mkdir()
    (directory / spec_delega.NOME).write_text(json.dumps([{
        "domanda": "x", "passi": 1, "riuscito": "false", "esaurito": False,
        "chiuso_a_forza": False,
    }]), encoding="utf-8")
    assert spec_delega.leggi(tmp_path) == []
