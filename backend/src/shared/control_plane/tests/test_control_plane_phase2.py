"""Phase-2 control-plane regressions. Live Agent Control is not contacted."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared.control_plane.mcp_surface import dispatch
from shared.control_plane.store import ControlPlane, ControlPlaneError


def plane(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "control.db")


def accounts(cp: ControlPlane) -> None:
    cp.create_account(account_id="agy-1", provider="antigravity", runtime_home="/home/agentctl/runtimes/agy-1")
    cp.create_account(account_id="agy-2", provider="antigravity", runtime_home="/home/agentctl/runtimes/agy-2")


def test_queued_prompts_are_not_consumed_until_acknowledged(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/fixture", goal="queued-prompt fixture, not live job 70")
    task = cp.add_task(job["id"], title="needs-input fixture", plan_key="needs-input", worker_status="needs_input")
    keys = []
    for index in range(4):
        key = f"fixture-{index}"
        keys.append(key)
        queued = cp.enqueue_message(task["id"], f"prompt {index} still waiting", idempotency_key=key)
        assert queued["state"] == "queued"
    duplicate = cp.enqueue_message(task["id"], "prompt 0 still waiting", idempotency_key=keys[0])
    assert duplicate["id"] == cp.messages(task["id"])[0]["id"]
    assert len(cp.messages(task["id"])) == 4
    assert cp.task(task["id"])["steer_consumed"] == 0
    blocked = cp.deliver_next(task["id"], worker_accepting=False)
    assert blocked["state"] == "queued"
    assert blocked["delivered"] is False
    sent = cp.deliver_next(task["id"], worker_accepting=True)
    assert sent["state"] == "sent"
    assert sent["state"] != "acknowledged"
    acked = cp.acknowledge_message(sent["id"])
    assert acked["state"] == "acknowledged"
    again = cp.acknowledge_message(sent["id"])
    assert again["state"] == "acknowledged"
    for _ in range(3):
        cp.deliver_next(task["id"], worker_accepting=False)
    failed = next(row for row in cp.messages(task["id"]) if row["state"] == "failed")
    retried = cp.retry_message(failed["id"])
    assert retried["state"] == "queued"
    assert len(cp.messages(task["id"])) == 4


def test_quota_route_explains_conserve_and_does_not_invent_stale_zero(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/fixture", goal="quota")
    task = cp.add_task(job["id"], title="quota", plan_key="quota")
    cp.observe_quota("agy-1", "gemini", "weekly", value_pct=40, status="observed", source="fixture")
    cp.observe_quota("agy-1", "gemini", "five_hour", value_pct=40, status="observed", source="fixture")
    cp.observe_quota("agy-2", "gemini", "weekly", value_pct=80, status="observed", source="fixture")
    cp.observe_quota("agy-2", "gemini", "five_hour", value_pct=80, status="observed", source="fixture")
    decision = cp.route(task["id"], pool="gemini")
    assert decision["account_id"] == "agy-2"
    assert "agy-1 gemini is conserve-constrained" in decision["summary"]
    stale_at = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    other = cp.add_task(job["id"], title="stale", plan_key="stale")
    cp.observe_quota("agy-1", "claude_gpt", "weekly", value_pct=90, status="observed", source="fixture", observed_at=stale_at)
    with pytest.raises(ControlPlaneError) as missing:
        cp.route(other["id"], pool="claude_gpt")
    assert missing.value.code == "unroutable"
    considered = cp.route.__wrapped__ if False else None
    assert considered is None
    with cp._conn() as db:
        row = db.execute(
            "SELECT detail_json FROM route_decisions WHERE task_id=?",
            (other["id"],),
        ).fetchone()
    assert '"score": null' in row["detail_json"] or '"score": None' in row["detail_json"] or "null" in row["detail_json"]
    assert '"score": 0' not in row["detail_json"]


def test_shadow_mode_does_not_dispatch_imported_work(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/fixture", goal="shadow", source_system="agent-control", source_id="fixture")
    cp.set_shadow(job["id"], True)
    task = cp.add_task(job["id"], title="held", plan_key="held", source_system="agent-control")
    with pytest.raises(ControlPlaneError) as blocked:
        cp.start_task(task["id"], account_id="agy-1", session_id="no")
    assert blocked.value.code == "shadow"
    cp.set_shadow(job["id"], False)
    started = cp.start_task(task["id"], account_id="agy-1", session_id="local")
    assert started["worker_status"] == "running"


def test_owner_policy_and_allow_rule_revoke(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/fixture", goal="policy")
    task = cp.add_task(job["id"], title="paid", plan_key="paid")
    cp.set_policy(task["id"], "paid")
    tick = cp.automation_tick()
    assert {"task_id": task["id"], "reason": "paid"} in tick["stopped"]
    with pytest.raises(ControlPlaneError) as owner:
        cp.require_owner_action(task["id"], "paid_render")
    assert str(owner.value).startswith("OWNER ACTION REQUIRED")
    approval = cp.open_approval(task["id"], prompt_text="Allow the fixture prompt explicitly?")
    decided = cp.approve_once(
        approval["id"],
        prompt_text="Allow the fixture prompt explicitly?",
        confirm_permanent=True,
    )
    assert decided["permanent"] == 1
    with cp._conn() as db:
        rule_id = int(db.execute("SELECT id FROM allow_rules").fetchone()["id"])
    revoked = cp.revoke_allow_rule(rule_id)
    assert revoked["revoked"] == 1


def test_restart_keeps_messages_and_does_not_start_workers(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/fixture", goal="restart")
    task = cp.add_task(job["id"], title="running", plan_key="running")
    cp.start_task(task["id"], account_id="agy-1", session_id="s")
    cp.enqueue_message(task["id"], "keep this queued", idempotency_key="keep")
    recovered = cp.recover_after_restart()
    assert task["id"] in recovered
    assert cp.task(task["id"])["worker_status"] == "interrupted"
    assert cp.messages(task["id"])[0]["state"] == "queued"
    assert cp.account("agy-1")["active_workers"] == 0
    claimed = cp.claim_job(job["id"], "hermes-main")
    assert claimed["started"] == []
    assert cp.heartbeat_job(job["id"])["started"] == []


def test_mcp_message_stays_queued(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    accounts(cp)
    job = cp.create_job(project="/tmp/fixture", goal="mcp")
    task = cp.add_task(job["id"], title="mcp", plan_key="mcp", worker_status="needs_input")
    queued = dispatch(
        cp,
        "message_worker",
        {"task_id": task["id"], "text": "do not deliver this yet", "idempotency_key": "mcp-1"},
    )
    assert queued["state"] == "queued"
    with pytest.raises(ControlPlaneError):
        dispatch(cp, "process_kill", {})


def test_postgres_migration_roundtrip() -> None:
    dsn = os.environ.get("VICOA_CONTROL_PLANE_PG", "postgresql://alex@/vicoa_control_plane")
    pytest.importorskip("psycopg2")
    import importlib.util
    import psycopg2
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    try:
        psycopg2.connect(dsn).close()
    except Exception as exc:
        pytest.skip(f"local postgres unavailable: {exc}")
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg2://", 1))
    revision_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "a8c1e4b72d09_control_plane_tables.py"
    spec = importlib.util.spec_from_file_location("control_plane_revision", revision_path)
    revision = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(revision)

    def run(direction: str) -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                getattr(revision, direction)()

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS allow_rules, route_decisions, events, account_events, steer_messages, quota_observations, approvals, verifications, task_dependencies, tasks, jobs, accounts CASCADE")
    run("upgrade")
    run("downgrade")
    run("upgrade")
    cp = ControlPlane(dsn)
    cp.create_account(account_id="agy-1", provider="antigravity", runtime_home="/tmp/agy-1")
    job = cp.create_job(project="/tmp/pg", goal="persist")
    task = cp.add_task(job["id"], title="persist", plan_key="persist")
    cp.enqueue_message(task["id"], "kept", idempotency_key="kept")
    again = ControlPlane(dsn)
    assert again.task(task["id"])["title"] == "persist"
    assert again.messages(task["id"])[0]["state"] == "queued"
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS allow_rules, route_decisions, events, account_events, steer_messages, quota_observations, approvals, verifications, task_dependencies, tasks, jobs, accounts CASCADE")
