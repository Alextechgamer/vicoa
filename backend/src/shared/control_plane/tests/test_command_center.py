"""Command Center read model. No live worker and no Agent Control writes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from shared.control_plane.command_center import (
    assert_action_allowed,
    events_since,
    read_task_diff,
    snapshot,
    task_files,
    task_view,
)
from shared.control_plane.store import ControlPlane, ControlPlaneError


def plane(tmp_path: Path) -> ControlPlane:
    cp = ControlPlane(tmp_path / "cp.db")
    cp.create_account(account_id="vicoa-agy-1", provider="antigravity", runtime_home=str(tmp_path / "a"))
    cp.create_account(account_id="agy-1", provider="antigravity", runtime_home="/home/agentctl/runtimes/agy-1")
    cp.drain("agy-1")
    return cp


def test_snapshot_is_bounded_and_hides_homes(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    job = cp.create_job(project="/tmp/canary", goal="visible")
    task = cp.add_task(job["id"], title="visible", plan_key="visible")
    cp.knowledge.open_session(task["id"], "session-a", account_id="vicoa-agy-1", provider="antigravity")
    view = snapshot(cp, services=[{"name": "agent-control", "state": "active", "role": "legacy caretaker"}])
    dumped = json.dumps(view)
    assert "/home/agentctl" not in dumped
    assert "sk_live_" not in dumped
    assert view["source"] == "control_plane_state"
    assert any(row["session_id"] == "session-a" for row in view["sessions"])
    assert "worker_output" not in dumped


def test_locked_tasks_are_visible_and_actions_are_refused(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    held = cp.create_job(project="/tmp/held", goal="held", import_hold=True)
    task = cp.add_task(held["id"], title="task 6 stand-in", plan_key="held", protected=True, import_hold=True)
    cp.protect(task["id"], "fixture")
    view = task_view(cp, task["id"])
    assert view["locked"] is True
    assert view["actions_allowed"] is False
    with pytest.raises(ControlPlaneError) as exc:
        assert_action_allowed(cp, task["id"], "message")
    assert exc.value.code == "locked"


def test_event_cursor_does_not_replay(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    job = cp.create_job(project="/tmp/events", goal="events")
    task = cp.add_task(job["id"], title="events", plan_key="events")
    first = events_since(cp, 0)
    assert first["events"]
    again = events_since(cp, first["cursor"])
    assert again["events"] == []
    assert again["cursor"] == first["cursor"]
    cp.knowledge.prepare_handoff(task["id"], reason="manual")
    delta = events_since(cp, first["cursor"])
    assert delta["events"]
    assert all(item["id"] not in {row["id"] for row in first["events"]} for item in delta["events"])
    assert "sk-" not in json.dumps(delta)


def test_worktree_paths_reject_traversal_and_symlink_escape(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    (root / "ok.txt").write_text("ok\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    link = root / "escape"
    link.symlink_to(outside)
    job = cp.create_job(project=str(root), goal="files")
    cp.create_account(account_id="canary-a", provider="antigravity", runtime_home=str(tmp_path / "home"))
    task = cp.add_task(job["id"], title="files", plan_key="files")
    cp.start_task(task["id"], account_id="canary-a", session_id="files")
    cp.complete_worker(task["id"], worktree_path=str(root), output="done")
    assert task_files(cp, task["id"])["available"] is True
    with pytest.raises(ControlPlaneError) as traversal:
        read_task_diff(cp, task["id"], "../outside.txt")
    assert traversal.value.code == "path_rejected"
    with pytest.raises(ControlPlaneError) as linked:
        read_task_diff(cp, task["id"], "escape")
    assert linked.value.code == "path_rejected"
