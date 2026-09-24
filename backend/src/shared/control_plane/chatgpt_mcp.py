"""ChatGPT MCP adapter for the native Vicoa control plane.

This process is a protocol adapter. It does not schedule workers, open a
shell, or write the Agent Control database. The controller identity and the
bearer token are injected from the environment. Tool arguments cannot supply
either one, and no tool accepts an override reason.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from shared.control_plane.store import LIVE_AGENT_CONTROL_DB, ControlPlane, ControlPlaneError, redact

HELD_TASK_IDS = frozenset({6, 113, 114, 124})
HELD_JOB_IDS = frozenset({70, 71, 106, 111, 125})
LEGACY_HOME_PREFIX = "/home/agentctl/"
PATCH_FIELDS = frozenset({"title", "prompt", "acceptance_criteria", "vicoa_task_id"})
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
FORBIDDEN_TOOLS = frozenset(
    {
        "shell",
        "exec",
        "sql",
        "query_database",
        "read_file",
        "write_file",
        "kill",
        "process_kill",
        "force_unhealthy",
        "delete_worktree",
    }
)


def controller_id() -> str:
    value = os.environ.get("VICOA_MCP_CONTROLLER_ID", "chatgpt-main").strip()
    return value or "chatgpt-main"


def bearer_token() -> str:
    token = os.environ.get("VICOA_MCP_TOKEN", "")
    if not token or token.strip() != token or len(token) < 24:
        raise RuntimeError("VICOA_MCP_TOKEN is missing or too short")
    return token


def open_plane() -> ControlPlane:
    raw = os.environ.get("VICOA_CONTROL_PLANE_DB", "")
    if not raw:
        raise RuntimeError("VICOA_CONTROL_PLANE_DB is required")
    path = Path(raw).resolve()
    if path == LIVE_AGENT_CONTROL_DB.resolve():
        raise RuntimeError("refusing the Agent Control database")
    return ControlPlane(path)


def _public(value: Any) -> Any:
    token = os.environ.get("VICOA_MCP_TOKEN", "")
    encoded = json.dumps(value, sort_keys=True, default=str)
    encoded = redact(encoded)
    if token:
        encoded = encoded.replace(token, "[redacted]")
    return json.loads(encoded)


def _fail(exc: ControlPlaneError) -> None:
    raise ToolError(f"{exc.code}: {exc}") from None


def _task_or_refuse(plane: ControlPlane, task_id: int) -> dict[str, Any]:
    try:
        task = plane.task(int(task_id))
    except ControlPlaneError as exc:
        _fail(exc)
    if int(task["id"]) in HELD_TASK_IDS or int(task["job_id"]) in HELD_JOB_IDS:
        raise ToolError("refused: held live work")
    if task["protected"] or task["owner_only"] or task["import_hold"]:
        raise ToolError("refused: held or owner-only task")
    return task


def _job_or_refuse(plane: ControlPlane, job_id: int) -> dict[str, Any]:
    try:
        job = plane.job(int(job_id))
    except ControlPlaneError as exc:
        _fail(exc)
    if int(job["id"]) in HELD_JOB_IDS:
        raise ToolError("refused: held live job")
    for task in job["tasks"]:
        if int(task["id"]) in HELD_TASK_IDS or task["protected"] or task["owner_only"]:
            raise ToolError("refused: job contains held work")
    return job


def _account_or_refuse(plane: ControlPlane, account_id: str, *, action: str) -> dict[str, Any]:
    with plane._conn() as db:
        row = db.execute("SELECT id, runtime_home FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        raise ToolError("not_found: account")
    home = str(row["runtime_home"])
    if home.startswith(LEGACY_HOME_PREFIX) or account_id.startswith("agy-"):
        raise ToolError("refused: legacy runtime is not mutable from ChatGPT")
    if action == "drain" and (account_id.startswith("vicoa-agy-") or "/vicoa-agy-" in home):
        raise ToolError("refused: new-work profile is not drained from ChatGPT")
    return {"id": row["id"]}


class VicoaMcp:
    def __init__(self, plane: ControlPlane):
        self.plane = plane

    def status(self) -> dict[str, Any]:
        data = self.plane.status()
        data.pop("jobs", None)
        data["jobs"] = [
            {"id": job["id"], "project": job["project"], "status": job["status"]}
            for job in self.plane.jobs()
        ]
        return _public(data)

    def profiles(self) -> dict[str, Any]:
        return _public({"profiles": self.plane.accounts()})

    def quota(self) -> dict[str, Any]:
        rows = []
        for row in self.plane.quota():
            item = dict(row)
            if item.get("value_pct") is None:
                item["value_pct"] = None
                item["status"] = item.get("status") or "unknown"
            rows.append(item)
        return _public({"observations": rows})

    def jobs(self) -> dict[str, Any]:
        return _public({"jobs": self.plane.jobs()})

    def job(self, job_id: int) -> dict[str, Any]:
        try:
            return _public(self.plane.job(int(job_id)))
        except ControlPlaneError as exc:
            _fail(exc)

    def tasks(self) -> dict[str, Any]:
        return _public({"tasks": self.plane.task_board()})

    def task(self, task_id: int) -> dict[str, Any]:
        try:
            return _public(self.plane.task(int(task_id)))
        except ControlPlaneError as exc:
            _fail(exc)

    def messages(self, task_id: int) -> dict[str, Any]:
        try:
            self.plane.task(int(task_id))
            return _public({"messages": self.plane.messages(int(task_id))})
        except ControlPlaneError as exc:
            _fail(exc)

    def events(self, task_id: int = 0) -> dict[str, Any]:
        if task_id:
            return _public({"events": self.plane.knowledge.timeline(int(task_id))})
        return _public({"events": self.plane.knowledge.events()})

    def evidence(self, task_id: int) -> dict[str, Any]:
        return self.task(task_id)

    def skills(self) -> dict[str, Any]:
        rows = []
        for skill in self.plane.knowledge.list_skills():
            rows.append(
                {
                    "id": skill.get("id"),
                    "name": skill.get("name"),
                    "active": skill.get("active"),
                    "adapter": skill.get("adapter"),
                }
            )
        return _public({"skills": rows})

    def context(self, task_id: int) -> dict[str, Any]:
        try:
            return _public(self.plane.knowledge.build_context(int(task_id)))
        except ControlPlaneError as exc:
            _fail(exc)

    def handoff(self, task_id: int) -> dict[str, Any]:
        return _public({"packets": self.plane.knowledge.list_handoffs(int(task_id))})

    def approvals(self) -> dict[str, Any]:
        return _public({"approvals": self.plane.approval_board()})

    def routes(self, task_id: int) -> dict[str, Any]:
        return _public({"explanations": self.plane.knowledge.explain_route(int(task_id))})

    def blockers(self) -> dict[str, Any]:
        return _public({"blockers": self.plane.blockers()})

    def create_job(self, project: str, goal: str) -> dict[str, Any]:
        if not project.strip() or not goal.strip():
            raise ToolError("rejected: project and goal are required")
        try:
            return _public(self.plane.create_job(project=project, goal=goal))
        except ControlPlaneError as exc:
            _fail(exc)

    def claim_job(self, job_id: int) -> dict[str, Any]:
        _job_or_refuse(self.plane, job_id)
        try:
            return _public(self.plane.claim_job(int(job_id), controller_id()))
        except ControlPlaneError as exc:
            _fail(exc)

    def heartbeat_job(self, job_id: int) -> dict[str, Any]:
        _job_or_refuse(self.plane, job_id)
        try:
            return _public(self.plane.heartbeat_job(int(job_id)))
        except ControlPlaneError as exc:
            _fail(exc)

    def submit_plan(self, job_id: int, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        _job_or_refuse(self.plane, job_id)
        created: dict[str, int] = {}
        try:
            for spec in tasks:
                task = self.plane.add_task(
                    int(job_id),
                    title=str(spec["title"]),
                    plan_key=str(spec["key"]),
                    prompt=str(spec.get("prompt") or ""),
                    acceptance_criteria=str(spec.get("acceptance_criteria") or ""),
                )
                created[str(spec["key"])] = int(task["id"])
            for spec in tasks:
                for dep in spec.get("depends_on") or []:
                    self.plane.add_dependency(created[str(spec["key"])], created[str(dep)])
        except ControlPlaneError as exc:
            _fail(exc)
        return _public({"job_id": int(job_id), "task_ids": created})

    def message_worker(self, task_id: int, text: str, idempotency_key: str) -> dict[str, Any]:
        _task_or_refuse(self.plane, task_id)
        try:
            return _public(
                self.plane.enqueue_message(int(task_id), text, idempotency_key=idempotency_key)
            )
        except ControlPlaneError as exc:
            _fail(exc)

    def patch_task_prefs(self, task_id: int, fields: dict[str, str]) -> dict[str, Any]:
        _task_or_refuse(self.plane, task_id)
        unknown = set(fields) - PATCH_FIELDS
        if unknown:
            raise ToolError("rejected: field is not a task preference")
        try:
            return _public(self.plane.mutate(int(task_id), **fields))
        except ControlPlaneError as exc:
            _fail(exc)

    def approve_once(self, approval_id: int, prompt_text: str) -> dict[str, Any]:
        approval = self.plane.approval(int(approval_id))
        _task_or_refuse(self.plane, int(approval["task_id"]))
        try:
            return _public(
                self.plane.approve_once(int(approval_id), prompt_text=prompt_text, confirm_permanent=False)
            )
        except ControlPlaneError as exc:
            _fail(exc)

    def deny(self, approval_id: int, prompt_text: str) -> dict[str, Any]:
        approval = self.plane.approval(int(approval_id))
        _task_or_refuse(self.plane, int(approval["task_id"]))
        try:
            return _public(self.plane.deny(int(approval_id), prompt_text=prompt_text))
        except ControlPlaneError as exc:
            _fail(exc)

    def retry_task(self, task_id: int) -> dict[str, Any]:
        _task_or_refuse(self.plane, task_id)
        try:
            return _public(self.plane.retry_verification(int(task_id), []))
        except ControlPlaneError as exc:
            _fail(exc)

    def retry_reap(self, task_id: int) -> dict[str, Any]:
        _task_or_refuse(self.plane, task_id)
        try:
            return _public(self.plane.reap(int(task_id)))
        except ControlPlaneError as exc:
            _fail(exc)

    def drain_profile(self, account_id: str) -> dict[str, Any]:
        _account_or_refuse(self.plane, account_id, action="drain")
        try:
            return _public(self.plane.drain(account_id))
        except ControlPlaneError as exc:
            _fail(exc)

    def enable_profile(self, account_id: str) -> dict[str, Any]:
        _account_or_refuse(self.plane, account_id, action="enable")
        try:
            return _public(self.plane.enable(account_id))
        except ControlPlaneError as exc:
            _fail(exc)

    def route(self, task_id: int) -> dict[str, Any]:
        _task_or_refuse(self.plane, task_id)
        try:
            return _public(self.plane.route(int(task_id)))
        except ControlPlaneError as exc:
            _fail(exc)


def build_server(adapter: VicoaMcp) -> MCPServer:
    mcp = MCPServer(
        "Vicoa",
        instructions=(
            "Use these tools to operate the Vicoa control plane. The server injects "
            "the controller identity. Do not pass a manager id, token, or override reason. "
            "Held live work is refused. Missing quota is unknown, not zero. "
            "There is no release, cancel, raw kill, shell, or filesystem tool."
        ),
    )

    def read(name: str, title: str, fn):
        mcp.tool(name=name, title=title, annotations=READ_ONLY)(fn)

    def write(name: str, title: str, fn):
        mcp.tool(name=name, title=title, annotations=WRITE)(fn)

    read("vicoa_status", "Vicoa status", adapter.status)
    read("vicoa_profiles", "Vicoa profiles", adapter.profiles)
    read("vicoa_quota", "Vicoa quota", adapter.quota)
    read("vicoa_jobs", "Vicoa jobs", adapter.jobs)
    read("vicoa_job", "Vicoa job", adapter.job)
    read("vicoa_tasks", "Vicoa tasks", adapter.tasks)
    read("vicoa_task", "Vicoa task", adapter.task)
    read("vicoa_messages", "Vicoa messages", adapter.messages)
    read("vicoa_events", "Vicoa events", adapter.events)
    read("vicoa_evidence", "Vicoa evidence", adapter.evidence)
    read("vicoa_skills", "Vicoa skills", adapter.skills)
    read("vicoa_context", "Vicoa context", adapter.context)
    read("vicoa_handoff", "Vicoa handoff", adapter.handoff)
    read("vicoa_approvals", "Vicoa approvals", adapter.approvals)
    read("vicoa_routes", "Vicoa routes", adapter.routes)
    read("vicoa_blockers", "Vicoa blockers", adapter.blockers)
    write("vicoa_create_job", "Create Vicoa job", adapter.create_job)
    write("vicoa_claim_job", "Claim Vicoa job", adapter.claim_job)
    write("vicoa_heartbeat_job", "Heartbeat Vicoa job", adapter.heartbeat_job)
    write("vicoa_submit_plan", "Submit Vicoa plan", adapter.submit_plan)
    write("vicoa_message_worker", "Message Vicoa worker", adapter.message_worker)
    write("vicoa_patch_task_prefs", "Patch Vicoa task preferences", adapter.patch_task_prefs)
    write("vicoa_approve_once", "Approve once", adapter.approve_once)
    write("vicoa_deny", "Deny approval", adapter.deny)
    write("vicoa_retry_task", "Retry Vicoa verification", adapter.retry_task)
    write("vicoa_retry_reap", "Reap a finished Vicoa task", adapter.retry_reap)
    write("vicoa_drain_profile", "Drain a non-legacy profile", adapter.drain_profile)
    write("vicoa_enable_profile", "Enable a non-legacy profile", adapter.enable_profile)
    write("vicoa_route", "Route a Vicoa task", adapter.route)
    return mcp


class BearerGuard:
    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client") or ("", 0)
        if client[0] not in {"127.0.0.1", "::1"}:
            await _reject(send, 403)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        if b"x-forwarded-for" in headers or b"forwarded" in headers:
            await _reject(send, 403)
            return
        expected = f"Bearer {self.token}".encode()
        if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
            await _reject(send, 401)
            return
        await self.app(scope, receive, send)


async def _reject(send: Any, status: int) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


def build_app(plane: ControlPlane | None = None) -> BearerGuard:
    adapter = VicoaMcp(plane or open_plane())
    server = build_server(adapter)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )
    return BearerGuard(app, bearer_token())


app = None
