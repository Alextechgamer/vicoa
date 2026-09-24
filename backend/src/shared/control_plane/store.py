"""Vicoa control plane.

Native orchestration for multi-account workers, verification-gated DAGs,
one-time approvals, quota routing, and protected tasks. This is not a wrapper
around Agent Control. Agent Control's database is never written by this package.

Worker status and verification status are separate. A dependency unlocks only
when the upstream task's verification status is ``passed``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SECRET_RE = re.compile(
    r"(?i)(sk_live_[A-Za-z0-9]+|sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+"
    r"|(?:api[_-]?key|token|password|secret|authorization)\s*[=:]\s*\S+)"
)
PROTECTED_ACTIONS = frozenset({"resume", "message", "route", "kill", "reap", "approve", "mutate"})
TASK_POLICIES = frozenset(
    {"routine", "approval_required", "owner_only", "destructive", "paid", "production", "protected"}
)
STOP_POLICIES = TASK_POLICIES - {"routine"}
OWNER_ACTIONS = {
    "production_deploy": "production",
    "credential_rotation": "owner_only",
    "paid_render": "paid",
    "customer_contact": "owner_only",
    "plugin_publication": "production",
    "destructive_migration": "destructive",
    "protected_mutation": "protected",
}
CONSERVE_PCT = 50
CRITICAL_PCT = 15
MIN_ADVANTAGE_PCT = 10
STALE_HOURS = 6
LIVE_AGENT_CONTROL_DB = Path("/opt/agent-control/state/agent-control.db")


class ControlPlaneError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def redact(value: str | None) -> str:
    if not value:
        return ""
    return SECRET_RE.sub("[redacted]", value)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  profile TEXT NOT NULL,
  runtime_home TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  drained INTEGER NOT NULL DEFAULT 0 CHECK(drained IN (0, 1)),
  constrained INTEGER NOT NULL DEFAULT 0 CHECK(constrained IN (0, 1)),
  constraint_reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unknown',
  max_workers INTEGER NOT NULL DEFAULT 1 CHECK(max_workers > 0),
  active_workers INTEGER NOT NULL DEFAULT 0 CHECK(active_workers >= 0),
  auth_state TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  goal TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  manager_id TEXT NOT NULL DEFAULT '',
  vicoa_project_id TEXT NOT NULL DEFAULT '',
  source_system TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL DEFAULT '',
  import_hold INTEGER NOT NULL DEFAULT 0 CHECK(import_hold IN (0, 1)),
  shadow INTEGER NOT NULL DEFAULT 0 CHECK(shadow IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
  plan_key TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  prompt TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  worker_status TEXT NOT NULL DEFAULT 'queued',
  verification_status TEXT NOT NULL DEFAULT 'not_started',
  verification_summary TEXT NOT NULL DEFAULT '',
  worktree_path TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  base_ref TEXT NOT NULL DEFAULT '',
  account_id TEXT REFERENCES accounts(id),
  session_id TEXT NOT NULL DEFAULT '',
  worker_output TEXT NOT NULL DEFAULT '',
  protected INTEGER NOT NULL DEFAULT 0 CHECK(protected IN (0, 1)),
  protection_reason TEXT NOT NULL DEFAULT '',
  owner_only INTEGER NOT NULL DEFAULT 0 CHECK(owner_only IN (0, 1)),
  owner_blocker TEXT NOT NULL DEFAULT '',
  import_hold INTEGER NOT NULL DEFAULT 0 CHECK(import_hold IN (0, 1)),
  queued_prompts INTEGER NOT NULL DEFAULT 0,
  steer_consumed INTEGER NOT NULL DEFAULT 1 CHECK(steer_consumed IN (0, 1)),
  steer_text TEXT NOT NULL DEFAULT '',
  heartbeat_at TEXT,
  stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
  reap_state TEXT NOT NULL DEFAULT 'none',
  evidence_dir TEXT NOT NULL DEFAULT '',
  vicoa_task_id TEXT NOT NULL DEFAULT '',
  source_system TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL DEFAULT '',
  acceptance_criteria TEXT NOT NULL DEFAULT '',
  policy TEXT NOT NULL DEFAULT 'routine',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id, plan_key)
);
CREATE TABLE IF NOT EXISTS task_dependencies(
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY(task_id, depends_on_task_id),
  CHECK(task_id != depends_on_task_id)
);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  permanent INTEGER NOT NULL DEFAULT 0 CHECK(permanent IN (0, 1)),
  consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0, 1)),
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS quota_observations(
  account_id TEXT NOT NULL,
  pool TEXT NOT NULL,
  quota_window TEXT NOT NULL,
  value_pct REAL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(account_id, pool, quota_window)
);
CREATE TABLE IF NOT EXISTS steer_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  body TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued', 'sent', 'acknowledged', 'failed')),
  attempt INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(task_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS account_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_decisions(
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  account_id TEXT,
  summary TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allow_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
  created_at TEXT NOT NULL
);
"""


class ControlPlane:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.dialect = "postgres" if self.path.startswith("postgresql") else "sqlite"
        if self.dialect == "sqlite":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.executescript(SCHEMA)
            from .knowledge import KNOWLEDGE_SCHEMA

            db.executescript(KNOWLEDGE_SCHEMA)

    @property
    def knowledge(self):
        from .knowledge import Knowledge

        return Knowledge(self)

    def _conn(self):
        if self.dialect == "postgres":
            from .pg import connect

            return connect(self.path)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _task(self, db: sqlite3.Connection, task_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ControlPlaneError("not_found", f"task {task_id} not found")
        return row

    def _event(self, db: sqlite3.Connection, task_id: int | None, message: str, level: str = "info") -> None:
        db.execute(
            "INSERT INTO events(task_id, level, message, created_at) VALUES(?,?,?,?)",
            (task_id, level, redact(message), _now()),
        )

    def _touch(self, db: sqlite3.Connection, task_id: int, **fields: Any) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{key}=?" for key in fields)
        db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def _guard(self, task: sqlite3.Row, action: str, *, override_reason: str = "") -> None:
        if not task["protected"]:
            return
        if action not in PROTECTED_ACTIONS:
            return
        if override_reason.strip():
            return
        raise ControlPlaneError(
            "protected",
            f"task {task['id']} is protected; {action} requires an explicit override reason",
        )

    def create_account(
        self,
        *,
        account_id: str,
        provider: str,
        runtime_home: str,
        profile: str = "default",
        max_workers: int = 1,
        auth_state: str = "unknown",
        **rejected: Any,
    ) -> dict[str, Any]:
        if rejected:
            raise ControlPlaneError("rejected", "account records do not store credentials")
        if any(part in account_id.lower() for part in ("token", "secret", "password")):
            raise ControlPlaneError("rejected", "account id must not carry a credential")
        home = str(Path(runtime_home))
        with self._conn() as db:
            db.execute(
                """
                INSERT INTO accounts(
                  id, provider, profile, runtime_home, max_workers, auth_state, created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (account_id, provider, profile, home, max_workers, auth_state, _now()),
            )
        return self.account(account_id)

    def account(self, account_id: str) -> dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise ControlPlaneError("not_found", f"account {account_id} not found")
        return self._public_account(row)

    def accounts(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [self._public_account(row) for row in rows]

    def _public_account(self, row: sqlite3.Row) -> dict[str, Any]:
        data = {key: row[key] for key in row.keys() if key != "runtime_home"}  # noqa: SIM118
        data["runtime_home_set"] = bool(row["runtime_home"])
        data["runtime_home_hash"] = hashlib.sha256(row["runtime_home"].encode()).hexdigest()[:12]
        data["constraint_reason"] = redact(row["constraint_reason"])
        data["quota_state"] = self._quota_state(row["id"])
        return data

    def _quota_state(self, account_id: str) -> str:
        with self._conn() as db:
            rows = db.execute(
                "SELECT value_pct, status, observed_at FROM quota_observations WHERE account_id=?",
                (account_id,),
            ).fetchall()
        fresh = [row for row in rows if row["value_pct"] is not None and _fresh(str(row["observed_at"]))]
        if not fresh:
            return "unknown"
        score = min(float(row["value_pct"]) for row in fresh)
        if score <= 0:
            return "exhausted"
        if score < CRITICAL_PCT:
            return "critical"
        if score < CONSERVE_PCT:
            return "conserve"
        return "healthy"

    def set_constrained(self, account_id: str, *, constrained: bool, reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE accounts SET constrained=?, constraint_reason=? WHERE id=?",
                (1 if constrained else 0, redact(reason), account_id),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"account {account_id} not found")
        return self.account(account_id)

    def set_enabled(self, account_id: str, enabled: bool) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE accounts SET enabled=? WHERE id=?",
                (1 if enabled else 0, account_id),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"account {account_id} not found")
        return self.account(account_id)

    def drain(self, account_id: str) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute("UPDATE accounts SET drained=1 WHERE id=?", (account_id,))
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"account {account_id} not found")
            self._audit(db, account_id, "drain", "")
        self._auto_handoff(self._assigned_tasks(account_id, ("running", "needs_input")), "account_switch")
        return self.account(account_id)

    def enable(self, account_id: str) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE accounts SET enabled=1, drained=0, constrained=0 WHERE id=?",
                (account_id,),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"account {account_id} not found")
            self._audit(db, account_id, "enable", "")
        return self.account(account_id)

    def _audit(self, db, account_id: str, action: str, detail: str) -> None:
        db.execute(
            "INSERT INTO account_events(account_id, action, detail, created_at) VALUES(?,?,?,?)",
            (account_id, action, redact(detail), _now()),
        )

    def mark_provider_failure(self, account_id: str, reason: str) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE accounts SET status='provider_failed', constraint_reason=? WHERE id=?",
                (redact(reason), account_id),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"account {account_id} not found")
        self._auto_handoff(self._assigned_tasks(account_id, ("running", "needs_input")), "provider_switch")
        return self.account(account_id)

    def observe_quota(
        self,
        account_id: str,
        pool: str,
        window: str,
        *,
        value_pct: float | None,
        status: str,
        source: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        if window not in {"weekly", "five_hour"}:
            raise ControlPlaneError("rejected", "window must be weekly or five_hour")
        if status == "unavailable" and value_pct is not None:
            raise ControlPlaneError("rejected", "unavailable quota must not invent a percentage")
        if value_pct is None and status == "observed":
            raise ControlPlaneError("rejected", "observed quota requires an explicit percentage")
        with self._conn() as db:
            if db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise ControlPlaneError("not_found", f"account {account_id} not found")
            db.execute(
                """
                INSERT INTO quota_observations(
                  account_id, pool, quota_window, value_pct, status, source, observed_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(account_id, pool, quota_window) DO UPDATE SET
                  value_pct=excluded.value_pct,
                  status=excluded.status,
                  source=excluded.source,
                  observed_at=excluded.observed_at
                """,
                (account_id, pool, window, value_pct, status, source, observed_at or _now()),
            )
            row = db.execute(
                """
                SELECT account_id, pool, quota_window, value_pct, status, source, observed_at
                FROM quota_observations WHERE account_id=? AND pool=? AND quota_window=?
                """,
                (account_id, pool, window),
            ).fetchone()
        return dict(row)

    def quota(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT account_id, pool, quota_window, value_pct, status, source, observed_at FROM quota_observations ORDER BY 1,2,3"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(
        self,
        *,
        project: str,
        goal: str,
        manager_id: str = "",
        vicoa_project_id: str = "",
        source_system: str = "",
        source_id: str = "",
        import_hold: bool = False,
        status: str = "planned",
    ) -> dict[str, Any]:
        now = _now()
        with self._conn() as db:
            cur = db.execute(
                """
                INSERT INTO jobs(
                  project, goal, status, manager_id, vicoa_project_id, source_system,
                  source_id, import_hold, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    project,
                    redact(goal),
                    status,
                    manager_id,
                    vicoa_project_id,
                    source_system,
                    source_id,
                    1 if import_hold else 0,
                    now,
                    now,
                ),
            )
            job_id = int(cur.lastrowid)
        return self.job(job_id)

    def job(self, job_id: int) -> dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ControlPlaneError("not_found", f"job {job_id} not found")
            tasks = db.execute(
                "SELECT id, title, worker_status, verification_status, protected, owner_only, import_hold, source_id FROM tasks WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
        data = dict(row)
        data["tasks"] = [dict(task) for task in tasks]
        return data

    def jobs(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT id FROM jobs ORDER BY id").fetchall()
        return [self.job(int(row["id"])) for row in rows]

    def add_task(
        self,
        job_id: int,
        *,
        title: str,
        plan_key: str,
        prompt: str = "",
        project: str = "",
        acceptance_criteria: str = "",
        worker_status: str = "queued",
        verification_status: str = "not_started",
        protected: bool = False,
        protection_reason: str = "",
        owner_only: bool = False,
        owner_blocker: str = "",
        import_hold: bool = False,
        source_system: str = "",
        source_id: str = "",
        vicoa_task_id: str = "",
        account_id: str | None = None,
        session_id: str = "",
        queued_prompts: int = 0,
    ) -> dict[str, Any]:
        now = _now()
        with self._conn() as db:
            if db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise ControlPlaneError("not_found", f"job {job_id} not found")
            cur = db.execute(
                """
                INSERT INTO tasks(
                  job_id, plan_key, title, prompt, project, worker_status, verification_status,
                  protected, protection_reason, owner_only, owner_blocker, import_hold,
                  source_system, source_id, vicoa_task_id, account_id, session_id,
                  queued_prompts, steer_consumed, created_at, updated_at, acceptance_criteria
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    plan_key,
                    redact(title),
                    redact(prompt),
                    project,
                    worker_status,
                    verification_status,
                    1 if protected else 0,
                    protection_reason,
                    1 if owner_only else 0,
                    owner_blocker,
                    1 if import_hold else 0,
                    source_system,
                    source_id,
                    vicoa_task_id,
                    account_id,
                    session_id,
                    queued_prompts,
                    0 if queued_prompts else 1,
                    now,
                    now,
                    redact(acceptance_criteria),
                ),
            )
            task_id = int(cur.lastrowid)
            self._event(db, task_id, f"task created status={worker_status}")
        return self.task(task_id)

    def task(self, task_id: int) -> dict[str, Any]:
        with self._conn() as db:
            row = self._task(db, task_id)
            deps = [
                int(item["depends_on_task_id"])
                for item in db.execute(
                    "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=? ORDER BY 1",
                    (task_id,),
                )
            ]
            evidence = [
                dict(item)
                for item in db.execute(
                    "SELECT id, status, evidence_json, created_at FROM verifications WHERE task_id=? ORDER BY id",
                    (task_id,),
                )
            ]
        data = dict(row)
        data["prompt"] = redact(data["prompt"])
        data["worker_output"] = redact(data["worker_output"])
        data["depends_on"] = deps
        data["evidence"] = evidence
        data["startable"] = self._startable(row, deps, self._dep_states(task_id))
        return data

    def _dep_states(self, task_id: int) -> list[str]:
        with self._conn() as db:
            rows = db.execute(
                """
                SELECT upstream.verification_status AS verification_status
                FROM task_dependencies dep
                JOIN tasks upstream ON upstream.id = dep.depends_on_task_id
                WHERE dep.task_id=?
                """,
                (task_id,),
            ).fetchall()
        return [row["verification_status"] for row in rows]

    def _startable(self, task: sqlite3.Row, deps: list[int], states: list[str]) -> bool:
        if task["protected"] or task["owner_only"] or task["import_hold"]:
            return False
        if task["worker_status"] not in {"queued", "interrupted"}:
            return False
        return all(state == "passed" for state in states) and len(states) == len(deps)

    def link_vicoa_task(self, task_id: int, vicoa_task_id: str) -> dict[str, Any]:
        return self.mutate(task_id, vicoa_task_id=vicoa_task_id)

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        if task_id == depends_on_task_id:
            raise ControlPlaneError("cycle", "a task cannot depend on itself")
        with self._conn() as db:
            self._task(db, task_id)
            self._task(db, depends_on_task_id)
            if self._reaches(db, depends_on_task_id, task_id):
                raise ControlPlaneError("cycle", "dependency would create a cycle")
            db.execute(
                "INSERT INTO task_dependencies(task_id, depends_on_task_id) VALUES(?,?)",
                (task_id, depends_on_task_id),
            )

    def _reaches(self, db: sqlite3.Connection, start: int, target: int) -> bool:
        seen: set[int] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(
                int(row["depends_on_task_id"])
                for row in db.execute(
                    "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?",
                    (current,),
                )
            )
        return False

    def start_task(self, task_id: int, *, account_id: str, session_id: str, override_reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "resume", override_reason=override_reason)
            if task["owner_only"]:
                raise ControlPlaneError("owner_only", "owner-only blocker stops automation")
            if task["import_hold"]:
                raise ControlPlaneError("import_hold", "imported work is held until the owner releases it")
            job = db.execute("SELECT shadow FROM jobs WHERE id=?", (task["job_id"],)).fetchone()
            if job and job["shadow"]:
                raise ControlPlaneError("shadow", "shadow mode does not dispatch imported live work")
            deps = db.execute(
                """
                SELECT upstream.verification_status AS verification_status
                FROM task_dependencies dep
                JOIN tasks upstream ON upstream.id = dep.depends_on_task_id
                WHERE dep.task_id=?
                """,
                (task_id,),
            ).fetchall()
            if any(row["verification_status"] != "passed" for row in deps):
                raise ControlPlaneError("dependency", "upstream verification has not passed")
            from .knowledge import assert_fresh_session

            assert_fresh_session(db, task_id, session_id)
            if task["worker_status"] == "running" and task["session_id"] == session_id:
                return self.task(task_id)
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None or not account["enabled"] or account["drained"] or account["constrained"]:
                raise ControlPlaneError("account", "account is not eligible to start work")
            if account["status"] == "provider_failed":
                raise ControlPlaneError("account", "account has a provider failure")
            if account["active_workers"] >= account["max_workers"]:
                raise ControlPlaneError("capacity", "account is at worker capacity")
            self._touch(
                db,
                task_id,
                worker_status="running",
                account_id=account_id,
                session_id=session_id,
                stale=0,
                heartbeat_at=_now(),
            )
            db.execute(
                "UPDATE accounts SET active_workers=active_workers+1 WHERE id=?",
                (account_id,),
            )
            if override_reason.strip():
                self._event(db, task_id, f"protected override used for resume: {override_reason}")
            self._event(db, task_id, f"started on {account_id} session {session_id}")
        return self.task(task_id)

    def complete_worker(
        self,
        task_id: int,
        *,
        worktree_path: str = "",
        worktree: str = "",
        branch: str = "",
        output: str = "",
        base_ref: str = "",
    ) -> dict[str, Any]:
        worktree_path = worktree_path or worktree
        with self._conn() as db:
            task = self._task(db, task_id)
            if task["worker_status"] == "candidate_complete" and task["verification_status"] == "pending":
                return self.task(task_id)
            if task["worker_status"] not in {"running", "needs_input", "interrupted"}:
                raise ControlPlaneError("state", "worker is not in a completable state")
            self._touch(
                db,
                task_id,
                worker_status="candidate_complete",
                verification_status="pending",
                worktree_path=worktree_path,
                branch=branch,
                base_ref=base_ref,
                worker_output=redact(output),
            )
            if task["account_id"]:
                db.execute(
                    "UPDATE accounts SET active_workers=MAX(active_workers-1, 0) WHERE id=?",
                    (task["account_id"],),
                )
            self._event(db, task_id, "worker finished; verification still pending; dependencies stay locked")
        return self.task(task_id)

    def verify_task(self, task_id: int, checks: list[dict[str, Any]], *, worktree: str | None = None) -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            if task["verification_status"] == "passed":
                prior = db.execute(
                    "SELECT evidence_json FROM verifications WHERE task_id=? AND status='passed' ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                return {
                    "task_id": task_id,
                    "status": "passed",
                    "idempotent": True,
                    "verification_status": "passed",
                    "evidence": json.loads(prior["evidence_json"]) if prior else {},
                }
            root = Path(worktree or task["worktree_path"])
            if not root.is_dir():
                raise ControlPlaneError("worktree", "verification worktree does not exist")
            try:
                results = [_run_check(root, task, check) for check in checks]
            except ValueError as exc:
                raise ControlPlaneError("rejected", str(exc)) from exc
            passed = all(item["passed"] for item in results)
            status = "passed" if passed else "revision_required"
            evidence = {"checks": results, "worktree": str(root), "idempotent": False}
            db.execute(
                "INSERT INTO verifications(task_id, status, evidence_json, created_at) VALUES(?,?,?,?)",
                (task_id, status, _json(evidence), _now()),
            )
            self._touch(
                db,
                task_id,
                verification_status=status,
                verification_summary=redact("; ".join(item["summary"] for item in results)[:500]),
            )
            self._event(db, task_id, f"verification {status}")
        if status == "revision_required":
            self._auto_handoff([task_id], "verification_failure")
        return {"task_id": task_id, "status": status, "verification_status": status, "idempotent": False, "evidence": evidence}

    def route(self, task_id: int, *, override_reason: str = "", pool: str | None = None) -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "route", override_reason=override_reason)
            if task["owner_only"]:
                raise ControlPlaneError("owner_only", "owner-only blocker stops routing")
            if task["import_hold"]:
                raise ControlPlaneError("import_hold", "imported work is held")
            accounts = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            observations = db.execute("SELECT * FROM quota_observations").fetchall()
            chosen, considered, summary = _choose_account(accounts, observations, pool)
            db.execute(
                """
                INSERT INTO route_decisions(task_id, account_id, summary, detail_json, decided_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                  account_id=excluded.account_id,
                  summary=excluded.summary,
                  detail_json=excluded.detail_json,
                  decided_at=excluded.decided_at
                """,
                (task_id, chosen, summary, _json(considered), _now()),
            )
            if override_reason.strip():
                self._event(db, task_id, f"protected override used for route: {override_reason}")
        explanations = self.knowledge.explain_route(
            task_id,
            chosen=chosen,
            summary=summary,
            considered=considered,
            record=True,
        )
        if chosen is None:
            raise ControlPlaneError("unroutable", summary)
        return {
            "task_id": task_id,
            "account_id": chosen,
            "summary": summary,
            "considered": considered,
            "explanations": explanations,
        }

    def open_approval(self, task_id: int, *, prompt_text: str) -> dict[str, Any]:
        fingerprint = hashlib.sha256(prompt_text.encode()).hexdigest()
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "approve")
            cur = db.execute(
                """
                INSERT INTO approvals(task_id, fingerprint, prompt_text, status)
                VALUES(?,?,?, 'pending')
                """,
                (task_id, fingerprint, redact(prompt_text)),
            )
            approval_id = int(cur.lastrowid)
        return self.approval(approval_id)

    def approval(self, approval_id: int) -> dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ControlPlaneError("not_found", f"approval {approval_id} not found")
        return dict(row)

    def approve_once(
        self,
        approval_id: int,
        *,
        prompt_text: str,
        confirm_permanent: bool = False,
        override_reason: str = "",
    ) -> dict[str, Any]:
        return self._decide(
            approval_id,
            decision="approved_once",
            prompt_text=prompt_text,
            confirm_permanent=confirm_permanent,
            override_reason=override_reason,
        )

    def deny(self, approval_id: int, *, prompt_text: str, override_reason: str = "") -> dict[str, Any]:
        return self._decide(
            approval_id,
            decision="denied",
            prompt_text=prompt_text,
            confirm_permanent=False,
            override_reason=override_reason,
        )

    def _decide(
        self,
        approval_id: int,
        *,
        decision: str,
        prompt_text: str,
        confirm_permanent: bool,
        override_reason: str,
    ) -> dict[str, Any]:
        if _ambiguous(prompt_text):
            raise ControlPlaneError("ambiguous", "refusing to decide an empty or ambiguous prompt")
        fingerprint = hashlib.sha256(prompt_text.encode()).hexdigest()
        with self._conn() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise ControlPlaneError("not_found", f"approval {approval_id} not found")
            task = self._task(db, int(row["task_id"]))
            self._guard(task, "approve", override_reason=override_reason)
            if row["consumed"]:
                raise ControlPlaneError("consumed", "approval was already used")
            if row["fingerprint"] != fingerprint:
                db.execute(
                    "UPDATE approvals SET status='stale', decided_at=? WHERE id=?",
                    (_now(), approval_id),
                )
                raise ControlPlaneError("stale", "prompt fingerprint does not match this approval")
            permanent = 0
            if confirm_permanent:
                permanent = 1
                db.execute(
                    "INSERT INTO allow_rules(scope, fingerprint, created_at) VALUES('fingerprint', ?, ?)",
                    (fingerprint, _now()),
                )
            db.execute(
                """
                UPDATE approvals
                SET status=?, consumed=1, permanent=?, decided_at=?
                WHERE id=?
                """,
                (decision, permanent, _now(), approval_id),
            )
            if override_reason.strip():
                self._event(db, int(row["task_id"]), f"protected override used for approve: {override_reason}")
        return self.approval(approval_id)

    def message(self, task_id: int, text: str, *, override_reason: str = "") -> dict[str, Any]:
        if not text.strip():
            raise ControlPlaneError("rejected", "empty steer is not queued")
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "message", override_reason=override_reason)
            self._touch(
                db,
                task_id,
                queued_prompts=int(task["queued_prompts"]) + 1,
                steer_consumed=0,
                steer_text=redact(text),
            )
            self._event(db, task_id, "steer queued and not yet consumed")
        return self.task(task_id)

    def consume_steer(self, task_id: int) -> dict[str, Any]:
        with self._conn() as db:
            self._task(db, task_id)
            self._touch(db, task_id, steer_consumed=1, queued_prompts=0, steer_text="")
            self._event(db, task_id, "steer consumed")
        return self.task(task_id)

    def crash_worker(self, task_id: int) -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._touch(db, task_id, worker_status="interrupted", stale=1)
            if task["account_id"] and task["worker_status"] == "running":
                db.execute(
                    "UPDATE accounts SET active_workers=MAX(active_workers-1, 0) WHERE id=?",
                    (task["account_id"],),
                )
            self._event(db, task_id, "worker crashed; evidence retained")
        self._auto_handoff([task_id], "crash")
        return self.task(task_id)

    def mark_stale(self, task_id: int) -> dict[str, Any]:
        with self._conn() as db:
            self._task(db, task_id)
            self._touch(db, task_id, stale=1)
            self._event(db, task_id, "session marked stale")
        return self.task(task_id)

    def recover_after_restart(self) -> list[int]:
        recovered: list[int] = []
        with self._conn() as db:
            rows = db.execute(
                "SELECT id FROM tasks WHERE worker_status='running'"
            ).fetchall()
            for row in rows:
                task_id = int(row["id"])
                self._touch(db, task_id, worker_status="interrupted", stale=1)
                self._event(db, task_id, "restart recovery; verification rows kept")
                recovered.append(task_id)
            db.execute("UPDATE accounts SET active_workers=0")
        self._auto_handoff(recovered, "crash")
        return recovered

    def _assigned_tasks(self, account_id: str, statuses: tuple[str, ...]) -> list[int]:
        marks = ",".join("?" for _ in statuses)
        with self._conn() as db:
            rows = db.execute(
                f"SELECT id FROM tasks WHERE account_id=? AND worker_status IN ({marks}) ORDER BY id",
                (account_id, *statuses),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def _auto_handoff(self, task_ids: list[int], reason: str) -> None:
        for task_id in task_ids:
            try:
                self.knowledge.prepare_handoff(task_id, reason=reason)
            except ControlPlaneError:
                continue

    def cleanup_session(self, task_id: int, *, evidence_root: str, override_reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "reap", override_reason=override_reason)
            if task["worker_status"] in {"running", "needs_input"}:
                raise ControlPlaneError("active", "active work is never reaped")
            dest = Path(evidence_root) / f"task-{task_id}"
            dest.mkdir(parents=True, exist_ok=True)
            evidence_path = dest / "evidence.json"
            rows = db.execute(
                "SELECT id, status, evidence_json, created_at FROM verifications WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
            evidence_path.write_text(_json([dict(row) for row in rows]))
            self._touch(
                db,
                task_id,
                session_id="",
                reap_state="reaped",
                evidence_dir=str(dest),
            )
            self._event(db, task_id, f"session cleaned; evidence kept at {dest}")
        return self.task(task_id)

    def enqueue_message(self, task_id: int, body: str, *, idempotency_key: str) -> dict[str, Any]:
        if not body.strip() or not idempotency_key.strip():
            raise ControlPlaneError("rejected", "message body and idempotency key are required")
        now = _now()
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "message")
            existing = db.execute(
                "SELECT * FROM steer_messages WHERE task_id=? AND idempotency_key=?",
                (task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            cur = db.execute(
                """
                INSERT INTO steer_messages(
                  task_id, idempotency_key, body, state, attempt, created_at, updated_at
                ) VALUES(?,?,?, 'queued', 0, ?, ?)
                """,
                (task_id, idempotency_key, redact(body), now, now),
            )
            self._touch(
                db,
                task_id,
                queued_prompts=int(task["queued_prompts"]) + 1,
                steer_consumed=0,
                steer_text=redact(body),
            )
            self._event(db, task_id, "steer queued; not delivered")
            message_id = int(cur.lastrowid)
        return self.message_row(message_id)

    def message_row(self, message_id: int) -> dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM steer_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ControlPlaneError("not_found", f"message {message_id} not found")
        return dict(row)

    def messages(self, task_id: int) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM steer_messages WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def deliver_next(self, task_id: int, *, worker_accepting: bool) -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "message")
            row = db.execute(
                """
                SELECT * FROM steer_messages
                WHERE task_id=? AND state='queued'
                ORDER BY id LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return {"task_id": task_id, "delivered": False, "reason": "empty"}
            attempt = int(row["attempt"]) + 1
            if not worker_accepting or task["worker_status"] not in {"running", "needs_input"}:
                state = "failed" if attempt >= 3 else "queued"
                db.execute(
                    "UPDATE steer_messages SET attempt=?, state=?, error=?, updated_at=? WHERE id=?",
                    (attempt, state, "worker unavailable", _now(), row["id"]),
                )
                self._event(db, task_id, "steer not delivered; worker unavailable")
                message_id = int(row["id"])
                delivered = False
            else:
                db.execute(
                    "UPDATE steer_messages SET state='sent', attempt=?, updated_at=? WHERE id=?",
                    (attempt, _now(), row["id"]),
                )
                self._event(db, task_id, "steer sent; waiting for acknowledgement")
                message_id = int(row["id"])
                delivered = True
        row = self.message_row(message_id)
        row["delivered"] = delivered
        return row

    def acknowledge_message(self, message_id: int) -> dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM steer_messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise ControlPlaneError("not_found", f"message {message_id} not found")
            if row["state"] == "acknowledged":
                return dict(row)
            if row["state"] != "sent":
                raise ControlPlaneError("state", "only a sent message can be acknowledged")
            db.execute(
                "UPDATE steer_messages SET state='acknowledged', updated_at=? WHERE id=?",
                (_now(), message_id),
            )
            remaining = db.execute(
                "SELECT COUNT(*) AS n FROM steer_messages WHERE task_id=? AND state IN ('queued', 'sent')",
                (row["task_id"],),
            ).fetchone()["n"]
            self._touch(
                db,
                int(row["task_id"]),
                queued_prompts=int(remaining),
                steer_consumed=0 if remaining else 1,
            )
            self._event(db, int(row["task_id"]), "steer acknowledged")
        return self.message_row(message_id)

    def retry_message(self, message_id: int) -> dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM steer_messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise ControlPlaneError("not_found", f"message {message_id} not found")
            if row["state"] == "queued":
                return dict(row)
            if row["state"] != "failed":
                raise ControlPlaneError("state", "only a failed message can be retried")
            db.execute(
                "UPDATE steer_messages SET state='queued', error='', updated_at=? WHERE id=?",
                (_now(), message_id),
            )
            self._event(db, int(row["task_id"]), "failed steer returned to queued; no duplicate row")
        return self.message_row(message_id)

    def set_policy(self, task_id: int, policy: str) -> dict[str, Any]:
        if policy not in TASK_POLICIES:
            raise ControlPlaneError("rejected", f"unknown policy {policy}")
        with self._conn() as db:
            self._task(db, task_id)
            self._touch(db, task_id, policy=policy)
            self._event(db, task_id, f"policy set to {policy}")
        return self.task(task_id)

    def require_owner_action(self, task_id: int, action: str) -> None:
        kind = OWNER_ACTIONS.get(action)
        if kind is None:
            raise ControlPlaneError("rejected", f"unknown owner action {action}")
        raise ControlPlaneError("owner_only", f"OWNER ACTION REQUIRED: {action} ({kind})")

    def revoke_allow_rule(self, rule_id: int) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE allow_rules SET revoked=1 WHERE id=? AND revoked=0",
                (rule_id,),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"allow rule {rule_id} not found")
            row = db.execute("SELECT * FROM allow_rules WHERE id=?", (rule_id,)).fetchone()
        return dict(row)

    def set_shadow(self, job_id: int, enabled: bool) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute(
                "UPDATE jobs SET shadow=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, _now(), job_id),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"job {job_id} not found")
        return self.job(job_id)

    def claim_job(self, job_id: int, manager_id: str) -> dict[str, Any]:
        if not manager_id.strip():
            raise ControlPlaneError("rejected", "manager id is required")
        with self._conn() as db:
            cur = db.execute(
                "UPDATE jobs SET manager_id=?, updated_at=? WHERE id=? AND manager_id IN ('', ?)",
                (manager_id, _now(), job_id, manager_id),
            )
            if cur.rowcount != 1:
                raise ControlPlaneError("conflict", "job is owned by another manager")
        job = self.job(job_id)
        job["started"] = []
        return job

    def heartbeat_job(self, job_id: int) -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_now(), job_id))
            if cur.rowcount != 1:
                raise ControlPlaneError("not_found", f"job {job_id} not found")
        return {"job_id": job_id, "started": []}

    def _shadow_job_ids(self) -> list[int]:
        with self._conn() as db:
            return [int(row["id"]) for row in db.execute("SELECT id FROM jobs WHERE shadow=1 ORDER BY id")]

    def _message_states(self) -> dict[str, int]:
        with self._conn() as db:
            rows = db.execute("SELECT state, COUNT(*) AS n FROM steer_messages GROUP BY state").fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    def kill(self, task_id: int, *, override_reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "kill", override_reason=override_reason)
            self._touch(db, task_id, worker_status="killed")
            self._event(db, task_id, "kill requested")
        return self.task(task_id)

    def resume(self, task_id: int, *, override_reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "resume", override_reason=override_reason)
            if task["owner_only"] and not override_reason.strip():
                raise ControlPlaneError("owner_only", "owner-only blocker stops automation")
            self._touch(db, task_id, worker_status="queued", stale=0)
            if override_reason.strip():
                self._event(db, task_id, f"protected override used for resume: {override_reason}")
        return self.task(task_id)

    def reap(self, task_id: int, *, override_reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "reap", override_reason=override_reason)
            if task["worker_status"] in {"running", "needs_input"}:
                raise ControlPlaneError("active", "active work is never reaped")
            self._touch(db, task_id, reap_state="reaped", session_id="")
            self._event(db, task_id, "reap recorded; verification rows kept")
        return self.task(task_id)

    def tasks(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            ids = [int(row["id"]) for row in db.execute("SELECT id FROM tasks ORDER BY id")]
        return [self.task(task_id) for task_id in ids]

    def mutate(self, task_id: int, *, override_reason: str = "", **fields: Any) -> dict[str, Any]:
        allowed = {
            "title",
            "prompt",
            "acceptance_criteria",
            "vicoa_task_id",
            "owner_blocker",
            "import_hold",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ControlPlaneError("rejected", f"cannot mutate {sorted(unknown)}")
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "mutate", override_reason=override_reason)
            cleaned = {
                key: (redact(value) if isinstance(value, str) else value)
                for key, value in fields.items()
            }
            if cleaned:
                self._touch(db, task_id, **cleaned)
            if override_reason.strip():
                self._event(db, task_id, f"protected override used for mutate: {override_reason}")
        return self.task(task_id)

    def protect(self, task_id: int, reason: str) -> dict[str, Any]:
        with self._conn() as db:
            self._task(db, task_id)
            self._touch(
                db,
                task_id,
                protected=1,
                protection_reason=reason,
                worker_status="paused",
                import_hold=1,
            )
            self._event(db, task_id, f"protected: {reason}")
        return self.task(task_id)

    def set_owner_blocker(self, task_id: int, blocker: str) -> dict[str, Any]:
        with self._conn() as db:
            self._task(db, task_id)
            self._touch(db, task_id, owner_only=1, owner_blocker=blocker, import_hold=1)
            self._event(db, task_id, "owner-only blocker set")
        return self.task(task_id)

    def release_import_hold(self, task_id: int, *, override_reason: str = "") -> dict[str, Any]:
        with self._conn() as db:
            task = self._task(db, task_id)
            self._guard(task, "mutate", override_reason=override_reason)
            if task["owner_only"] and not override_reason.strip():
                raise ControlPlaneError("owner_only", "owner-only blocker stops automation")
            self._touch(db, task_id, import_hold=0)
        return self.task(task_id)

    def automation_tick(self) -> dict[str, Any]:
        """One bounded pass. Does not schedule itself and does not bypass blockers."""
        stopped: list[dict[str, Any]] = []
        retried: list[int] = []
        attention: list[int] = []
        with self._conn() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        for task in rows:
            task_id = int(task["id"])
            if task["owner_only"] or task["policy"] in STOP_POLICIES:
                stopped.append({"task_id": task_id, "reason": "owner_only" if task["owner_only"] else task["policy"]})
                continue
            if task["protected"] or task["import_hold"]:
                stopped.append({"task_id": task_id, "reason": "held"})
                continue
            if task["stale"] or (task["queued_prompts"] and not task["steer_consumed"]):
                attention.append(task_id)
        return {"stopped": stopped, "attention": attention, "retried": retried, "loop": False}

    def retry_verification(self, task_id: int, checks: list[dict[str, Any]]) -> dict[str, Any]:
        task = self.task(task_id)
        if task["verification_status"] == "passed":
            return self.verify_task(task_id, checks)
        if task["owner_only"]:
            raise ControlPlaneError("owner_only", "owner-only blocker stops automation")
        if any(check.get("type") == "command" for check in checks):
            raise ControlPlaneError("unsafe_retry", "command verification is not auto-retried")
        return self.verify_task(task_id, checks)

    def health(self) -> dict[str, Any]:
        with self._conn() as db:
            stale = [
                int(row["id"])
                for row in db.execute("SELECT id FROM tasks WHERE stale=1 ORDER BY id")
            ]
            unconsumed = [
                int(row["id"])
                for row in db.execute(
                    "SELECT id FROM tasks WHERE queued_prompts>0 AND steer_consumed=0 ORDER BY id"
                )
            ]
            owner = [
                {"task_id": int(row["id"]), "blocker": row["owner_blocker"]}
                for row in db.execute("SELECT id, owner_blocker FROM tasks WHERE owner_only=1 ORDER BY id")
            ]
            protected = [
                int(row["id"])
                for row in db.execute("SELECT id FROM tasks WHERE protected=1 ORDER BY id")
            ]
        return {
            "stale_sessions": stale,
            "unconsumed_steers": unconsumed,
            "owner_blockers": owner,
            "protected_tasks": protected,
        }

    def portfolio(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                """
                SELECT project,
                       COUNT(*) AS jobs,
                       SUM(CASE WHEN status='needs_attention' THEN 1 ELSE 0 END) AS attention
                FROM jobs GROUP BY project ORDER BY project
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def blockers(self) -> list[dict[str, Any]]:
        health = self.health()
        items = [{"kind": "owner_only", **row} for row in health["owner_blockers"]]
        items.extend({"kind": "protected", "task_id": task_id} for task_id in health["protected_tasks"])
        items.extend({"kind": "unconsumed_steer", "task_id": task_id} for task_id in health["unconsumed_steers"])
        items.extend({"kind": "stale_session", "task_id": task_id} for task_id in health["stale_sessions"])
        return items

    def status(self) -> dict[str, Any]:
        with self._conn() as db:
            counts = {
                "accounts": db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
                "jobs": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "tasks": db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "allow_rules": db.execute("SELECT COUNT(*) FROM allow_rules").fetchone()[0],
            }
        return {
            "product": "vicoa",
            "control_plane": "native",
            "counts": counts,
            "health": self.health(),
            "accounts": self.accounts(),
            "jobs": self.jobs(),
            "blockers": self.blockers(),
            "shadow_jobs": self._shadow_job_ids(),
            "message_states": self._message_states(),
            "tasks": self.task_board(),
            "approvals": self.approval_board(),
        }

    def task_board(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                """
                SELECT id, title, worker_status, verification_status, account_id, session_id,
                       CASE WHEN worktree_path = '' THEN 0 ELSE 1 END AS worktree_set,
                       protected, owner_only, import_hold, policy, source_id
                FROM tasks ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def approval_board(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, task_id, status, permanent, consumed FROM approvals ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def import_agent_control(
        self,
        source: str | Path,
        *,
        observations: list[dict[str, Any]] | None = None,
        protect_source_ids: tuple[int, ...] = (6,),
    ) -> dict[str, Any]:
        source_path = Path(source).resolve()
        if source_path == LIVE_AGENT_CONTROL_DB.resolve():
            raise ControlPlaneError(
                "live_db_refused",
                "refusing to import the live Agent Control database; import a snapshot copy",
            )
        before = source_path.read_bytes()
        uri = f"file:{source_path}?mode=ro"
        src = sqlite3.connect(uri, uri=True)
        src.row_factory = sqlite3.Row
        try:
            src.execute("CREATE TABLE refuse_write(id INTEGER)")
        except sqlite3.OperationalError:
            pass
        else:
            src.close()
            raise ControlPlaneError("not_readonly", "source connection was writable")
        account_map: dict[str, str] = {}
        for row in src.execute("SELECT * FROM accounts"):
            account_id = str(row["id"])
            account_map[account_id] = account_id
            if self._missing_account(account_id):
                self.create_account(
                    account_id=account_id,
                    provider=row["provider"],
                    runtime_home=row["runtime_home"] or f"/imported/{account_id}",
                    profile=row["profile"] or "default",
                    auth_state="not_copied",
                )
            if str(row["runtime_home"] or "").startswith("/home/agentctl"):
                self.drain(account_id)
        job_map: dict[int, int] = {}
        for row in src.execute("SELECT * FROM jobs ORDER BY id"):
            existing = self._source_job(str(row["id"]))
            if existing is not None:
                job_map[int(row["id"])] = existing
                continue
            created = self.create_job(
                project=row["project"],
                goal=redact(row["goal"] or ""),
                manager_id=str(_field(row, "manager_id")),
                source_system="agent-control",
                source_id=str(row["id"]),
                import_hold=True,
                status=row["status"] or "planned",
            )
            job_map[int(row["id"])] = int(created["id"])
        task_map: dict[int, int] = {}
        obs = {str(item.get("title")): item for item in observations or []}
        for row in src.execute("SELECT * FROM tasks ORDER BY id"):
            source_id = int(row["id"])
            protected = source_id in protect_source_ids or (row["atrium_session"] or "") == "task-6"
            title = row["title"] or ""
            observed = obs.get(title) or obs.get(row["atrium_session"] or "") or {}
            existing = self._source_task(str(source_id))
            if existing is not None:
                task_map[source_id] = existing
                continue
            job_id = job_map.get(row["job_id"]) if row["job_id"] is not None else None
            if job_id is None:
                held = self.create_job(
                    project=row["project"] or "",
                    goal=f"imported task {source_id}",
                    source_system="agent-control",
                    source_id=f"task-{source_id}",
                    import_hold=True,
                    status="paused" if protected else "planned",
                )
                job_id = int(held["id"])
            created = self.add_task(
                job_id,
                title=title,
                plan_key=row["plan_key"] or f"source-{source_id}",
                prompt=redact(row["prompt"] or ""),
                project=row["project"] or "",
                worker_status="paused" if protected else (row["status"] or "queued"),
                verification_status=row["verification_status"] or "pending",
                protected=protected,
                protection_reason="source task-6 remains paused; imported as a locked copy" if protected else "",
                import_hold=True,
                source_system="agent-control",
                source_id=str(source_id),
                account_id=row["account_id"] if row["account_id"] in account_map else None,
                session_id="",
                queued_prompts=int(observed.get("queued_prompts") or 0),
            )
            task_map[source_id] = int(created["id"])
            if protected:
                self.protect(int(created["id"]), "source task-6 remains paused and untouched")
        for row in src.execute("SELECT task_id, depends_on_task_id FROM task_dependencies"):
            if row["task_id"] in task_map and row["depends_on_task_id"] in task_map:
                self.add_dependency(task_map[row["task_id"]], task_map[row["depends_on_task_id"]])
        src.close()
        with self._conn() as db:
            db.execute(
                "UPDATE jobs SET shadow=1 WHERE source_system='agent-control'"
            )
        after = source_path.read_bytes()
        if before != after:
            raise ControlPlaneError("source_mutated", "import changed the source snapshot")
        return {
            "source_sha256": hashlib.sha256(before).hexdigest(),
            "accounts": len(account_map),
            "jobs": len(job_map),
            "tasks": len(task_map),
            "protected_source_ids": list(protect_source_ids),
            "task_map_sample": {str(key): task_map[key] for key in (6, 113, 114) if key in task_map},
            "live_db_written": False,
        }

    def _missing_account(self, account_id: str) -> bool:
        with self._conn() as db:
            return db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None

    def _source_job(self, source_id: str) -> int | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE source_system='agent-control' AND source_id=?",
                (source_id,),
            ).fetchone()
        return int(row["id"]) if row else None

    def _source_task(self, source_id: str) -> int | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT id FROM tasks WHERE source_system='agent-control' AND source_id=?",
                (source_id,),
            ).fetchone()
        return int(row["id"]) if row else None


def _field(row: sqlite3.Row, name: str, default: str = "") -> str:
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    if value is None:
        return default
    return str(value)


def _choose_account(accounts, observations, pool: str | None) -> tuple[str | None, list[dict[str, Any]], str]:
    health = []
    for account in accounts:
        reason = _route_block(account)
        health.append({"account_id": account["id"], "eligible": reason is None, "reason": reason or "eligible"})
    if pool is None:
        chosen = next((item["account_id"] for item in health if item["eligible"]), None)
        summary = f"routed to {chosen}" if chosen else "no eligible account"
        return chosen, health, summary
    scores: dict[str, float | None] = {}
    notes: dict[str, str] = {}
    for account in accounts:
        account_id = account["id"]
        block = _route_block(account)
        if block:
            scores[account_id] = None
            notes[account_id] = block
            continue
        rows = [row for row in observations if row["account_id"] == account_id and row["pool"] == pool]
        if any(row["status"] == "limited_access" for row in rows):
            scores[account_id] = None
            notes[account_id] = "limited_access"
            continue
        fresh = [row for row in rows if row["value_pct"] is not None and _fresh(row["observed_at"])]
        stale = [row for row in rows if row["value_pct"] is not None and not _fresh(row["observed_at"])]
        if not fresh:
            scores[account_id] = None
            notes[account_id] = "stale" if stale else "missing"
            continue
        scores[account_id] = min(float(row["value_pct"]) for row in fresh)
        if scores[account_id] < CRITICAL_PCT:
            notes[account_id] = "critical"
            scores[account_id] = None
        elif scores[account_id] < CONSERVE_PCT:
            notes[account_id] = "conserve"
        else:
            notes[account_id] = "fresh"
    eligible = {key: value for key, value in scores.items() if value is not None and value >= CRITICAL_PCT}
    conserve = {key: value for key, value in eligible.items() if value < CONSERVE_PCT}
    preferred = {key: value for key, value in eligible.items() if key not in conserve}
    chosen = None
    summary = f"no eligible {pool} account"
    if preferred:
        chosen = max(preferred, key=preferred.get)
        summary = f"selected {chosen} / {pool} because it is above the conserve threshold"
    elif conserve:
        best = max(conserve, key=conserve.get)
        better = [key for key, value in preferred.items() if value >= conserve[best] + MIN_ADVANTAGE_PCT]
        if better:
            chosen = better[0]
            summary = f"selected {chosen} / {pool} because {best} {pool} is conserve-constrained"
        else:
            chosen = best
            summary = f"selected {chosen} / {pool}; conserve-constrained and no healthier alternative"
    considered = []
    for account in accounts:
        account_id = account["id"]
        considered.append(
            {
                "account_id": account_id,
                "pool": pool,
                "eligible": account_id == chosen or scores.get(account_id) is not None,
                "reason": notes.get(account_id, "missing"),
                "score": scores.get(account_id),
            }
        )
    if chosen and any(notes.get(key) == "conserve" or (scores.get(key) is not None and scores[key] < CONSERVE_PCT and key != chosen) for key in scores):
        constrained = [key for key, value in scores.items() if value is not None and value < CONSERVE_PCT and key != chosen]
        if constrained:
            summary = f"selected {chosen} / {pool} because {constrained[0]} {pool} is conserve-constrained"
    return chosen, considered, summary


def _fresh(observed_at: str) -> bool:
    try:
        stamp = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - stamp < timedelta(hours=STALE_HOURS)


def _route_block(account: sqlite3.Row) -> str | None:
    if not account["enabled"]:
        return "disabled"
    if account["drained"]:
        return "drained"
    if account["constrained"]:
        return "constrained"
    if account["status"] == "provider_failed":
        return "provider_failed"
    if account["active_workers"] >= account["max_workers"]:
        return "at_capacity"
    return None


def _ambiguous(prompt_text: str) -> bool:
    text = prompt_text.strip()
    if len(text) < 12:
        return True
    lowered = text.lower()
    return "which file" in lowered or lowered in {"allow", "yes", "ok", "approve"}


def _run_check(root: Path, task: sqlite3.Row, check: dict[str, Any]) -> dict[str, Any]:
    kind = check.get("type")
    try:
        if kind == "file_exists":
            path = _safe(root, str(check.get("path") or ""))
            passed = path.is_file()
            return {"type": kind, "passed": passed, "summary": f"exists={passed}"}
        if kind == "file_contains":
            path = _safe(root, str(check.get("path") or ""))
            expected = str(check.get("text") or "")
            if not path.is_file():
                return {"type": kind, "passed": False, "summary": "missing file"}
            passed = expected in path.read_text()
            return {"type": kind, "passed": passed, "summary": f"contains={passed}"}
        if kind in {"git_changed", "git_changed_path"}:
            return _git_changed(root, task, str(check.get("path") or ""))
        if kind == "git_diff_nonempty":
            changed = _git_changed(root, task, ".")
            changed["type"] = kind
            return changed
        if kind == "command":
            return _command(root, check)
        if kind in {"worker_output", "output_contains"}:
            expected = str(check.get("contains") or "")
            passed = expected in (task["worker_output"] or "")
            return {"type": kind, "passed": passed, "summary": f"output_contains={passed}"}
        return {"type": str(kind), "passed": False, "summary": "unknown check"}
    except OSError as exc:
        return {"type": str(kind), "passed": False, "summary": type(exc).__name__}


def _safe(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("path must be relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes worktree")
    return path


def _git_changed(root: Path, task: sqlite3.Row, relative: str) -> dict[str, Any]:
    _safe(root, relative)
    base = task["base_ref"] or "HEAD"
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", relative],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return {"type": "git_changed", "passed": False, "summary": "git status failed"}
    dirty = bool(proc.stdout.strip())
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", base, "--", relative],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    changed = dirty or bool((diff.stdout or "").strip())
    return {"type": "git_changed", "passed": changed, "summary": f"changed={changed}"}


def _command(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    argv = check.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        return {"type": "command", "passed": False, "summary": "argv must be a string list"}
    timeout = int(check.get("timeout_seconds") or 5)
    timeout = max(1, min(timeout, 30))
    try:
        proc = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        return {"type": "command", "passed": False, "summary": "timeout"}
    passed = proc.returncode == 0
    return {"type": "command", "passed": passed, "summary": f"exit={proc.returncode}"}


def copy_evidence(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        shutil.copy2(src, dest)
