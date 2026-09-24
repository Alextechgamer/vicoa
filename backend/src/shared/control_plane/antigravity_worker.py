"""Vicoa-owned Antigravity task execution with automatic session rollover.

This is an adapter around the existing Antigravity process integration, not a
scheduler. The control plane remains the sole owner of routing, task state,
messages, Context Packs, handoffs, and verification.
"""

from __future__ import annotations

import inspect
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from integrations.headless.antigravity.session import AntigravitySession

from .continuation import LEGACY_ACCOUNTS, render_pack, rollover_due
from .knowledge import emit, estimate_tokens
from .store import ControlPlane, ControlPlaneError


class AgentSession(Protocol):
    conversation_id: str | None
    system_prompt: str | None
    context_used_tokens: int | None

    async def start(self) -> None: ...

    async def deliver_user_message(self, text: str) -> None: ...

    async def aclose(self) -> None: ...


class RecordingAgentClient:
    """Minimal async client for direct control-plane workers and canaries."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.statuses: list[str] = []
        self.patches: list[dict[str, Any]] = []

    async def send_message(self, *, content: str, **_: Any) -> None:
        self.messages.append(content)

    async def update_agent_instance_status(
        self, _instance_id: str, status: str
    ) -> None:
        self.statuses.append(status)

    async def patch_agent_instance(
        self, _instance_id: str, **patch: Any
    ) -> dict[str, Any]:
        self.patches.append(patch)
        return {}

    async def download_attachment(self, _attachment_id: str) -> tuple[bytes, str]:
        return b"", "application/octet-stream"


SessionFactory = Callable[..., AgentSession]
ClientFactory = Callable[[], Any]
HandoffCallback = Callable[[dict[str, Any]], Any]


class AntigravityTaskWorker:
    """Run one Vicoa task through one or more real Antigravity sessions."""

    def __init__(
        self,
        plane: ControlPlane,
        task_id: int,
        *,
        cwd: str | Path,
        session_factory: SessionFactory = AntigravitySession,
        client_factory: ClientFactory = RecordingAgentClient,
        agent_binary: str = "agy",
        model: str | None = None,
        permission_mode: str = "acceptEdits",
        initial_context_budget: int = 1200,
        rollover_budget_tokens: int | None = None,
        max_rollovers: int = 1,
        on_handoff_prepared: HandoffCallback | None = None,
    ) -> None:
        self.plane = plane
        self.task_id = int(task_id)
        self.cwd = str(Path(cwd))
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.agent_binary = agent_binary
        self.model = model
        self.permission_mode = permission_mode
        self.initial_context_budget = max(32, int(initial_context_budget))
        self.rollover_budget_tokens = rollover_budget_tokens
        self.max_rollovers = max(0, int(max_rollovers))
        self.on_handoff_prepared = on_handoff_prepared
        self.session: AgentSession | None = None
        self.client: Any = None
        self.account_id = ""
        self.session_id = ""
        self.current_pack: dict[str, Any] | None = None
        self._estimated_context_tokens = 0

    def _account(self, account_id: str) -> dict[str, Any]:
        if account_id in LEGACY_ACCOUNTS:
            raise ControlPlaneError(
                "legacy_account", "legacy profiles cannot receive new work"
            )
        with self.plane._conn() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        if row is None:
            raise ControlPlaneError("not_found", f"account {account_id} not found")
        account = dict(row)
        if account["provider"] != "antigravity":
            raise ControlPlaneError(
                "provider", "the Antigravity worker requires an Antigravity profile"
            )
        if (
            not account["enabled"]
            or account["drained"]
            or account["constrained"]
            or account["status"] == "provider_failed"
        ):
            raise ControlPlaneError(
                "account", "account is not eligible for Antigravity work"
            )
        return account

    def _choose_account(self, preferred: str = "") -> str:
        if preferred:
            try:
                account = self._account(preferred)
                if int(account["active_workers"]) < int(account["max_workers"]):
                    return preferred
            except ControlPlaneError:
                pass
        decision = self.plane.route(self.task_id)
        chosen = str(decision["account_id"])
        self._account(chosen)
        return chosen

    def _process_env(self, runtime_home: str) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = runtime_home
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        return env

    async def _launch_process(
        self, account_id: str, *, system_prompt: str | None = None
    ) -> tuple[AgentSession, Any, str]:
        account = self._account(account_id)
        client = self.client_factory()
        session = self.session_factory(
            vicoa_client=client,
            instance_id=f"cp-{self.task_id}-{uuid.uuid4().hex}",
            cwd=self.cwd,
            binary=self.agent_binary,
            agent_type="antigravity",
            model=self.model,
            permission_mode=self.permission_mode,
            system_prompt=system_prompt,
            process_env=self._process_env(str(account["runtime_home"])),
        )
        await session.start()
        session_id = str(session.conversation_id or "")
        if not session_id:
            await session.aclose()
            raise ControlPlaneError(
                "session_missing", "Antigravity did not return a conversation id"
            )
        return session, client, session_id

    async def start(self, *, account_id: str = "") -> dict[str, Any]:
        """Open Session A and start the Vicoa task."""
        task = self.plane.task(self.task_id)
        if task["worker_status"] == "interrupted":
            pending = self.plane.knowledge.pending_handoff(self.task_id)
            if pending is not None:
                return await self.resume_pending(preferred_account=account_id)
        if task["worker_status"] not in {"queued", "interrupted"}:
            raise ControlPlaneError(
                "state", "the task is not ready to start a new worker"
            )
        chosen = self._choose_account(account_id)
        session, client, session_id = await self._launch_process(chosen)
        registered = False
        try:
            self.plane.knowledge.open_session(
                self.task_id,
                session_id,
                account_id=chosen,
                provider="antigravity",
            )
            registered = True
            self.plane.start_task(
                self.task_id, account_id=chosen, session_id=session_id
            )
            pack = self.plane.knowledge.build_context(
                self.task_id,
                purpose="initial",
                budget_tokens=self.initial_context_budget,
                session_id=session_id,
            )
            rendered = render_pack(pack, adapter="antigravity")
            session.system_prompt = rendered
        except Exception:
            if registered:
                self.plane.knowledge.close_session(session_id, status="failed")
            await session.aclose()
            raise
        self.session = session
        self.client = client
        self.account_id = chosen
        self.session_id = session_id
        self.current_pack = pack
        self._estimated_context_tokens = max(
            int(pack["used_tokens"]), estimate_tokens(rendered)
        )
        self._emit(
            "worker_session_started",
            f"Session A {session_id} started on {chosen}",
            {"context_pack_id": int(pack["id"]), "account_id": chosen},
        )
        return self.state()

    def _emit(self, event_type: str, message: str, detail: dict[str, Any]) -> None:
        task = self.plane.task(self.task_id)
        with self.plane._conn() as db:
            emit(
                db,
                self.plane,
                task_id=self.task_id,
                job_id=int(task["job_id"]),
                session_id=self.session_id,
                event_type=event_type,
                message=message,
                detail=detail,
            )

    def state(self) -> dict[str, Any]:
        board = self.plane.knowledge.dashboard(self.task_id)
        return {
            "task_id": self.task_id,
            "account_id": self.account_id,
            "session_id": self.session_id,
            "context_pack_id": (
                None if self.current_pack is None else int(self.current_pack["id"])
            ),
            "rollover_count": int(board["rollover_count"]),
            "worker_status": board["worker_status"],
            "verification_status": board["verification_status"],
        }

    def _context_used(self, text: str = "") -> int:
        if text:
            self._estimated_context_tokens += estimate_tokens(text)
        actual = 0
        if self.session is not None:
            actual = int(self.session.context_used_tokens or 0)
        return max(actual, self._estimated_context_tokens)

    async def deliver(self, text: str) -> dict[str, Any]:
        """Run a turn and automatically roll over after it when due."""
        if self.session is None:
            raise ControlPlaneError("state", "no Antigravity session is active")
        await self.session.deliver_user_message(text)
        used = self._context_used(text)
        result: dict[str, Any] = {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "used_tokens": used,
            "rolled_over": False,
        }
        budget = self.rollover_budget_tokens
        count = int(self.plane.knowledge.dashboard(self.task_id)["rollover_count"])
        if (
            budget is not None
            and count < self.max_rollovers
            and rollover_due(used, int(budget))
        ):
            prepared = await self.prepare_rollover(
                used_tokens=used, budget_tokens=int(budget)
            )
            resumed = await self.resume_pending(
                preferred_account=str(prepared["from_account_id"])
            )
            result.update(
                {
                    "rolled_over": True,
                    "handoff_id": int(prepared["packet_id"]),
                    "from_session_id": prepared["from_session_id"],
                    "to_session_id": resumed["session_id"],
                    "account_id": resumed["account_id"],
                }
            )
        return result

    async def _run_handoff_callback(self, packet: dict[str, Any]) -> None:
        if self.on_handoff_prepared is None:
            return
        result = self.on_handoff_prepared(packet)
        if inspect.isawaitable(result):
            await result

    async def prepare_rollover(
        self, *, used_tokens: int, budget_tokens: int
    ) -> dict[str, Any]:
        """Persist the handoff and stop Session A, but do not launch B."""
        if self.session is None or not self.session_id:
            raise ControlPlaneError("state", "no Session A is active")
        pressure = self.plane.knowledge.note_pressure(
            self.task_id,
            used_tokens=used_tokens,
            budget_tokens=budget_tokens,
        )
        if not pressure.get("prepared"):
            raise ControlPlaneError(
                "within_budget", "context pressure is below the rollover threshold"
            )
        packet = self.plane.knowledge.handoff(int(pressure["packet_id"]))
        await self._run_handoff_callback(packet)
        old_session = self.session
        old_session_id = self.session_id
        old_account_id = self.account_id
        await old_session.aclose()
        self.plane.suspend_for_rollover(
            self.task_id,
            packet_id=int(packet["id"]),
            session_id=old_session_id,
        )
        self.session = None
        self.client = None
        self.session_id = ""
        self.account_id = ""
        self.current_pack = None
        return {
            "packet_id": int(packet["id"]),
            "from_session_id": old_session_id,
            "from_account_id": old_account_id,
            "used_tokens": used_tokens,
            "budget_tokens": budget_tokens,
        }

    async def resume_pending(self, *, preferred_account: str = "") -> dict[str, Any]:
        """Launch exactly one Session B for the task's pending handoff."""
        task = self.plane.task(self.task_id)
        if task["worker_status"] != "interrupted":
            raise ControlPlaneError(
                "state", "a pending continuation requires an interrupted task"
            )
        packet = self.plane.knowledge.pending_handoff(self.task_id)
        if packet is None:
            raise ControlPlaneError("not_found", "no pending handoff exists")
        chosen = self._choose_account(preferred_account)
        session, client, session_id = await self._launch_process(chosen)
        registered = False
        try:
            self.plane.knowledge.open_session(
                self.task_id,
                session_id,
                account_id=chosen,
                provider="antigravity",
                parent_session_id=str(packet["from_session_id"]),
                handoff_id=int(packet["id"]),
            )
            registered = True
            resumed = self.plane.knowledge.resume_from_handoff(
                int(packet["id"]), new_session_id=session_id
            )
            pack = self.plane.knowledge.context_pack(int(resumed["context_pack_id"]))
            rendered = render_pack(pack, adapter="antigravity")
            session.system_prompt = rendered
            self.plane.start_task(
                self.task_id, account_id=chosen, session_id=session_id
            )
        except Exception:
            if registered:
                self.plane.knowledge.close_session(session_id, status="failed")
            await session.aclose()
            raise
        self.session = session
        self.client = client
        self.account_id = chosen
        self.session_id = session_id
        self.current_pack = pack
        self._estimated_context_tokens = max(
            int(pack["used_tokens"]), estimate_tokens(rendered)
        )
        self._emit(
            "rollover_completed",
            f"handoff {packet['id']} continued in Session B {session_id}",
            {
                "packet_id": int(packet["id"]),
                "from_session_id": packet["from_session_id"],
                "to_session_id": session_id,
                "account_id": chosen,
                "context_pack_id": int(pack["id"]),
            },
        )
        return self.state()

    async def deliver_queued(self, *, limit: int | None = None) -> list[str]:
        """Deliver queued task messages in order and acknowledge after success."""
        if self.session is None:
            raise ControlPlaneError("state", "no Antigravity session is active")
        delivered: list[str] = []
        while limit is None or len(delivered) < limit:
            row = self.plane.deliver_next(self.task_id, worker_accepting=True)
            if not row.get("delivered"):
                break
            body = str(row["body"])
            await self.session.deliver_user_message(body)
            self._context_used(body)
            self.plane.acknowledge_message(int(row["id"]))
            delivered.append(body)
        return delivered

    async def finish(
        self,
        checks: list[dict[str, Any]],
        *,
        output: str = "task completed",
        branch: str = "",
        base_ref: str = "",
    ) -> dict[str, Any]:
        if self.session is None or not self.session_id:
            raise ControlPlaneError("state", "no Antigravity session is active")
        session = self.session
        session_id = self.session_id
        self.plane.complete_worker(
            self.task_id,
            worktree_path=self.cwd,
            branch=branch,
            base_ref=base_ref,
            output=output,
        )
        verified = self.plane.verify_task(self.task_id, checks, worktree=self.cwd)
        await session.aclose()
        self.plane.knowledge.close_session(session_id, status="completed")
        self.session = None
        self.client = None
        self._emit(
            "worker_verified",
            f"task {self.task_id} verification {verified['verification_status']}",
            {"verification_status": verified["verification_status"]},
        )
        return {"task": self.plane.task(self.task_id), "verification": verified}

    async def aclose(self) -> None:
        """Stop the owned process without claiming successful completion."""
        if self.session is None:
            return
        session = self.session
        session_id = self.session_id
        await session.aclose()
        task = self.plane.task(self.task_id)
        if task["worker_status"] in {"running", "needs_input"}:
            self.plane.crash_worker(self.task_id)
        try:
            self.plane.knowledge.close_session(session_id, status="interrupted")
        except ControlPlaneError as exc:
            if exc.code != "state":
                raise
        self.session = None
        self.client = None


__all__ = ["AntigravityTaskWorker", "RecordingAgentClient"]
