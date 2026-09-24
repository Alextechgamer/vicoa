"""Control-plane regression tests. No live Agent Control mutation."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from shared.control_plane.mcp_surface import REJECTED, dispatch
from shared.control_plane.store import (
    LIVE_AGENT_CONTROL_DB,
    ControlPlane,
    ControlPlaneError,
    redact,
)


def plane(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "cp.db")


def accounts(cp: ControlPlane) -> None:
    cp.create_account(account_id="agy-1", provider="antigravity", runtime_home="/home/agentctl/runtimes/agy-1")
    cp.create_account(account_id="agy-2", provider="antigravity", runtime_home="/home/agentctl/runtimes/agy-2")


def dag(cp: ControlPlane) -> tuple[int, int, int]:
    job = cp.create_job(project="/tmp/canary", goal="A then B then C")
    a = cp.add_task(job["id"], title="A", plan_key="A")
    b = cp.add_task(job["id"], title="B", plan_key="B")
    c = cp.add_task(job["id"], title="C", plan_key="C")
    cp.add_dependency(b["id"], a["id"])
    cp.add_dependency(c["id"], b["id"])
    return a["id"], b["id"], c["id"]


def test_secret_redaction_does_not_corrupt_benign_skill_keys() -> None:
    assert redact("vicoa-task-decomposition") == "vicoa-task-decomposition"
    assert redact("prefix sk-1234567890 suffix") == "prefix [redacted] suffix"
    assert redact("token=topsecret") == "[redacted]"


def test_two_profiles_are_independent_and_secrets_stay_out(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    assert {row["id"] for row in cp.accounts()} == {"agy-1", "agy-2"}
    job = cp.create_job(project="/tmp/a", goal="parallel")
    left = cp.add_task(job["id"], title="left", plan_key="left")
    right = cp.add_task(job["id"], title="right", plan_key="right")
    cp.route(left["id"])
    cp.route(right["id"])
    cp.start_task(left["id"], account_id="agy-1", session_id="s-1")
    cp.start_task(right["id"], account_id="agy-2", session_id="s-2")
    assert cp.task(left["id"])["session_id"] != cp.task(right["id"])["session_id"]
    dumped = str(cp.status())
    assert "sk_live_" not in dumped
    assert "/home/agentctl/runtimes" not in dumped
    with pytest.raises(ControlPlaneError):
        cp.create_account(account_id="bad", provider="antigravity", runtime_home="/tmp/x", token="secret")


def test_constraint_on_one_account_does_not_disable_the_other(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    cp.set_constrained("agy-1", constrained=True, reason="rate_limited")
    job = cp.create_job(project="/tmp/a", goal="route")
    task = cp.add_task(job["id"], title="work", plan_key="work")
    decision = cp.route(task["id"])
    assert decision["account_id"] == "agy-2"
    assert cp.account("agy-2")["enabled"] == 1
    assert cp.account("agy-1")["enabled"] == 1


def test_dependency_waits_for_verification_not_worker_claim(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    a, b, c = dag(cp)
    with pytest.raises(ControlPlaneError) as blocked:
        cp.start_task(b, account_id="agy-1", session_id="b")
    assert blocked.value.code == "dependency"
    cp.start_task(a, account_id="agy-1", session_id="a")
    cp.complete_worker(a, worktree="/tmp/a", output="claimed done")
    with pytest.raises(ControlPlaneError):
        cp.start_task(b, account_id="agy-2", session_id="b")
    root = tmp_path / "work"
    root.mkdir()
    (root / "marker.txt").write_text("ALPHA-MARKER")
    cp.verify_task(a, [{"type": "file_exists", "path": "marker.txt"}, {"type": "file_contains", "path": "marker.txt", "text": "ALPHA-MARKER"}], worktree=root)
    cp.start_task(b, account_id="agy-2", session_id="b")
    cp.complete_worker(b, worktree=str(root), output="b done")
    with pytest.raises(ControlPlaneError):
        cp.start_task(c, account_id="agy-1", session_id="c")
    cp.verify_task(b, [{"type": "worker_output", "contains": "b done"}])
    cp.start_task(c, account_id="agy-1", session_id="c")


def test_verification_checks(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "canary@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "canary"], cwd=root, check=True)
    (root / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

    def task(key: str) -> int:
        job = cp.create_job(project=str(root), goal=key)
        row = cp.add_task(job["id"], title=key, plan_key=key)
        cp.start_task(row["id"], account_id="agy-1", session_id=key)
        cp.complete_worker(row["id"], output="worker said READY-MARKER")
        with cp._conn() as db:
            db.execute("UPDATE tasks SET base_ref=? WHERE id=?", (base, row["id"]))
        return row["id"]

    missing = tmp_path / "empty"
    missing.mkdir()
    failed = cp.verify_task(task("missing"), [{"type": "file_exists", "path": "marker.txt"}], worktree=missing)
    assert failed["verification_status"] == "revision_required"
    (root / "marker.txt").write_text("EXACT-MARKER\n")
    bad_text = cp.verify_task(task("text"), [{"type": "file_contains", "path": "marker.txt", "text": "nope"}], worktree=root)
    assert bad_text["verification_status"] == "revision_required"
    found = cp.verify_task(task("exists"), [{"type": "file_exists", "path": "marker.txt"}], worktree=root)
    assert found["verification_status"] == "passed"
    exact = cp.verify_task(task("exact"), [{"type": "file_contains", "path": "marker.txt", "text": "EXACT-MARKER"}], worktree=root)
    assert exact["verification_status"] == "passed"
    unchanged = cp.verify_task(task("no-diff"), [{"type": "git_changed", "path": "missing.txt"}], worktree=root)
    assert unchanged["verification_status"] == "revision_required"
    subprocess.run(["git", "add", "marker.txt"], cwd=root, check=True)
    changed = cp.verify_task(task("diff"), [{"type": "git_changed", "path": "marker.txt"}], worktree=root)
    assert changed["verification_status"] == "passed"
    cmd = cp.verify_task(task("cmd"), [{"type": "command", "argv": ["python3", "-c", "print('ok')"], "timeout_seconds": 5}], worktree=root)
    assert cmd["verification_status"] == "passed"
    timed = cp.verify_task(
        task("slow"),
        [{"type": "command", "argv": ["python3", "-c", "import time; time.sleep(5)"], "timeout_seconds": 1}],
        worktree=root,
    )
    assert timed["verification_status"] == "revision_required"
    assert timed["evidence"]["checks"][0]["summary"] == "timeout"
    output = cp.verify_task(task("out"), [{"type": "worker_output", "contains": "READY-MARKER"}])
    assert output["verification_status"] == "passed"
    again = cp.verify_task(output["task_id"], [{"type": "command", "argv": ["python3", "-c", "raise SystemExit('should not run')"]}])
    assert again["idempotent"] is True
    with pytest.raises(ControlPlaneError):
        cp.verify_task(task("escape"), [{"type": "file_exists", "path": "../etc/passwd"}], worktree=root)


def test_approvals(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/a", goal="approve")
    task = cp.add_task(job["id"], title="approve", plan_key="approve")
    prompt = "Allow writing marker.txt in the canary worktree?"
    approval = cp.open_approval(task["id"], prompt_text=prompt)
    with pytest.raises(ControlPlaneError) as ambiguous:
        cp.approve_once(approval["id"], prompt_text="yes")
    assert ambiguous.value.code == "ambiguous"
    decided = cp.approve_once(approval["id"], prompt_text=prompt)
    assert decided["consumed"] == 1
    assert decided["permanent"] == 0
    with cp._conn() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM allow_rules").fetchone()["n"] == 0
    with pytest.raises(ControlPlaneError):
        cp.approve_once(approval["id"], prompt_text=prompt)
    other = cp.open_approval(task["id"], prompt_text="Allow a different file write in the canary?")
    cp.deny(other["id"], prompt_text="Allow a different file write in the canary?")
    assert cp.approval(other["id"])["status"] == "denied"
    stale = cp.open_approval(task["id"], prompt_text="Allow writing marker.txt in the canary worktree?")
    with pytest.raises(ControlPlaneError) as stale_err:
        cp.approve_once(stale["id"], prompt_text="Allow writing a different marker in the canary worktree?")
    assert stale_err.value.code == "stale"
    explicit = cp.open_approval(task["id"], prompt_text=prompt)
    ruled = cp.approve_once(explicit["id"], prompt_text=prompt, confirm_permanent=True)
    assert ruled["permanent"] == 1
    with cp._conn() as db:
        rule = db.execute("SELECT scope, fingerprint FROM allow_rules").fetchone()
    assert rule["scope"] == "fingerprint"
    assert rule["fingerprint"] != "*"


def test_failure_recovery(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/a", goal="recover")
    task = cp.add_task(job["id"], title="recover", plan_key="recover")
    cp.start_task(task["id"], account_id="agy-1", session_id="live")
    cp.complete_worker(task["id"], output="partial")
    cp.verify_task(task["id"], [{"type": "worker_output", "contains": "partial"}])
    evidence_before = cp.task(task["id"])["evidence"]
    cp.crash_worker(task["id"])
    assert cp.task(task["id"])["worker_status"] == "interrupted"
    assert cp.task(task["id"])["evidence"] == evidence_before
    cp.mark_provider_failure("agy-1", "provider down")
    other = cp.add_task(job["id"], title="other", plan_key="other")
    assert cp.route(other["id"])["account_id"] == "agy-2"
    cp.observe_quota("agy-2", "antigravity", "weekly", value_pct=None, status="unavailable", source="not_observed")
    obs = cp.quota()[0]
    assert obs["value_pct"] is None
    assert obs["value_pct"] != 0
    cp.mark_stale(task["id"])
    cp.message(task["id"], "still waiting")
    health = cp.health()
    assert task["id"] in health["stale_sessions"]
    assert task["id"] in health["unconsumed_steers"]
    cp.recover_after_restart()
    assert cp.task(task["id"])["evidence"]
    cp.consume_steer(task["id"])
    evidence_root = tmp_path / "evidence"
    cp.cleanup_session(task["id"], evidence_root=str(evidence_root))
    assert (evidence_root / f"task-{task['id']}" / "evidence.json").exists()
    assert cp.task(task["id"])["evidence"]
    running = cp.add_task(job["id"], title="active", plan_key="active")
    cp.start_task(running["id"], account_id="agy-2", session_id="busy")
    with pytest.raises(ControlPlaneError) as active:
        cp.cleanup_session(running["id"], evidence_root=str(evidence_root))
    assert active.value.code == "active"
    retried = cp.retry_verification(task["id"], [{"type": "command", "argv": ["false"]}])
    assert retried["idempotent"] is True


def test_protected_task_cannot_be_mutated_without_override(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/e2e-demo", goal="fixture, not live task-6")
    task = cp.add_task(job["id"], title="para-B-fixture", plan_key="fixture")
    cp.protect(task["id"], reason="fixture stand-in for paused task-6")
    actions = [
        lambda: cp.resume(task["id"]),
        lambda: cp.message(task["id"], "hello"),
        lambda: cp.route(task["id"]),
        lambda: cp.kill(task["id"]),
        lambda: cp.reap(task["id"]),
        lambda: cp.approve_once(cp.open_approval(task["id"], prompt_text="Allow the fixture prompt explicitly?")["id"], prompt_text="Allow the fixture prompt explicitly?"),
        lambda: cp.mutate(task["id"], prompt="changed"),
    ]
    for action in actions:
        with pytest.raises(ControlPlaneError) as err:
            action()
        assert err.value.code == "protected"
    cp.resume(task["id"], override_reason="owner confirmed this fixture only")
    assert cp.task(task["id"])["worker_status"] == "queued"


def test_owner_only_blocker_stops_automation(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/a", goal="owner")
    task = cp.add_task(job["id"], title="owner", plan_key="owner", owner_only=True, owner_blocker="needs a human decision")
    with pytest.raises(ControlPlaneError) as err:
        cp.start_task(task["id"], account_id="agy-1", session_id="nope")
    assert err.value.code == "owner_only"
    tick = cp.automation_tick()
    assert tick["stopped"][0]["task_id"] == task["id"]
    assert cp.task(task["id"])["worker_status"] == "queued"
    assert tick["loop"] is False


def _source_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE accounts(id TEXT PRIMARY KEY, provider TEXT, profile TEXT, runtime_home TEXT, enabled INT, max_workers INT);
        CREATE TABLE jobs(id INTEGER PRIMARY KEY, project TEXT, goal TEXT, status TEXT, plan_version INT);
        CREATE TABLE tasks(
          id INTEGER PRIMARY KEY, job_id INT, plan_key TEXT, title TEXT, prompt TEXT, status TEXT,
          verification_status TEXT, account_id TEXT, atrium_session TEXT, project TEXT, acceptance_criteria TEXT,
          worktree_path TEXT, branch TEXT
        );
        CREATE TABLE task_dependencies(task_id INT, depends_on_task_id INT);
        CREATE TABLE task_verifications(task_id INT, status TEXT, evidence_json TEXT);
        INSERT INTO accounts VALUES('agy-1','antigravity','agy','/home/agentctl/runtimes/agy-1',1,1);
        INSERT INTO accounts VALUES('agy-2','antigravity','agy','/home/agentctl/runtimes/agy-2',1,1);
        INSERT INTO jobs VALUES(70,'/opt/projects/batchideo','job 70 goal','planned',1);
        INSERT INTO jobs VALUES(71,'/opt/projects/orderring','job 71 goal','planned',1);
        INSERT INTO tasks VALUES(6,NULL,NULL,'para-B','sk_live_SECRETPROMPT','paused','pending','agy-2','task-6','/opt/projects/e2e-demo','','','');
        INSERT INTO tasks VALUES(113,70,'batchideo-readiness-audit','Batchideo baseline readiness audit','prompt','needs_input','pending','agy-1','task-113','/opt/projects/batchideo','','','');
        INSERT INTO tasks VALUES(114,71,'tillpress-current-audit','Tillpress monorepo current-state audit','prompt','needs_input','pending','agy-2','task-114','/opt/projects/orderring','','','');
        INSERT INTO task_verifications VALUES(113,'pending','{"token":"sk_live_EVIDENCE"}');
        """
    )
    db.commit()
    db.close()


def test_import_is_read_only_and_preserves_holds(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _source_db(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    cp = plane(tmp_path)
    manifest = cp.import_agent_control(
        source,
        observations=[
            {"title": "task-6", "queued_prompts": 2},
            {"title": "task-113", "queued_prompts": 4},
            {"title": "task-114", "queued_prompts": 2},
        ],
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert manifest["protected_source_ids"] == [6]
    imported = {row["source_id"]: row for row in cp.tasks() if row["source_system"] == "agent-control"}
    assert imported["6"]["protected"] == 1
    assert imported["6"]["worker_status"] == "paused"
    assert imported["113"]["worker_status"] == "needs_input"
    assert imported["114"]["worker_status"] == "needs_input"
    assert "sk_live_" not in str(imported["6"]["prompt"])
    assert imported["113"]["queued_prompts"] == 4
    with pytest.raises(ControlPlaneError):
        cp.start_task(imported["113"]["id"], account_id="agy-1", session_id="no")
    before_count = len(cp.tasks())
    again = cp.import_agent_control(source)
    assert len(cp.tasks()) == before_count
    assert again["live_db_written"] is False
    with pytest.raises(ControlPlaneError):
        cp.import_agent_control(LIVE_AGENT_CONTROL_DB)


def test_mcp_rejects_shell_and_database_tools(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    for name in REJECTED:
        with pytest.raises(ControlPlaneError) as err:
            dispatch(cp, name, {})
        assert err.value.code == "forbidden"
    status = dispatch(cp, "status", {})
    assert "accounts" in status


def test_canary_lifecycle(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    work = tmp_path / "canary"
    work.mkdir()
    job = cp.create_job(project=str(work), goal="harmless marker canary")
    a = cp.add_task(job["id"], title="write marker", plan_key="A")
    b = cp.add_task(job["id"], title="append marker", plan_key="B")
    cp.add_dependency(b["id"], a["id"])
    cp.route(a["id"])
    cp.start_task(a["id"], account_id="agy-1", session_id="canary-a")
    (work / "a.txt").write_text("CANARY-A")
    cp.complete_worker(a["id"], worktree=str(work), branch="canary", output="wrote CANARY-A")
    with pytest.raises(ControlPlaneError):
        cp.start_task(b["id"], account_id="agy-2", session_id="canary-b")
    passed = cp.verify_task(
        a["id"],
        [
            {"type": "file_exists", "path": "a.txt"},
            {"type": "file_contains", "path": "a.txt", "text": "CANARY-A"},
        ],
        worktree=work,
    )
    assert passed["verification_status"] == "passed"
    cp.route(b["id"])
    cp.start_task(b["id"], account_id="agy-2", session_id="canary-b")
    (work / "b.txt").write_text("CANARY-B")
    cp.complete_worker(b["id"], worktree=str(work), output="wrote CANARY-B")
    second = cp.verify_task(
        b["id"],
        [{"type": "file_contains", "path": "b.txt", "text": "CANARY-B"}],
        worktree=work,
    )
    assert second["verification_status"] == "passed"
    assert cp.task(a["id"])["account_id"] == "agy-1"
    assert cp.task(b["id"])["account_id"] == "agy-2"
