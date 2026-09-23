"""Disposable local-worker canaries. No live Agent Control contact."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared.control_plane.mcp_surface import dispatch
from shared.control_plane.store import ControlPlane, ControlPlaneError
from shared.control_plane.worker import LIVE_RUNTIME_HOMES, LocalWorker


def repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "canary@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Canary"], cwd=path, check=True)
    (path / "README").write_text("canary\n")
    subprocess.run(["git", "add", "README"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)
    return path


def plane(tmp_path: Path) -> ControlPlane:
    cp = ControlPlane(tmp_path / "control.db")
    cp.create_account(account_id="agy-1", provider="antigravity", runtime_home=str(tmp_path / "homes" / "agy-1"), profile="agy")
    cp.create_account(account_id="agy-2", provider="antigravity", runtime_home=str(tmp_path / "homes" / "agy-2"), profile="agy")
    return cp


def test_file_canary_reaches_verified_not_merely_complete(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    root = repo(tmp_path / "repo")
    job = cp.create_job(project=str(root), goal="file canary")
    task = cp.add_task(job["id"], title="marker", plan_key="marker")
    worker = LocalWorker(cp, tmp_path / "worktrees")
    done = worker.launch(
        task["id"],
        repo=root,
        argv=["python3", "-c", "open('marker.txt','w').write('EXACT-MARKER\\n')"],
    )
    assert done["worker_status"] == "candidate_complete"
    assert done["verification_status"] == "pending"
    verified = cp.verify_task(
        task["id"],
        [
            {"type": "file_exists", "path": "marker.txt"},
            {"type": "file_contains", "path": "marker.txt", "text": "EXACT-MARKER"},
        ],
    )
    assert verified["verification_status"] == "passed"
    assert cp.task(task["id"])["worker_status"] != "verified"


def test_dag_and_failed_verification_do_not_launch_duplicates(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    root = repo(tmp_path / "repo")
    job = cp.create_job(project=str(root), goal="dag")
    first = cp.add_task(job["id"], title="A", plan_key="A")
    second = cp.add_task(job["id"], title="B", plan_key="B")
    third = cp.add_task(job["id"], title="C", plan_key="C")
    cp.add_dependency(second["id"], first["id"])
    cp.add_dependency(third["id"], second["id"])
    worker = LocalWorker(cp, tmp_path / "worktrees")
    worker.launch(first["id"], repo=root, argv=["python3", "-c", "open('a.txt','w').write('wrong\\n')"])
    with pytest.raises(ControlPlaneError):
        worker.launch(second["id"], repo=root, argv=["python3", "-c", "print('no')"])
    failed = cp.verify_task(first["id"], [{"type": "file_contains", "path": "a.txt", "text": "EXACT"}])
    assert failed["verification_status"] == "revision_required"
    assert cp.task(second["id"])["worker_status"] == "queued"
    assert cp.account("agy-1")["active_workers"] == 0
    worker.launch(first["id"], repo=root, argv=["python3", "-c", "open('a.txt','w').write('EXACT\\n')"])
    cp.verify_task(first["id"], [{"type": "file_contains", "path": "a.txt", "text": "EXACT"}])
    worker.launch(second["id"], repo=root, argv=["python3", "-c", "open('b.txt','w').write('B-OK\\n')"])
    with pytest.raises(ControlPlaneError):
        worker.launch(third["id"], repo=root, argv=["python3", "-c", "print('no')"])
    cp.verify_task(second["id"], [{"type": "file_contains", "path": "b.txt", "text": "B-OK"}])
    worker.launch(third["id"], repo=root, argv=["python3", "-c", "open('c.txt','w').write('C-OK\\n')"])
    cp.verify_task(third["id"], [{"type": "file_exists", "path": "c.txt"}])
    assert cp.task(third["id"])["verification_status"] == "passed"


def test_messages_wait_until_worker_accepts(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    job = cp.create_job(project="/tmp/canary", goal="messages")
    task = cp.add_task(job["id"], title="messages", plan_key="messages", worker_status="needs_input")
    first = cp.enqueue_message(task["id"], "first prompt stays queued", idempotency_key="m1")
    second = cp.enqueue_message(task["id"], "second prompt stays queued", idempotency_key="m2")
    again = cp.enqueue_message(task["id"], "first prompt stays queued", idempotency_key="m1")
    assert again["id"] == first["id"]
    held = cp.deliver_next(task["id"], worker_accepting=False)
    assert held["state"] == "queued"
    sent = cp.deliver_next(task["id"], worker_accepting=True)
    assert sent["id"] == first["id"]
    assert sent["state"] == "sent"
    assert cp.messages(task["id"])[1]["id"] == second["id"]
    assert cp.messages(task["id"])[1]["state"] == "queued"
    assert cp.acknowledge_message(sent["id"])["state"] == "acknowledged"


def test_approval_once_does_not_create_a_standing_rule(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    job = cp.create_job(project="/tmp/canary", goal="approval")
    task = cp.add_task(job["id"], title="approval", plan_key="approval")
    prompt = "Allow reading repository status in the canary worktree?"
    approval = cp.open_approval(task["id"], prompt_text=prompt)
    decided = cp.approve_once(approval["id"], prompt_text=prompt)
    assert decided["status"] == "approved_once"
    assert decided["permanent"] == 0
    with cp._conn() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM allow_rules").fetchone()["n"] == 0
    other = "Allow writing an unrelated file in the canary worktree?"
    second = cp.open_approval(task["id"], prompt_text=other)
    with pytest.raises(ControlPlaneError):
        cp.approve_once(second["id"], prompt_text=prompt)


def test_restart_does_not_duplicate_a_running_local_worker(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    root = repo(tmp_path / "repo")
    job = cp.create_job(project=str(root), goal="restart")
    task = cp.add_task(job["id"], title="restart", plan_key="restart")
    cp.start_task(task["id"], account_id="agy-1", session_id="local-restart")
    cp.enqueue_message(task["id"], "survive restart", idempotency_key="keep")
    recovered = cp.recover_after_restart()
    assert task["id"] in recovered
    assert cp.task(task["id"])["worker_status"] == "interrupted"
    assert cp.account("agy-1")["active_workers"] == 0
    assert cp.messages(task["id"])[0]["state"] == "queued"
    claimed = cp.claim_job(job["id"], "hermes-main")
    assert claimed["started"] == []


def test_same_provider_profiles_stay_isolated_and_live_homes_are_refused(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    assert cp.account("agy-1")["runtime_home_hash"] != cp.account("agy-2")["runtime_home_hash"]
    assert "runtime_home" not in cp.account("agy-1")
    cp.set_constrained("agy-1", constrained=True, reason="rate_limited")
    job = cp.create_job(project="/tmp/canary", goal="route")
    task = cp.add_task(job["id"], title="route", plan_key="route")
    assert cp.route(task["id"])["account_id"] == "agy-2"
    live = ControlPlane(tmp_path / "live-check.db")
    live.create_account(account_id="agy-1", provider="antigravity", runtime_home=next(iter(LIVE_RUNTIME_HOMES)))
    held = live.create_job(project="/tmp/canary", goal="live")
    row = live.add_task(held["id"], title="live", plan_key="live")
    with pytest.raises(ControlPlaneError) as refused:
        LocalWorker(live, tmp_path / "nope").launch(row["id"], repo=repo(tmp_path / "other"), argv=["python3", "-c", "print('no')"])
    assert refused.value.code == "live_runtime"
    drained = dispatch(cp, "drain_profile", {"account_id": "agy-2"})
    assert drained["drained"] == 1
    with pytest.raises(ControlPlaneError):
        cp.route(task["id"])
    enabled = dispatch(cp, "enable_profile", {"account_id": "agy-2"})
    assert enabled["enabled"] == 1
    assert dispatch(cp, "profiles")["profiles"]
    with pytest.raises(ControlPlaneError):
        dispatch(cp, "shell", {})
