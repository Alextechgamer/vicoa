"""Constrained MCP surface for the Vicoa control plane.

No shell, database, or filesystem tools are registered. Credentials stay in the
store and are not returned by these operations.
"""

from __future__ import annotations

from typing import Any

from .store import ControlPlane, ControlPlaneError

TOOL_NAMES = (
    "status",
    "projects",
    "jobs",
    "evidence",
    "verification",
    "approvals",
    "quota_health",
    "create_goal",
    "submit_dag",
    "message_worker",
    "blockers",
    "retry_eligible",
    "tasks",
    "routing",
    "list_approvals",
    "claim_job",
    "heartbeat",
    "enable_profile",
    "drain_profile",
    "job",
    "profiles",
    "deny",
)

REJECTED = frozenset(
    {
        "shell",
        "exec",
        "sql",
        "query_database",
        "read_file",
        "write_file",
        "raw_fs",
        "process_kill",
        "kill_process",
        "shell_exec",
    }
)


def dispatch(plane: ControlPlane, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if tool in REJECTED or tool not in TOOL_NAMES:
        raise ControlPlaneError("forbidden", f"{tool} is not on the control-plane MCP surface")
    args = arguments or {}
    if tool == "status":
        return plane.status()
    if tool == "projects":
        return {"projects": plane.portfolio()}
    if tool == "jobs":
        return {"jobs": plane.jobs()}
    if tool == "evidence":
        return {"task": plane.task(int(args["task_id"]))}
    if tool == "verification":
        return plane.verify_task(int(args["task_id"]), list(args.get("checks") or []))
    if tool == "approvals":
        return plane.approve_once(
            int(args["approval_id"]),
            prompt_text=str(args.get("prompt_text") or ""),
            confirm_permanent=bool(args.get("confirm_permanent")),
        )
    if tool == "quota_health":
        return {"observations": plane.quota(), "accounts": plane.accounts()}
    if tool == "create_goal":
        return plane.create_job(
            project=str(args["project"]),
            goal=str(args["goal"]),
            manager_id=str(args.get("manager_id") or ""),
            vicoa_project_id=str(args.get("vicoa_project_id") or ""),
        )
    if tool == "submit_dag":
        job = plane.create_job(project=str(args["project"]), goal=str(args["goal"]))
        created = []
        keys: dict[str, int] = {}
        for spec in args.get("tasks") or []:
            task = plane.add_task(
                int(job["id"]),
                title=str(spec["title"]),
                plan_key=str(spec["key"]),
                prompt=str(spec.get("prompt") or ""),
            )
            keys[str(spec["key"])] = int(task["id"])
            created.append(task["id"])
        for spec in args.get("tasks") or []:
            for dep in spec.get("depends_on") or []:
                plane.add_dependency(keys[str(spec["key"])], keys[str(dep)])
        return {"job_id": job["id"], "task_ids": created}
    if tool == "message_worker":
        return plane.enqueue_message(
            int(args["task_id"]),
            str(args.get("text") or ""),
            idempotency_key=str(args.get("idempotency_key") or args.get("text") or ""),
        )
    if tool == "tasks":
        return {"tasks": plane.tasks()}
    if tool == "routing":
        return plane.route(int(args["task_id"]), pool=args.get("pool"))
    if tool == "list_approvals":
        with plane._conn() as db:
            rows = db.execute("SELECT id, task_id, status, permanent, consumed FROM approvals ORDER BY id").fetchall()
        return {"approvals": [dict(row) for row in rows]}
    if tool == "claim_job":
        return plane.claim_job(int(args["job_id"]), str(args["manager_id"]))
    if tool == "heartbeat":
        return plane.heartbeat_job(int(args["job_id"]))
    if tool == "job":
        return plane.job(int(args["job_id"]))
    if tool == "profiles":
        return {"profiles": plane.accounts()}
    if tool == "drain_profile":
        return plane.drain(str(args["account_id"]))
    if tool == "enable_profile":
        return plane.enable(str(args["account_id"]))
    if tool == "deny":
        return plane.deny(int(args["approval_id"]), prompt_text=str(args.get("prompt_text") or ""))
    if tool == "blockers":
        return {"blockers": plane.blockers()}
    if tool == "retry_eligible":
        return plane.retry_verification(int(args["task_id"]), list(args.get("checks") or []))
    raise ControlPlaneError("forbidden", tool)
