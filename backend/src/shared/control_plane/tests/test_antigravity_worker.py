"""Automatic Antigravity rollover, restart recovery, and message continuity."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from shared.control_plane.antigravity_worker import AntigravityTaskWorker
from shared.control_plane.store import ControlPlane, ControlPlaneError


class FakeSession:
    counter = 0
    starts: ClassVar[list[FakeSession]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.cwd = Path(kwargs["cwd"])
        self.process_env = dict(kwargs["process_env"])
        self.system_prompt = kwargs.get("system_prompt")
        self.conversation_id: str | None = None
        self.context_used_tokens: int | None = None
        self.delivered: list[str] = []
        self.closed = False

    async def start(self) -> None:
        type(self).counter += 1
        self.conversation_id = f"real-session-{type(self).counter}"
        type(self).starts.append(self)

    async def deliver_user_message(self, text: str) -> None:
        self.delivered.append(text)
        self.context_used_tokens = 100
        if "PHASE A" in text:
            marker = self.cwd / "phase-a.txt"
            if marker.exists():
                raise AssertionError("Session B repeated completed Phase A")
            marker.write_text("PHASE-A-OK\n")
        if "PHASE B" in text:
            marker = self.cwd / "phase-a.txt"
            assert marker.read_text() == "PHASE-A-OK\n"
            (self.cwd / "phase-b.txt").write_text("PHASE-B-OK\n")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_sessions() -> None:
    FakeSession.counter = 0
    FakeSession.starts = []


def plane(tmp_path: Path) -> tuple[ControlPlane, int, Path]:
    cp = ControlPlane(tmp_path / "control-plane.db")
    legacy_a = tmp_path / "legacy-a"
    legacy_b = tmp_path / "legacy-b"
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    for path in (legacy_a, legacy_b, profile_a, profile_b):
        path.mkdir()
    cp.create_account(
        account_id="agy-1", provider="antigravity", runtime_home=str(legacy_a)
    )
    cp.create_account(
        account_id="agy-2", provider="antigravity", runtime_home=str(legacy_b)
    )
    cp.drain("agy-1")
    cp.drain("agy-2")
    cp.create_account(
        account_id="vicoa-agy-1",
        provider="antigravity",
        runtime_home=str(profile_a),
    )
    cp.create_account(
        account_id="vicoa-agy-2",
        provider="antigravity",
        runtime_home=str(profile_b),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    job = cp.create_job(project=str(repo), goal="prove automatic rollover")
    task = cp.add_task(
        int(job["id"]),
        title="two-phase rollover canary",
        plan_key="rollover",
        prompt="complete Phase A, roll over, then complete Phase B",
        project=str(repo),
        acceptance_criteria="both marker files exist without repeated work",
    )
    return cp, int(task["id"]), repo


def worker(
    cp: ControlPlane,
    task_id: int,
    repo: Path,
    *,
    callback=None,
) -> AntigravityTaskWorker:
    return AntigravityTaskWorker(
        cp,
        task_id,
        cwd=repo,
        session_factory=FakeSession,
        initial_context_budget=240,
        rollover_budget_tokens=120,
        max_rollovers=1,
        on_handoff_prepared=callback,
    )


@pytest.mark.asyncio
async def test_automatic_rollover_preserves_work_and_messages(
    tmp_path: Path,
) -> None:
    cp, task_id, repo = plane(tmp_path)

    def during_handoff(_packet: dict[str, Any]) -> None:
        cp.enqueue_message(task_id, "during-handoff", idempotency_key="during-handoff")
        cp.drain("vicoa-agy-1")

    runner = worker(cp, task_id, repo, callback=during_handoff)
    started = await runner.start(account_id="vicoa-agy-1")
    session_a = started["session_id"]
    assert started["account_id"] == "vicoa-agy-1"
    assert FakeSession.starts[0].process_env["HOME"].endswith("profile-a")

    cp.enqueue_message(task_id, "ack-me", idempotency_key="ack-me")
    cp.enqueue_message(task_id, "keep-me", idempotency_key="keep-me")
    assert await runner.deliver_queued(limit=1) == ["ack-me"]

    rolled = await runner.deliver("PHASE A: create phase-a.txt")
    assert rolled["rolled_over"] is True
    session_b = rolled["to_session_id"]
    assert session_a != session_b
    assert rolled["account_id"] == "vicoa-agy-2"
    assert FakeSession.starts[1].process_env["HOME"].endswith("profile-b")
    assert (repo / "phase-a.txt").read_text() == "PHASE-A-OK\n"

    packets = cp.knowledge.list_handoffs(task_id)
    assert len(packets) == 1
    packet = packets[0]
    assert packet["status"] == "resumed"
    assert packet["from_session_id"] == session_a
    assert packet["to_session_id"] == session_b
    assert runner.current_pack is not None
    resumed_pack = cp.knowledge.context_pack(int(runner.current_pack["id"]))
    assert resumed_pack["purpose"] == "handoff-resume"
    assert resumed_pack["session_id"] == session_b
    assert cp.knowledge.dashboard(task_id)["rollover_count"] == 1

    assert await runner.deliver_queued() == ["keep-me", "during-handoff"]
    messages = cp.messages(task_id)
    assert [row["state"] for row in messages] == [
        "acknowledged",
        "acknowledged",
        "acknowledged",
    ]
    assert [row["attempt"] for row in messages] == [1, 1, 1]
    assert "ack-me" not in FakeSession.starts[1].delivered
    assert FakeSession.starts[1].delivered.count("keep-me") == 1
    assert FakeSession.starts[1].delivered.count("during-handoff") == 1

    await runner.deliver("PHASE B: create phase-b.txt without repeating Phase A")
    assert (repo / "phase-b.txt").read_text() == "PHASE-B-OK\n"
    finished = await runner.finish(
        [
            {"type": "file_contains", "path": "phase-a.txt", "text": "PHASE-A-OK"},
            {"type": "file_contains", "path": "phase-b.txt", "text": "PHASE-B-OK"},
        ],
        output="PHASE-A-OK PHASE-B-OK",
    )
    assert finished["verification"]["verification_status"] == "passed"

    with pytest.raises(ControlPlaneError) as duplicate:
        cp.knowledge.open_session(
            task_id,
            "another-session-b",
            account_id="vicoa-agy-2",
            provider="antigravity",
            parent_session_id=session_a,
            handoff_id=int(packet["id"]),
        )
    assert duplicate.value.code == "duplicate_continuation"
    with pytest.raises(ControlPlaneError) as reused:
        cp.start_task(task_id, account_id="vicoa-agy-2", session_id=session_a)
    assert reused.value.code == "session_reused"
    assert len(FakeSession.starts) == 2

    accounts = {row["id"]: row for row in cp.accounts()}
    assert accounts["agy-1"]["drained"] == 1
    assert accounts["agy-2"]["drained"] == 1


@pytest.mark.asyncio
async def test_restart_reuses_prepared_handoff_without_duplicate_session(
    tmp_path: Path,
) -> None:
    cp, task_id, repo = plane(tmp_path)
    first = worker(cp, task_id, repo)
    started = await first.start(account_id="vicoa-agy-1")
    session_a = started["session_id"]
    assert first.session is not None
    await first.session.deliver_user_message("PHASE A before restart")

    pressure = cp.knowledge.note_pressure(task_id, used_tokens=100, budget_tokens=120)
    packet_id = int(pressure["packet_id"])
    await first.session.aclose()
    first.session = None
    first.client = None

    reopened = ControlPlane(cp.path)
    assert reopened.recover_after_restart() == [task_id]
    packets = reopened.knowledge.list_handoffs(task_id)
    assert [row["id"] for row in packets] == [packet_id]
    sessions = reopened.knowledge.sessions(task_id)
    assert sessions[0]["session_id"] == session_a
    assert sessions[0]["status"] == "rolled_over"

    second = worker(reopened, task_id, repo)
    resumed = await second.resume_pending(preferred_account="vicoa-agy-1")
    assert resumed["session_id"] != session_a
    assert resumed["account_id"] == "vicoa-agy-1"
    assert len(FakeSession.starts) == 2
    packet = reopened.knowledge.handoff(packet_id)
    assert packet["status"] == "resumed"
    assert packet["to_session_id"] == resumed["session_id"]

    competing = worker(reopened, task_id, repo)
    with pytest.raises(ControlPlaneError) as blocked:
        await competing.resume_pending(preferred_account="vicoa-agy-2")
    assert blocked.value.code == "state"
    assert len(FakeSession.starts) == 2

    await second.deliver("PHASE B after restart")
    result = await second.finish(
        [
            {"type": "file_exists", "path": "phase-a.txt"},
            {"type": "file_exists", "path": "phase-b.txt"},
        ],
        output="restart continuation completed",
    )
    assert result["verification"]["verification_status"] == "passed"
