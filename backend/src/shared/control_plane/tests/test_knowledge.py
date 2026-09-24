"""Skills, memory, context budgets, and session handoff."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from shared.control_plane.canary import run_canary
from shared.control_plane.knowledge import canonical, digest
from shared.control_plane.mcp_surface import dispatch
from shared.control_plane.store import ControlPlane, ControlPlaneError


def plane(tmp_path: Path) -> ControlPlane:
    cp = ControlPlane(tmp_path / "cp.db")
    cp.create_account(account_id="canary-a", provider="local", runtime_home=str(tmp_path / "a"))
    cp.create_account(account_id="canary-b", provider="local", runtime_home=str(tmp_path / "b"))
    return cp


def task(cp: ControlPlane, *, title: str = "work", prompt: str = "") -> int:
    job = cp.create_job(project="canary-project", goal="ship the check")
    row = cp.add_task(job["id"], title=title, plan_key=title, prompt=prompt, acceptance_criteria=prompt)
    return int(row["id"])


def skill(cp: ControlPlane, *, body: str = "Check the marker file.", risk: str = "low", source: str = "manual", learned_from: int | None = None) -> dict:
    return cp.knowledge.register_skill(
        skill_key="check-marker",
        name="Check marker",
        description="Verify the local marker",
        body=body,
        scope="global",
        risk=risk,
        source=source,
        learned_from_task_id=learned_from,
        required_secret_names=["MARKER_TOKEN_NAME"],
    )


def test_skill_cannot_activate_without_canary_and_blocked_stays_blocked(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    row = skill(cp)
    with pytest.raises(ControlPlaneError) as missing:
        cp.knowledge.activate_skill(row["id"])
    assert missing.value.code == "canary_required"
    cp.knowledge.record_canary(row["id"], passed=True, evidence={"summary": "marker existed"})
    active = cp.knowledge.activate_skill(row["id"])
    assert active["status"] == "active"
    blocked = skill(cp, body="never run this", risk="blocked")
    with pytest.raises(ControlPlaneError) as refused:
        cp.knowledge.activate_skill(blocked["id"])
    assert refused.value.code == "risk_blocked"


def test_rollback_restores_previous_and_quarantine_blocks(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    first = skill(cp, body="version one")
    cp.knowledge.record_canary(first["id"], passed=True, evidence={"summary": "v1 passed"})
    cp.knowledge.activate_skill(first["id"])
    second = skill(cp, body="version two")
    cp.knowledge.record_canary(second["id"], passed=True, evidence={"summary": "v2 passed"})
    cp.knowledge.activate_skill(second["id"])
    cp.knowledge.rollback_skill(second["id"], reason="v2 failed in review")
    task_id = task(cp)
    resolved = cp.knowledge.resolve_skills(task_id)
    assert resolved["active"][0]["version"] == 1
    assert cp.knowledge.skill(second["id"])["status"] == "rolled_back"
    cp.knowledge.quarantine_skill(first["id"], reason="bad instruction")
    with pytest.raises(ControlPlaneError) as blocked:
        cp.knowledge.activate_skill(first["id"], owner_confirmed=True)
    assert blocked.value.code == "quarantined"


def test_secrets_are_redacted_and_rejected(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    row = skill(cp, body="token sk_live_supersecretvalue")
    assert "sk_live_" not in row["body"]
    assert row["secrets_removed"] == 1
    cp.knowledge.record_canary(row["id"], passed=True, evidence={"summary": "redacted"})
    with pytest.raises(ControlPlaneError) as confirm:
        cp.knowledge.activate_skill(row["id"])
    assert confirm.value.code == "owner_confirmation_required"
    with pytest.raises(ControlPlaneError):
        cp.knowledge.register_skill(
            skill_key="bad-secret",
            name="bad",
            description="bad",
            body="no",
            required_secret_names=["sk_live_anothersecret"],
        )
    dumped = json.dumps(cp.knowledge.events())
    assert "sk_live_" not in dumped


def test_memory_authority_and_unresolved_conflicts(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    owner = cp.knowledge.remember(topic="deploy", statement="ship from main only", authority="owner", scope="project", scope_ref="canary-project")
    rejected = cp.knowledge.remember(topic="deploy", statement="ship from any branch", authority="untrusted_content", scope="project", scope_ref="canary-project")
    assert owner["status"] == "active"
    assert rejected["applied"] == "rejected"
    assert rejected["conflict"]["resolution"] == "untrusted_rejected"
    inferred = cp.knowledge.remember(topic="port", statement="port is 8080", authority="agent_inference", scope="project", scope_ref="canary-project")
    verified = cp.knowledge.remember(
        topic="port",
        statement="port is 8787",
        authority="verified_evidence",
        scope="project",
        scope_ref="canary-project",
        evidence=["probe"],
    )
    assert verified["applied"] == "superseded"
    assert cp.knowledge.list_memories()[inferred["id"] - 1]["status"] == "superseded"
    conflicted = cp.knowledge.remember(topic="runner", statement="use pytest", authority="owner", scope="project", scope_ref="canary-project")
    other = cp.knowledge.remember(topic="runner", statement="use unittest", authority="owner", scope="project", scope_ref="canary-project")
    assert other["applied"] == "conflict"
    assert other["conflict"]["resolution"] == "unresolved"
    assert cp.knowledge.list_memories()[conflicted["id"] - 1]["status"] == "disputed"
    task_id = task(cp)
    pack = cp.knowledge.build_context(task_id, budget_tokens=2000)
    text = json.dumps(pack["sections"])
    assert "ship from main only" in text
    assert "ship from any branch" not in text
    assert "port is 8787" in text
    assert "port is 8080" not in text
    assert "use pytest" not in text
    assert "use unittest" not in text


def test_context_budget_omits_raw_logs_and_stale_memory(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    task_id = task(cp, prompt="A" * 800)
    cp.route(task_id)
    cp.start_task(task_id, account_id="canary-a", session_id="s-1")
    cp.complete_worker(task_id, output="RAW_WORKER_LOG_MARKER")
    cp.knowledge.remember(
        topic="old-note",
        statement="this expired",
        authority="owner",
        scope="project",
        scope_ref="canary-project",
        expires_at="2000-01-01T00:00:00Z",
    )
    pack = cp.knowledge.build_context(task_id, budget_tokens=48)
    assert pack["used_tokens"] <= 48
    assert any(item["reason"] in {"over_budget", "truncated_over_budget"} for item in pack["omitted"])
    assert any(item["reason"] == "stale" for item in pack["omitted"])
    assert "RAW_WORKER_LOG_MARKER" not in json.dumps(pack["sections"])
    compact = cp.knowledge.compact_context(pack["id"], budget_tokens=40)
    assert compact["used_tokens"] <= 40
    assert any(event["event_type"] == "context_compacted" for event in cp.knowledge.timeline(task_id))


def test_handoff_validation_resume_and_protected_override(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    task_id = task(cp)
    cp.route(task_id)
    cp.start_task(task_id, account_id="canary-a", session_id="old-session")
    prepared = cp.knowledge.prepare_handoff(task_id, reason="manual")
    assert "fresh session" in prepared["packet"]["next_action"]
    assert "do not repeat completed work" in prepared["packet"]["next_action"]
    with pytest.raises(ControlPlaneError) as running:
        cp.knowledge.resume_from_handoff(prepared["id"], new_session_id="new-session")
    assert running.value.code == "worker_running"
    assert cp.task(task_id)["worker_status"] == "running"
    pressure = cp.knowledge.note_pressure(task_id, used_tokens=90, budget_tokens=100)
    assert pressure["kept_healthy_session"] is True
    assert pressure["killed"] is False
    cp.knowledge.record_attempt(task_id, kind="command", name="pytest", status="passed", detail={"exit_code": 0, "stdout": "should not store"})
    cp.complete_worker(task_id, output="RAW_WORKER_LOG_MARKER")
    cp.verify_task(task_id, [{"type": "worker_output", "contains": "RAW_WORKER_LOG_MARKER"}])
    packet = cp.knowledge.prepare_handoff(task_id, reason="verification_failure")
    assert any("Do not repeat passed verification" in line for line in packet["packet"]["resume_instructions"])
    assert "stdout" not in json.dumps(packet["packet"]["commands_run"])
    resumed = cp.knowledge.resume_from_handoff(packet["id"], new_session_id="new-session")
    assert resumed["launched"] is False
    assert resumed["task_session_id"] == "old-session"
    assert "RAW_WORKER_LOG_MARKER" not in json.dumps(cp.knowledge.handoff(packet["id"])["packet"])
    with pytest.raises(ControlPlaneError) as reused:
        cp.start_task(task_id, account_id="canary-b", session_id="old-session")
    assert reused.value.code == "session_reused"
    started = cp.start_task(task_id, account_id="canary-b", session_id="new-session")
    assert started["worker_status"] == "running"
    assert started["session_id"] == "new-session"

    held = task(cp, title="held")
    cp.protect(held, "fixture")
    with pytest.raises(ControlPlaneError) as protected:
        cp.knowledge.prepare_handoff(held, reason="manual")
    assert protected.value.code == "protected"
    allowed = cp.knowledge.prepare_handoff(held, reason="owner_pause", override_reason="owner reviewed the packet")
    assert allowed["status"] == "prepared"
    assert any("protected override" in event["message"] for event in cp.knowledge.timeline(held))


def test_tampered_handoff_fails_closed(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    task_id = task(cp)
    packet = cp.knowledge.prepare_handoff(task_id, reason="manual")
    broken = dict(packet["packet"])
    broken.pop("next_action")
    with sqlite3.connect(cp.path) as db:
        db.execute(
            "UPDATE handoff_packets SET packet_json=?, packet_hash=? WHERE id=?",
            (canonical(broken), digest(broken), packet["id"]),
        )
    invalid = cp.knowledge.validate_handoff(packet["id"])
    assert invalid["ok"] is False
    assert any(item.startswith("missing:") for item in invalid["errors"])
    secret = dict(packet["packet"])
    secret["objective"] = {"goal": "sk_live_tamperedsecret", "title": "x", "acceptance": ""}
    with sqlite3.connect(cp.path) as db:
        db.execute(
            "UPDATE handoff_packets SET packet_json=?, packet_hash=?, status='prepared' WHERE id=?",
            (canonical(secret), digest(secret), packet["id"]),
        )
    with pytest.raises(ControlPlaneError) as refused:
        cp.knowledge.resume_from_handoff(packet["id"], new_session_id="fresh-session")
    assert refused.value.code == "handoff_invalid"
    assert "secret_present" in refused.value.args[0]


def test_learned_skill_waits_for_verification(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    task_id = task(cp)
    cp.route(task_id)
    cp.start_task(task_id, account_id="canary-a", session_id="learn")
    cp.complete_worker(task_id, output="learned")
    row = skill(cp, body="learned check", source="learned", learned_from=task_id)
    cp.knowledge.record_canary(row["id"], passed=True, evidence={"summary": "canary passed"})
    with pytest.raises(ControlPlaneError) as waiting:
        cp.knowledge.activate_skill(row["id"])
    assert waiting.value.code == "learned_unverified"
    cp.verify_task(task_id, [{"type": "worker_output", "contains": "learned"}])
    assert cp.knowledge.activate_skill(row["id"])["status"] == "active"


def test_route_and_mcp_views_explain_without_shell(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    row = skill(cp)
    cp.knowledge.record_canary(row["id"], passed=True, evidence={"summary": "ready"})
    cp.knowledge.activate_skill(row["id"])
    cp.knowledge.remember(topic="rule", statement="keep the budget", authority="owner", scope="global", scope_ref="")
    task_id = task(cp)
    decision = cp.route(task_id)
    kinds = {item["kind"] for item in decision["explanations"]}
    assert {"provider", "account", "quota", "skill", "memory", "context"} <= kinds
    assert decision["considered"][0]["account_id"]
    listed = dispatch(cp, "skills", {})
    assert listed["skills"][0]["skill_key"] == "check-marker"
    assert "body" not in listed["skills"][0]
    assert dispatch(cp, "timeline", {"task_id": task_id})["events"]
    with pytest.raises(ControlPlaneError) as shell:
        dispatch(cp, "shell", {})
    assert shell.value.code == "forbidden"


def test_crash_prepares_a_handoff_without_launching(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    task_id = task(cp)
    cp.route(task_id)
    cp.start_task(task_id, account_id="canary-a", session_id="live")
    crashed = cp.crash_worker(task_id)
    assert crashed["worker_status"] == "interrupted"
    packets = cp.knowledge.list_handoffs(task_id)
    assert packets[0]["reason"] == "crash"
    assert packets[0]["status"] == "prepared"
    assert cp.task(task_id)["session_id"] == "live"


def test_domain_routing_excludes_unrelated_skills(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    php = cp.knowledge.register_skill(
        skill_key="wp-signature",
        name="WP signature",
        description="Verify a WordPress PHP signature",
        body="Check the signature before recording state.",
        domains=["php", "wordpress", "tillpress", "security", "verification"],
        compatible_agents=["any"],
    )
    roblox = cp.knowledge.register_skill(
        skill_key="roblox-motion",
        name="Roblox motion",
        description="Roblox only",
        body="Do not use this outside Roblox.",
        domains=["roblox"],
        compatible_agents=["any"],
    )
    for row in (php, roblox):
        cp.knowledge.record_canary(row["id"], passed=True, evidence={"summary": "classified"})
        cp.knowledge.activate_skill(row["id"])
    till = cp.create_job(project="tillpress", goal="verify a PHP signature")
    task_a = cp.add_task(till["id"], title="Tillpress PHP signature verification", plan_key="a", prompt="verify the license signature")
    selected = cp.knowledge.resolve_skills(task_a["id"])
    assert php["id"] in selected["selected_ids"]
    assert roblox["id"] not in selected["selected_ids"]
    assert any(item["reason"] == "domain_excluded" and item["skill_key"] == "roblox-motion" for item in selected["skipped"])
    other = cp.create_job(project="roblox-game", goal="animate a Roblox character")
    task_b = cp.add_task(other["id"], title="Roblox animation", plan_key="b", prompt="roblox motion only")
    second = cp.knowledge.resolve_skills(task_b["id"])
    assert php["id"] not in second["selected_ids"]
    assert roblox["id"] in second["selected_ids"]
    generic = cp.create_job(project="generic", goal="run deterministic verification")
    task_c = cp.add_task(
        generic["id"],
        title="Generic verification",
        plan_key="c",
        prompt="verify two marker files deterministically",
    )
    third = cp.knowledge.resolve_skills(task_c["id"])
    assert php["id"] not in third["selected_ids"]
    assert any(
        item["reason"] == "domain_excluded" and item["skill_key"] == "wp-signature"
        for item in third["skipped"]
    )
    result = run_canary(tmp_path / "canary.db")
    assert result["passed"], result["checks"]
    with pytest.raises(ControlPlaneError):
        run_canary(Path("/opt/agent-control/state/canary.db"))


def test_postgres_knowledge_migration_roundtrip() -> None:
    import importlib.util
    import os

    pytest.importorskip("psycopg2")
    import psycopg2
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    dsn = os.environ.get("VICOA_CONTROL_PLANE_PG", "postgresql://alex@/vicoa_control_plane")
    try:
        psycopg2.connect(dsn).close()
    except psycopg2.Error as exc:
        pytest.skip(f"local postgres unavailable: {exc}")
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg2://", 1))
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"

    def load(name: str):
        path = next(versions.glob(f"{name}_*.py"))
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    base = load("a8c1e4b72d09")
    knowledge = load("b4e7c2a91d18")
    drop = (
        "DROP TABLE IF EXISTS work_attempts, plane_events, handoff_packets, context_packs, "
        "memory_conflicts, memories, skill_activations, skill_versions, allow_rules, "
        "route_decisions, events, account_events, steer_messages, quota_observations, "
        "approvals, verifications, task_dependencies, tasks, jobs, accounts CASCADE"
    )

    def run(module, direction: str) -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                getattr(module, direction)()

    with engine.begin() as conn:
        conn.exec_driver_sql(drop)
    run(base, "upgrade")
    run(knowledge, "upgrade")
    run(knowledge, "downgrade")
    run(knowledge, "upgrade")
    cp = ControlPlane(dsn)
    row = cp.knowledge.register_skill(
        skill_key="pg-check",
        name="PG check",
        description="persists",
        body="persist the skill",
        scope="global",
    )
    again = ControlPlane(dsn)
    assert again.knowledge.skill(row["id"])["skill_key"] == "pg-check"
    with engine.begin() as conn:
        conn.exec_driver_sql(drop)


def test_rolled_session_cannot_start_again(tmp_path: Path) -> None:
    cp = plane(tmp_path)
    task_id = task(cp)
    cp.route(task_id)
    cp.start_task(task_id, account_id="canary-a", session_id="session-a")
    cp.knowledge.open_session(task_id, "session-a", account_id="canary-a", provider="antigravity")
    cp.knowledge.prepare_handoff(task_id, reason="context_pressure")
    cp.knowledge.mark_rolled_over("session-a")
    with pytest.raises(ControlPlaneError) as blocked:
        cp.start_task(task_id, account_id="canary-a", session_id="session-a")
    assert blocked.value.code == "session_reused"
