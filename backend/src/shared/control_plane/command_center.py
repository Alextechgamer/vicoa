"""Read model for the private Command Center.

This reads the Vicoa control plane. It does not start workers, open a shell,
or write Agent Control. Locked legacy work is visible and cannot be acted on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .store import ControlPlane, ControlPlaneError, redact

LOCKED_TASK_IDS = frozenset({6, 113, 114, 124})
LOCKED_JOB_IDS = frozenset({70, 71, 106, 111})
LEGACY_ACCOUNTS = frozenset({"agy-1", "agy-2"})
LEGACY_SERVICES = (
    "agent-control",
    "agent-orchestrator",
    "agent-reaper",
    "agent-mcp",
)
EVENT_LIMIT = 80


def snapshot(plane: ControlPlane, *, services: list[dict[str, str]] | None = None) -> dict[str, Any]:
    status = plane.status()
    with plane._conn() as db:
        tasks = [_board_task(row) for row in db.execute(
            """
            SELECT id, job_id, title, worker_status, verification_status, account_id, session_id,
                   CASE WHEN worktree_path = '' THEN 0 ELSE 1 END AS worktree_set,
                   protected, owner_only, import_hold, policy, source_id, stale
            FROM tasks ORDER BY id
            """
        ).fetchall()]
        sessions = [dict(row) for row in db.execute(
            """
            SELECT task_id, session_id, account_id, provider, parent_session_id, handoff_id, status
            FROM sessions ORDER BY id DESC LIMIT 40
            """
        ).fetchall()]
        rollovers = int(db.execute("SELECT COUNT(*) AS n FROM sessions WHERE status='rolled_over'").fetchone()["n"])
        cursor = (
            f"plane={int(db.execute('SELECT COALESCE(MAX(id), 0) AS n FROM plane_events').fetchone()['n'])}"
            f"&legacy={int(db.execute('SELECT COALESCE(MAX(id), 0) AS n FROM events').fetchone()['n'])}"
        )
        handoffs = db.execute("SELECT status, COUNT(*) AS n FROM handoff_packets GROUP BY status").fetchall()
    return {
        "product": "vicoa",
        "source": "control_plane_state",
        "cursor": cursor,
        "summary": {
            "accounts": status["counts"]["accounts"],
            "jobs": status["counts"]["jobs"],
            "tasks": status["counts"]["tasks"],
            "active_sessions": sum(1 for row in sessions if row["status"] in {"active", "running"}),
            "rollovers": rollovers,
            "blockers": len(status["blockers"]),
            "shadow_jobs": len(status["shadow_jobs"]),
        },
        "accounts": [_public_account(row) for row in status["accounts"]],
        "jobs": [_public_job(row) for row in status["jobs"]],
        "tasks": tasks,
        "sessions": sessions,
        "approvals": status["approvals"],
        "blockers": status["blockers"],
        "message_states": status["message_states"],
        "handoffs": [dict(row) for row in handoffs],
        "services": services if services is not None else legacy_service_health(),
        "locked_task_ids": sorted(LOCKED_TASK_IDS),
        "locked_job_ids": sorted(LOCKED_JOB_IDS),
    }


def task_view(plane: ControlPlane, task_id: int) -> dict[str, Any]:
    task = plane.task(task_id)
    task.pop("worker_output", None)
    task.pop("prompt", None)
    with plane._conn() as db:
        messages = [
            {
                "id": int(row["id"]),
                "state": row["state"],
                "attempt": int(row["attempt"]),
                "body": redact(row["body"])[:240],
                "created_at": row["created_at"],
            }
            for row in db.execute(
                "SELECT id, state, attempt, body, created_at FROM steer_messages WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
        ]
    lineage = plane.knowledge.sessions(task_id)
    for row in lineage:
        row.pop("id", None)
    return {
        "source": "control_plane_state",
        "task": {key: value for key, value in task.items() if key != "evidence"},
        "locked": _locked(task),
        "lineage": lineage,
        "handoffs": plane.knowledge.list_handoffs(task_id),
        "context": _latest_context(plane, task_id),
        "skills": plane.knowledge.resolve_skills(task_id),
        "routing": plane.knowledge.explain_route(task_id),
        "messages": messages,
        "verifications": verification_view(plane, task_id),
        "conversation": conversation(plane, task_id),
        "activity": activity(plane, task_id),
        "files": task_files(plane, task_id),
        "timeline": plane.knowledge.timeline(task_id)[-EVENT_LIMIT:],
        "actions_allowed": not _locked(task),
    }


def events_since(plane: ControlPlane, cursor: int | str = 0, *, limit: int = EVENT_LIMIT) -> dict[str, Any]:
    plane_after, legacy_after = _parse_cursor(cursor)
    bounded = max(1, min(int(limit), EVENT_LIMIT))
    with plane._conn() as db:
        plane_rows = db.execute(
            """
            SELECT id, task_id, session_id, event_type, level, message, created_at
            FROM plane_events WHERE id > ? ORDER BY id LIMIT ?
            """,
            (plane_after, bounded),
        ).fetchall()
        legacy_rows = db.execute(
            """
            SELECT id, task_id, level, message, created_at
            FROM events WHERE id > ? ORDER BY id LIMIT ?
            """,
            (legacy_after, bounded),
        ).fetchall()
        next_plane = int(db.execute("SELECT COALESCE(MAX(id), 0) AS n FROM plane_events").fetchone()["n"])
        next_legacy = int(db.execute("SELECT COALESCE(MAX(id), 0) AS n FROM events").fetchone()["n"])
    events = [
        {
            "id": f"plane:{int(row['id'])}",
            "cursor_id": int(row["id"]),
            "source": "plane_events",
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "event_type": row["event_type"],
            "level": row["level"],
            "message": redact(row["message"])[:240],
            "created_at": row["created_at"],
        }
        for row in plane_rows
    ]
    events.extend(
        {
            "id": f"legacy:{int(row['id'])}",
            "cursor_id": int(row["id"]),
            "source": "events",
            "task_id": row["task_id"],
            "session_id": "",
            "event_type": "legacy",
            "level": row["level"],
            "message": redact(row["message"])[:240],
            "created_at": row["created_at"],
        }
        for row in legacy_rows
    )
    events.sort(key=lambda item: (item["created_at"], item["id"]))
    return {"events": events, "cursor": f"plane={next_plane}&legacy={next_legacy}"}


def assert_action_allowed(plane: ControlPlane, task_id: int, action: str) -> None:
    if action in {"shell", "sql", "deploy", "publish", "paid_render", "kill", "reap"}:
        raise ControlPlaneError("forbidden", f"{action} is not a command-center action")
    task = plane.task(task_id)
    if _locked(task):
        raise ControlPlaneError("locked", "this task is held and cannot be acted on from the command center")
    if str(task.get("account_id") or "") in LEGACY_ACCOUNTS:
        raise ControlPlaneError("legacy_account", "legacy profiles cannot receive new work")


def legacy_service_health() -> list[dict[str, str]]:
    rows = []
    for name in LEGACY_SERVICES:
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", f"{name}.service"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            state = (proc.stdout or proc.stderr or "unknown").strip()
        except (OSError, subprocess.TimeoutExpired):
            state = "unknown"
        rows.append({"name": name, "state": state, "role": "legacy caretaker"})
    return rows


def _locked(task: dict[str, Any]) -> bool:
    return (
        int(task["id"]) in LOCKED_TASK_IDS
        or int(task.get("job_id") or 0) in LOCKED_JOB_IDS
        or bool(task.get("protected"))
        or bool(task.get("import_hold"))
        or bool(task.get("owner_only"))
        or str(task.get("source_id") or "") in {"6", "113", "114"}
    )


def _board_task(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "job_id": int(row["job_id"]),
        "title": redact(row["title"])[:160],
        "worker_status": row["worker_status"],
        "verification_status": row["verification_status"],
        "account_id": row["account_id"],
        "session_set": bool(row["session_id"]),
        "worktree_set": bool(row["worktree_set"]),
        "protected": bool(row["protected"]),
        "owner_only": bool(row["owner_only"]),
        "import_hold": bool(row["import_hold"]),
        "policy": row["policy"],
        "stale": bool(row["stale"]),
        "locked": int(row["id"]) in LOCKED_TASK_IDS or int(row["job_id"]) in LOCKED_JOB_IDS or bool(row["protected"]) or bool(row["import_hold"]),
    }


def _public_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider": row.get("provider"),
        "enabled": row.get("enabled"),
        "drained": row.get("drained"),
        "constrained": row.get("constrained"),
        "status": row.get("status"),
        "quota_state": row.get("quota_state"),
        "active_workers": row.get("active_workers"),
        "max_workers": row.get("max_workers"),
        "auth_state": row.get("auth_state"),
        "runtime_home_set": row.get("runtime_home_set"),
    }


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": redact(str(row.get("project") or ""))[:120],
        "status": row.get("status"),
        "shadow": row.get("shadow"),
        "import_hold": row.get("import_hold"),
        "source_id": row.get("source_id"),
        "locked": int(row["id"]) in LOCKED_JOB_IDS or bool(row.get("import_hold")) or bool(row.get("shadow")),
    }


def _latest_context(plane: ControlPlane, task_id: int) -> dict[str, Any] | None:
    with plane._conn() as db:
        row = db.execute(
            """
            SELECT id, purpose, budget_tokens, used_tokens, omitted_json
            FROM context_packs WHERE task_id=? ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "purpose": row["purpose"],
        "budget_tokens": int(row["budget_tokens"]),
        "used_tokens": int(row["used_tokens"]),
        "omitted": row["omitted_json"],
    }


def conversation(plane: ControlPlane, task_id: int, *, limit: int = 40, before: int | None = None) -> dict[str, Any]:
    sessions = plane.knowledge.sessions(task_id)
    messages = plane.messages(task_id)
    if before is not None:
        messages = [row for row in messages if int(row["id"]) < before]
    page = messages[-limit:]
    items = []
    for session in sessions:
        items.append({
            "kind": "boundary",
            "session_id": session["session_id"],
            "account_id": session["account_id"],
            "status": session["status"],
            "parent_session_id": session.get("parent_session_id") or "",
            "handoff_id": session.get("handoff_id"),
            "created_at": session.get("created_at"),
        })
    for row in page:
        items.append({
            "kind": "message",
            "id": int(row["id"]),
            "role": "owner",
            "state": row["state"],
            "text": redact(str(row.get("body") or ""))[:240],
            "created_at": row.get("created_at"),
        })
    return {
        "available": bool(page or sessions),
        "source": "control_plane_messages_and_sessions",
        "note": "Vicoa retained structured messages and session boundaries. A full provider transcript was not stored.",
        "items": items,
        "next_before": page[0]["id"] if page else None,
    }


def activity(plane: ControlPlane, task_id: int, *, limit: int = 40) -> dict[str, Any]:
    rows = plane.knowledge.timeline(task_id)[-limit:]
    return {"events": rows, "source": "plane_events"}


def verification_view(plane: ControlPlane, task_id: int) -> list[dict[str, Any]]:
    with plane._conn() as db:
        rows = db.execute(
            "SELECT id, status, evidence_json, created_at FROM verifications WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    views = []
    for row in rows:
        evidence = {}
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        checks = []
        for item in evidence.get("checks") or []:
            checks.append({
                "type": item.get("type"),
                "passed": item.get("passed"),
                "summary": redact(str(item.get("summary") or ""))[:180],
            })
        views.append({
            "id": int(row["id"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "checks": checks,
            "worktree_recorded": bool(evidence.get("worktree")),
        })
    return views


def task_files(plane: ControlPlane, task_id: int) -> dict[str, Any]:
    root = _worktree(plane, task_id)
    if root is None:
        return {"available": False, "reason": "worktree_missing", "files": []}
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--short"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    files = []
    for line in (proc.stdout or "").splitlines()[:80]:
        status = line[:2].strip() or "changed"
        name = line[3:].strip()
        files.append({"status": status, "path": name})
    return {"available": True, "files": files, "truncated": len(files) >= 80}


def read_task_diff(plane: ControlPlane, task_id: int, relative: str) -> dict[str, Any]:
    root = _worktree(plane, task_id)
    if root is None:
        raise ControlPlaneError("worktree_missing", "this task has no recorded worktree")
    target = _contained(root, relative)
    if target.is_symlink():
        raise ControlPlaneError("path_rejected", "symlink escapes are rejected")
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--", relative],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    text = redact(proc.stdout or "")
    truncated = len(text) > 80000
    return {"path": relative, "diff": text[:80000], "truncated": truncated, "binary": "\0" in (proc.stdout or "")}


def _worktree(plane: ControlPlane, task_id: int):
    task = plane.task(task_id)
    raw = str(task.get("worktree_path") or "")
    if not raw:
        return None
    root = Path(raw).resolve()
    return root if root.is_dir() else None


def _contained(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or "\\" in relative:
        raise ControlPlaneError("path_rejected", "path is outside the task worktree")
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ControlPlaneError("path_rejected", "path is outside the task worktree")
    return target


def _parse_cursor(cursor: int | str) -> tuple[int, int]:
    if isinstance(cursor, int) or str(cursor).isdigit():
        return int(cursor), int(cursor)
    plane_after = 0
    legacy_after = 0
    for part in str(cursor).split("&"):
        if part.startswith("plane="):
            plane_after = int(part.split("=", 1)[1] or 0)
        if part.startswith("legacy="):
            legacy_after = int(part.split("=", 1)[1] or 0)
    return plane_after, legacy_after
