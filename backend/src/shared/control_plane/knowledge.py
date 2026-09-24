"""Shared skills, durable memory, context packs, and session handoff.

This extends the Vicoa control plane. It does not start workers, open a
shell, or read Agent Control's live database. A handoff prepares the next
session. The normal start_task path is still what launches work.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .store import SECRET_RE, ControlPlaneError, _json, _now, redact

EXTRA_SECRET_RE = re.compile(
    r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]+|ghp_[A-Za-z0-9]{20,})"
)
SCOPES = frozenset({"global", "project", "repo", "task_type", "account", "session"})
SCOPE_RANK = {"session": 6, "account": 5, "repo": 4, "project": 3, "task_type": 2, "global": 1}
AUTHORITIES = frozenset({"owner", "verified_evidence", "agent_inference", "untrusted_content"})
HIGH_AUTHORITY = frozenset({"owner", "verified_evidence"})
RISKS = frozenset({"low", "medium", "high", "blocked"})
SKILL_SOURCES = frozenset({"manual", "learned", "imported", "canary"})
HANDOFF_REASONS = frozenset(
    {
        "context_pressure",
        "provider_switch",
        "account_switch",
        "crash",
        "verification_failure",
        "owner_pause",
        "scheduled",
        "manual",
    }
)
PACKET_KEYS = (
    "objective",
    "current_state",
    "decisions",
    "evidence",
    "blockers",
    "attempted",
    "failed",
    "next_action",
    "files_changed",
    "commands_run",
    "verification",
    "risks",
    "open_questions",
    "owner_approvals",
    "active_skills",
    "relevant_memory",
    "context_budget",
    "resume_instructions",
)
PRESSURE_RATIO = 0.85
KNOWLEDGE_TABLES = (
    "work_attempts",
    "plane_events",
    "handoff_packets",
    "context_packs",
    "memory_conflicts",
    "memories",
    "skill_activations",
    "skill_versions",
)

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  scope TEXT NOT NULL,
  scope_ref TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL,
  body TEXT NOT NULL,
  inputs_json TEXT NOT NULL DEFAULT '[]',
  outputs_json TEXT NOT NULL DEFAULT '[]',
  preconditions_json TEXT NOT NULL DEFAULT '[]',
  verification_json TEXT NOT NULL DEFAULT '[]',
  rollback_json TEXT NOT NULL DEFAULT '{}',
  permissions_json TEXT NOT NULL DEFAULT '[]',
  compatible_agents_json TEXT NOT NULL DEFAULT '[]',
  required_tools_json TEXT NOT NULL DEFAULT '[]',
  required_secret_names_json TEXT NOT NULL DEFAULT '[]',
  risk TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  learned_from_task_id INTEGER,
  supersedes_id INTEGER,
  canary_status TEXT NOT NULL DEFAULT 'not_run',
  canary_evidence_json TEXT NOT NULL DEFAULT '{}',
  secrets_removed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(skill_key, version)
);
CREATE TABLE IF NOT EXISTS skill_activations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_key TEXT NOT NULL,
  version_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  scope_ref TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  topic TEXT NOT NULL,
  scope TEXT NOT NULL,
  scope_ref TEXT NOT NULL DEFAULT '',
  authority TEXT NOT NULL,
  statement TEXT NOT NULL,
  statement_hash TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  confidence TEXT NOT NULL,
  status TEXT NOT NULL,
  supersedes_id INTEGER,
  source_task_id INTEGER,
  source_session_id TEXT NOT NULL DEFAULT '',
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_conflicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  scope TEXT NOT NULL,
  scope_ref TEXT NOT NULL DEFAULT '',
  left_id INTEGER NOT NULL,
  right_id INTEGER NOT NULL,
  resolution TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_packs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  job_id INTEGER,
  session_id TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL,
  budget_tokens INTEGER NOT NULL,
  used_tokens INTEGER NOT NULL,
  pack_hash TEXT NOT NULL,
  sections_json TEXT NOT NULL,
  omitted_json TEXT NOT NULL,
  stale_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_packets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  job_id INTEGER NOT NULL,
  from_session_id TEXT NOT NULL DEFAULT '',
  to_session_id TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  packet_json TEXT NOT NULL,
  packet_hash TEXT NOT NULL,
  validation_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  resumed_at TEXT
);
CREATE TABLE IF NOT EXISTS plane_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  job_id INTEGER,
  session_id TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""


def tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def contains_secret(value: str) -> bool:
    return bool(SECRET_RE.search(value) or EXTRA_SECRET_RE.search(value))


def clean_text(value: str, limit: int = 4000) -> tuple[str, bool]:
    original = value or ""
    cleaned = redact(original)
    cleaned = EXTRA_SECRET_RE.sub("[redacted]", cleaned)
    removed = cleaned != original or contains_secret(original)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
        removed = True
    return cleaned, removed


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def assert_fresh_session(db, task_id: int, session_id: str) -> None:
    if not session_id:
        raise ControlPlaneError("session_reused", "a session id is required")
    row = db.execute(
        """
        SELECT id FROM handoff_packets
        WHERE task_id=? AND status='resumed' AND from_session_id=?
        """,
        (task_id, session_id),
    ).fetchone()
    if row is not None:
        raise ControlPlaneError("session_reused", "this session was already handed off")


def emit(
    db,
    plane,
    *,
    task_id: int | None,
    job_id: int | None,
    session_id: str,
    event_type: str,
    message: str,
    detail: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    clean, _removed = clean_text(message, limit=500)
    detail_text, _detail_removed = clean_text(canonical(detail or {}), limit=4000)
    db.execute(
        """
        INSERT INTO plane_events(
          task_id, job_id, session_id, event_type, level, message, detail_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (task_id, job_id, session_id or "", event_type, level, clean, detail_text, _now()),
    )
    if task_id is not None:
        plane._event(db, task_id, f"{event_type}: {clean}", level=level)


class Knowledge:
    def __init__(self, plane) -> None:
        self.plane = plane

    def register_skill(
        self,
        *,
        skill_key: str,
        name: str,
        description: str,
        body: str,
        scope: str = "global",
        scope_ref: str = "",
        risk: str = "low",
        source: str = "manual",
        inputs: list[Any] | None = None,
        outputs: list[Any] | None = None,
        preconditions: list[Any] | None = None,
        verification: list[Any] | None = None,
        rollback: dict[str, Any] | None = None,
        permissions: list[Any] | None = None,
        compatible_agents: list[Any] | None = None,
        required_tools: list[Any] | None = None,
        required_secret_names: list[str] | None = None,
        learned_from_task_id: int | None = None,
        supersedes_id: int | None = None,
    ) -> dict[str, Any]:
        key = _slug(skill_key)
        if scope not in SCOPES:
            raise ControlPlaneError("invalid", "skill scope is not recognized")
        if risk not in RISKS:
            raise ControlPlaneError("invalid", "skill risk is not recognized")
        if source not in SKILL_SOURCES:
            raise ControlPlaneError("invalid", "skill source is not recognized")
        names = [str(item) for item in (required_secret_names or [])]
        if any(contains_secret(item) or "=" in item or " " in item for item in names):
            raise ControlPlaneError("secret_rejected", "secret names must not contain values")
        cleaned, removed = clean_text(body, limit=8000)
        content_hash = digest(cleaned)
        with self.plane._conn() as db:
            if learned_from_task_id is not None:
                self.plane._guard(self.plane._task(db, learned_from_task_id), "mutate")
            prior = db.execute(
                "SELECT * FROM skill_versions WHERE skill_key=? AND content_hash=? ORDER BY version DESC LIMIT 1",
                (key, content_hash),
            ).fetchone()
            if prior is not None:
                found = _public_skill(prior, include_body=True)
                found["idempotent"] = True
                return found
            version = int(
                db.execute("SELECT COALESCE(MAX(version), 0) + 1 AS n FROM skill_versions WHERE skill_key=?", (key,)).fetchone()["n"]
            )
            cur = db.execute(
                """
                INSERT INTO skill_versions(
                  skill_key, version, name, description, scope, scope_ref, content_hash, body,
                  inputs_json, outputs_json, preconditions_json, verification_json, rollback_json,
                  permissions_json, compatible_agents_json, required_tools_json, required_secret_names_json,
                  risk, status, source, learned_from_task_id, supersedes_id, secrets_removed, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    version,
                    redact(name)[:200],
                    redact(description)[:500],
                    scope,
                    scope_ref,
                    content_hash,
                    cleaned,
                    _json(inputs or []),
                    _json(outputs or []),
                    _json(preconditions or []),
                    _json(verification or []),
                    _json(rollback or {}),
                    _json(permissions or []),
                    _json(compatible_agents or []),
                    _json(required_tools or []),
                    _json(names),
                    risk,
                    "candidate",
                    source,
                    learned_from_task_id,
                    supersedes_id,
                    1 if removed else 0,
                    _now(),
                ),
            )
            row = _must(db, "skill_versions", int(cur.lastrowid))
            emit(
                db,
                self.plane,
                task_id=learned_from_task_id,
                job_id=None,
                session_id="",
                event_type="skill_registered",
                message=f"registered {key} v{version} as candidate",
                detail={"skill_key": key, "version": version, "risk": risk, "secrets_removed": bool(removed)},
            )
        found = _public_skill(row, include_body=True)
        found["idempotent"] = False
        return found

    def record_canary(self, version_id: int, *, passed: bool, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        proof = evidence or {}
        if passed and not str(proof.get("summary") or "").strip():
            raise ControlPlaneError("canary_required", "a passing canary needs an evidence summary")
        summary, _removed = clean_text(str(proof.get("summary") or ""), limit=500)
        stored = {"summary": summary, "passed": bool(passed)}
        with self.plane._conn() as db:
            row = _must(db, "skill_versions", version_id)
            if row["risk"] == "blocked" and passed:
                raise ControlPlaneError("risk_blocked", "a blocked skill cannot pass a canary into activation")
            status = "passed" if passed else "failed"
            db.execute(
                "UPDATE skill_versions SET canary_status=?, canary_evidence_json=? WHERE id=?",
                (status, _json(stored), version_id),
            )
            emit(
                db,
                self.plane,
                task_id=row["learned_from_task_id"],
                job_id=None,
                session_id="",
                event_type="skill_canary_recorded",
                message=f"canary {status} for {row['skill_key']} v{row['version']}",
                detail={"version_id": version_id, "canary_status": status},
            )
            updated = _must(db, "skill_versions", version_id)
        return _public_skill(updated, include_body=False)

    def activate_skill(
        self,
        version_id: int,
        *,
        scope: str | None = None,
        scope_ref: str | None = None,
        owner_confirmed: bool = False,
        reason: str = "canary passed",
    ) -> dict[str, Any]:
        with self.plane._conn() as db:
            row = _must(db, "skill_versions", version_id)
            self._refuse_inactive(row, owner_confirmed=owner_confirmed)
            chosen_scope = scope or row["scope"]
            chosen_ref = row["scope_ref"] if scope_ref is None else scope_ref
            if chosen_scope not in SCOPES:
                raise ControlPlaneError("invalid", "activation scope is not recognized")
            db.execute(
                """
                UPDATE skill_activations SET status='superseded'
                WHERE skill_key=? AND scope=? AND scope_ref=? AND status='active'
                """,
                (row["skill_key"], chosen_scope, chosen_ref),
            )
            db.execute(
                "UPDATE skill_versions SET status='active' WHERE id=?",
                (version_id,),
            )
            cur = db.execute(
                """
                INSERT INTO skill_activations(skill_key, version_id, scope, scope_ref, status, reason, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (row["skill_key"], version_id, chosen_scope, chosen_ref, "active", redact(reason)[:300], _now()),
            )
            emit(
                db,
                self.plane,
                task_id=row["learned_from_task_id"],
                job_id=None,
                session_id="",
                event_type="skill_activated",
                message=f"activated {row['skill_key']} v{row['version']} on {chosen_scope}",
                detail={"version_id": version_id, "scope": chosen_scope, "scope_ref": chosen_ref},
            )
            updated = _must(db, "skill_versions", version_id)
        public = _public_skill(updated, include_body=False)
        public["activation_id"] = int(cur.lastrowid)
        public["scope"] = chosen_scope
        public["scope_ref"] = chosen_ref
        return public

    def rollback_skill(self, version_id: int, *, reason: str = "rollback") -> dict[str, Any]:
        with self.plane._conn() as db:
            row = _must(db, "skill_versions", version_id)
            db.execute(
                "UPDATE skill_activations SET status='rolled_back' WHERE version_id=? AND status='active'",
                (version_id,),
            )
            db.execute("UPDATE skill_versions SET status='rolled_back' WHERE id=?", (version_id,))
            previous = db.execute(
                """
                SELECT * FROM skill_versions
                WHERE skill_key=? AND version<? AND canary_status='passed' AND risk!='blocked'
                  AND status NOT IN ('quarantined', 'rolled_back', 'deprecated')
                ORDER BY version DESC LIMIT 1
                """,
                (row["skill_key"], row["version"]),
            ).fetchone()
            restored = None
            if previous is not None:
                restored = int(previous["id"])
            emit(
                db,
                self.plane,
                task_id=row["learned_from_task_id"],
                job_id=None,
                session_id="",
                event_type="skill_rolled_back",
                message=f"rolled back {row['skill_key']} v{row['version']}",
                detail={"version_id": version_id, "restored_version_id": restored, "reason": redact(reason)[:200]},
            )
        if restored is not None:
            self.activate_skill(restored, reason=f"restored after rollback: {reason}", owner_confirmed=True)
        return self.skill(version_id)

    def deprecate_skill(self, version_id: int, *, reason: str) -> dict[str, Any]:
        return self._set_skill_status(version_id, "deprecated", "skill_deprecated", reason)

    def quarantine_skill(self, version_id: int, *, reason: str) -> dict[str, Any]:
        return self._set_skill_status(version_id, "quarantined", "skill_quarantined", reason)

    def skill(self, version_id: int) -> dict[str, Any]:
        with self.plane._conn() as db:
            return _public_skill(_must(db, "skill_versions", version_id), include_body=True)

    def list_skills(self) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            rows = db.execute("SELECT * FROM skill_versions ORDER BY skill_key, version").fetchall()
        return [_public_skill(row, include_body=False) for row in rows]

    def resolve_skills(self, task_id: int) -> dict[str, Any]:
        with self.plane._conn() as db:
            task, job = _task_job(db, task_id)
            scopes = _scopes(task, job)
            activations = db.execute("SELECT * FROM skill_activations WHERE status='active' ORDER BY id").fetchall()
            versions = {int(row["id"]): row for row in db.execute("SELECT * FROM skill_versions").fetchall()}
        active: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        best: dict[str, tuple[int, dict[str, Any]]] = {}
        for activation in activations:
            version = versions.get(int(activation["version_id"]))
            if version is None or version["status"] in {"quarantined", "rolled_back", "deprecated"}:
                skipped.append({"skill_key": activation["skill_key"], "reason": "inactive_version"})
                continue
            if not _scope_matches(activation["scope"], activation["scope_ref"], scopes):
                skipped.append({"skill_key": activation["skill_key"], "reason": "scope_mismatch"})
                continue
            rank = SCOPE_RANK[activation["scope"]]
            current = best.get(activation["skill_key"])
            public = {
                "skill_key": activation["skill_key"],
                "version": int(version["version"]),
                "version_id": int(version["id"]),
                "scope": activation["scope"],
                "why": f"active on {activation['scope']}",
            }
            if current is None or rank >= current[0]:
                if current is not None:
                    skipped.append({"skill_key": activation["skill_key"], "reason": "less_specific_scope"})
                best[activation["skill_key"]] = (rank, public)
        active.extend(item for _rank, item in best.values())
        return {"active": active, "skipped": skipped}

    def remember(
        self,
        *,
        topic: str,
        statement: str,
        authority: str,
        scope: str = "project",
        scope_ref: str = "",
        confidence: str = "medium",
        evidence: list[Any] | None = None,
        source_task_id: int | None = None,
        source_session_id: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if authority not in AUTHORITIES:
            raise ControlPlaneError("invalid", "memory authority is not recognized")
        if scope not in SCOPES:
            raise ControlPlaneError("invalid", "memory scope is not recognized")
        if confidence not in {"low", "medium", "high"}:
            raise ControlPlaneError("invalid", "memory confidence is not recognized")
        cleaned, removed = clean_text(statement, limit=1000)
        if removed and contains_secret(statement):
            cleaned = redact(EXTRA_SECRET_RE.sub("[redacted]", statement))[:1000]
        topic_key = _slug(topic)
        statement_hash = digest(cleaned.strip().lower())
        now = _now()
        with self.plane._conn() as db:
            if source_task_id is not None:
                self.plane._guard(self.plane._task(db, source_task_id), "mutate")
            same = db.execute(
                """
                SELECT * FROM memories
                WHERE topic=? AND scope=? AND scope_ref=? AND statement_hash=? AND status IN ('active', 'disputed')
                ORDER BY id DESC LIMIT 1
                """,
                (topic_key, scope, scope_ref, statement_hash),
            ).fetchone()
            if same is not None:
                found = _public_memory(same)
                found["applied"] = "idempotent"
                found["conflict"] = None
                return found
            existing = db.execute(
                """
                SELECT * FROM memories
                WHERE topic=? AND scope=? AND scope_ref=? AND status='active'
                ORDER BY id
                """,
                (topic_key, scope, scope_ref),
            ).fetchall()
            contradictions = [row for row in existing if row["statement_hash"] != statement_hash]
            status, resolution, applied = _memory_outcome(authority, contradictions)
            if expires_at and expires_at <= now:
                status = "expired"
                applied = "expired"
            version = int(
                db.execute("SELECT COALESCE(MAX(version), 0) + 1 AS n FROM memories WHERE topic=? AND scope=? AND scope_ref=?", (topic_key, scope, scope_ref)).fetchone()["n"]
            )
            memory_key = f"{scope}:{scope_ref}:{topic_key}"
            cur = db.execute(
                """
                INSERT INTO memories(
                  memory_key, version, topic, scope, scope_ref, authority, statement, statement_hash,
                  evidence_json, confidence, status, source_task_id, source_session_id, expires_at, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_key,
                    version,
                    topic_key,
                    scope,
                    scope_ref,
                    authority,
                    cleaned,
                    statement_hash,
                    _json(_redact_list(evidence or [])),
                    confidence,
                    status,
                    source_task_id,
                    source_session_id,
                    expires_at,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)
            conflict = None
            if contradictions and resolution:
                if resolution == "superseded":
                    for row in contradictions:
                        if row["authority"] not in HIGH_AUTHORITY:
                            db.execute(
                                "UPDATE memories SET status='superseded', supersedes_id=?, updated_at=? WHERE id=?",
                                (new_id, now, row["id"]),
                            )
                    applied = "superseded"
                else:
                    for row in contradictions:
                        if resolution == "unresolved" and row["authority"] in HIGH_AUTHORITY:
                            db.execute("UPDATE memories SET status='disputed', updated_at=? WHERE id=?", (now, row["id"]))
                    left_id = int(contradictions[-1]["id"])
                    conflict_cur = db.execute(
                        """
                        INSERT INTO memory_conflicts(topic, scope, scope_ref, left_id, right_id, resolution, reason, created_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (topic_key, scope, scope_ref, left_id, new_id, resolution, resolution, now),
                    )
                    conflict = {"id": int(conflict_cur.lastrowid), "resolution": resolution, "topic": topic_key}
                    emit(
                        db,
                        self.plane,
                        task_id=source_task_id,
                        job_id=None,
                        session_id=source_session_id,
                        event_type="memory_conflict",
                        message=f"memory conflict on {topic_key}: {resolution}",
                        detail=conflict,
                    )
            if applied == "superseded":
                emit(
                    db,
                    self.plane,
                    task_id=source_task_id,
                    job_id=None,
                    session_id=source_session_id,
                    event_type="memory_superseded",
                    message=f"higher-authority memory replaced inference on {topic_key}",
                    detail={"memory_id": new_id, "topic": topic_key},
                )
            emit(
                db,
                self.plane,
                task_id=source_task_id,
                job_id=None,
                session_id=source_session_id,
                event_type="memory_stored",
                message=f"stored {authority} memory {topic_key} as {status}",
                detail={"memory_id": new_id, "applied": applied, "status": status},
            )
            stored = _public_memory(_must(db, "memories", new_id))
        stored["applied"] = applied
        stored["conflict"] = conflict
        return stored

    def list_memories(self) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            rows = db.execute("SELECT * FROM memories ORDER BY id").fetchall()
        return [_public_memory(row) for row in rows]

    def conflicts(self) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            rows = db.execute("SELECT * FROM memory_conflicts ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def record_attempt(
        self,
        task_id: int,
        *,
        kind: str,
        name: str,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"command", "file", "decision", "failure", "question"}:
            raise ControlPlaneError("invalid", "attempt kind is not recognized")
        safe_detail = {}
        for key, value in (detail or {}).items():
            if key.lower() in {"stdout", "stderr", "env", "token", "password", "secret", "authorization"}:
                continue
            if isinstance(value, str):
                safe_detail[key] = redact(value)[:300]
            elif isinstance(value, (int, float, bool)) or value is None:
                safe_detail[key] = value
        with self.plane._conn() as db:
            task = self.plane._task(db, task_id)
            self.plane._guard(task, "mutate")
            cur = db.execute(
                """
                INSERT INTO work_attempts(task_id, session_id, kind, name, status, detail_json, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (task_id, task["session_id"] or "", kind, redact(name)[:200], status, _json(safe_detail), _now()),
            )
        return {"id": int(cur.lastrowid), "kind": kind, "name": redact(name)[:200], "status": status, "detail": safe_detail}

    def build_context(self, task_id: int, *, purpose: str = "resume", budget_tokens: int = 1200, session_id: str = "") -> dict[str, Any]:
        budget = max(32, int(budget_tokens))
        with self.plane._conn() as db:
            task, job = _task_job(db, task_id)
            sections, omitted, stale = self._candidates(db, task, job)
            pack = _fit(sections, omitted, stale, budget)
            session = session_id or task["session_id"] or ""
            cur = db.execute(
                """
                INSERT INTO context_packs(
                  task_id, job_id, session_id, purpose, budget_tokens, used_tokens, pack_hash,
                  sections_json, omitted_json, stale_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    task["job_id"],
                    session,
                    purpose,
                    budget,
                    pack["used_tokens"],
                    digest(pack["sections"]),
                    _json(pack["sections"]),
                    _json(pack["omitted"]),
                    _json(stale),
                    _now(),
                ),
            )
            pack_id = int(cur.lastrowid)
            emit(
                db,
                self.plane,
                task_id=task_id,
                job_id=int(task["job_id"]),
                session_id=session,
                event_type="context_built",
                message=f"context pack {pack_id} used {pack['used_tokens']}/{budget}",
                detail={"pack_id": pack_id, "omitted": len(pack["omitted"])},
            )
        pack["id"] = pack_id
        pack["task_id"] = task_id
        pack["purpose"] = purpose
        pack["budget_tokens"] = budget
        return pack

    def compact_context(self, pack_id: int, *, budget_tokens: int) -> dict[str, Any]:
        with self.plane._conn() as db:
            row = db.execute("SELECT * FROM context_packs WHERE id=?", (pack_id,)).fetchone()
            if row is None:
                raise ControlPlaneError("not_found", f"context pack {pack_id} not found")
            sections = json.loads(row["sections_json"])
            keep = {"objective", "constraints", "current_state", "blockers", "decisions", "evidence", "verification", "next_action"}
            kept = [item for item in sections if item.get("key") in keep]
            omitted = [{"key": item.get("key"), "reason": "compacted", "tokens": item.get("tokens", 0)} for item in sections if item.get("key") not in keep]
            pack = _fit(kept, omitted, json.loads(row["stale_json"]), max(32, int(budget_tokens)))
            cur = db.execute(
                """
                INSERT INTO context_packs(
                  task_id, job_id, session_id, purpose, budget_tokens, used_tokens, pack_hash,
                  sections_json, omitted_json, stale_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["task_id"],
                    row["job_id"],
                    row["session_id"],
                    "compacted",
                    pack["budget_tokens"],
                    pack["used_tokens"],
                    digest(pack["sections"]),
                    _json(pack["sections"]),
                    _json(pack["omitted"]),
                    row["stale_json"],
                    _now(),
                ),
            )
            new_id = int(cur.lastrowid)
            emit(
                db,
                self.plane,
                task_id=row["task_id"],
                job_id=row["job_id"],
                session_id=row["session_id"] or "",
                event_type="context_compacted",
                message=f"compacted pack {pack_id} into {new_id}",
                detail={"from_pack_id": pack_id, "pack_id": new_id},
            )
        pack["id"] = new_id
        pack["task_id"] = row["task_id"]
        return pack

    def prepare_handoff(self, task_id: int, *, reason: str, override_reason: str = "") -> dict[str, Any]:
        if reason not in HANDOFF_REASONS:
            raise ControlPlaneError("invalid", "handoff reason is not recognized")
        with self.plane._conn() as db:
            task, job = _task_job(db, task_id)
            self.plane._guard(task, "mutate", override_reason=override_reason)
            packet = self._packet(db, task, job, reason)
            errors = _packet_errors(packet, digest(packet))
            if errors:
                raise ControlPlaneError("handoff_invalid", ",".join(errors))
            blob = canonical(packet)
            cur = db.execute(
                """
                INSERT INTO handoff_packets(
                  task_id, job_id, from_session_id, reason, status, packet_json, packet_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (task_id, task["job_id"], task["session_id"] or "", reason, "prepared", blob, digest(packet), _now()),
            )
            packet_id = int(cur.lastrowid)
            emit(
                db,
                self.plane,
                task_id=task_id,
                job_id=int(task["job_id"]),
                session_id=task["session_id"] or "",
                event_type="handoff_prepared",
                message=f"prepared handoff {packet_id} for {reason}",
                detail={"packet_id": packet_id, "reason": reason, "launched": False},
            )
            if override_reason.strip():
                self.plane._event(db, task_id, f"protected override used for handoff: {redact(override_reason)}")
        return self.handoff(packet_id)

    def validate_handoff(self, packet_id: int) -> dict[str, Any]:
        with self.plane._conn() as db:
            row = _must(db, "handoff_packets", packet_id)
            packet = json.loads(row["packet_json"])
            errors = _packet_errors(packet, row["packet_hash"])
            status = "validated" if not errors else "rejected"
            db.execute(
                "UPDATE handoff_packets SET status=?, validation_json=? WHERE id=? AND status!='resumed'",
                (status, _json({"ok": not errors, "errors": errors}), packet_id),
            )
            emit(
                db,
                self.plane,
                task_id=int(row["task_id"]),
                job_id=int(row["job_id"]),
                session_id=row["from_session_id"] or "",
                event_type="handoff_validated" if not errors else "handoff_rejected",
                message=f"handoff {packet_id} {status}",
                detail={"errors": errors},
                level="info" if not errors else "error",
            )
        return {"ok": not errors, "errors": errors, "status": status, "packet_id": packet_id}

    def resume_from_handoff(self, packet_id: int, *, new_session_id: str, override_reason: str = "") -> dict[str, Any]:
        if not new_session_id.strip():
            raise ControlPlaneError("session_reused", "resume requires a new session id")
        with self.plane._conn() as db:
            row = _must(db, "handoff_packets", packet_id)
            task = self.plane._task(db, int(row["task_id"]))
            self.plane._guard(task, "mutate", override_reason=override_reason)
            if task["worker_status"] in {"running", "needs_input"}:
                emit(
                    db,
                    self.plane,
                    task_id=int(task["id"]),
                    job_id=int(task["job_id"]),
                    session_id=task["session_id"] or "",
                    event_type="session_resume_refused",
                    message="refusing to replace a running session",
                    detail={"packet_id": packet_id, "killed": False},
                    level="error",
                )
                raise ControlPlaneError("worker_running", "a healthy session is not replaced by a handoff")
            if new_session_id == row["from_session_id"]:
                raise ControlPlaneError("session_reused", "resume must use a new session id")
            packet = json.loads(row["packet_json"])
            errors = _packet_errors(packet, row["packet_hash"])
            if errors:
                db.execute(
                    "UPDATE handoff_packets SET status='rejected', validation_json=? WHERE id=?",
                    (_json({"ok": False, "errors": errors}), packet_id),
                )
                emit(
                    db,
                    self.plane,
                    task_id=int(task["id"]),
                    job_id=int(task["job_id"]),
                    session_id=row["from_session_id"] or "",
                    event_type="handoff_rejected",
                    message=f"rejected handoff {packet_id}",
                    detail={"errors": errors},
                    level="error",
                )
                raise ControlPlaneError("handoff_invalid", ",".join(errors))
            sections = [{"key": key, "priority": 50, "text": canonical(packet[key])[:1200], "tokens": tokens(canonical(packet[key])[:1200])} for key in PACKET_KEYS]
            budget = int((packet.get("context_budget") or {}).get("budget_tokens") or 1200)
            fitted = _fit(sections, [], [], budget)
            cur = db.execute(
                """
                INSERT INTO context_packs(
                  task_id, job_id, session_id, purpose, budget_tokens, used_tokens, pack_hash,
                  sections_json, omitted_json, stale_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task["id"],
                    task["job_id"],
                    new_session_id,
                    "handoff-resume",
                    fitted["budget_tokens"],
                    fitted["used_tokens"],
                    digest(fitted["sections"]),
                    _json(fitted["sections"]),
                    _json(fitted["omitted"]),
                    "[]",
                    _now(),
                ),
            )
            pack_id = int(cur.lastrowid)
            db.execute(
                """
                UPDATE handoff_packets
                SET status='resumed', to_session_id=?, validation_json=?, resumed_at=?
                WHERE id=?
                """,
                (new_session_id, _json({"ok": True, "errors": []}), _now(), packet_id),
            )
            emit(
                db,
                self.plane,
                task_id=int(task["id"]),
                job_id=int(task["job_id"]),
                session_id=new_session_id,
                event_type="session_resumed",
                message=f"handoff {packet_id} bound to new session; worker not launched",
                detail={"packet_id": packet_id, "launched": False, "context_pack_id": pack_id, "from_session_id": row["from_session_id"]},
            )
        fresh = self.plane.task(int(row["task_id"]))
        return {
            "launched": False,
            "killed": False,
            "packet_id": packet_id,
            "context_pack_id": pack_id,
            "session_id": new_session_id,
            "from_session_id": row["from_session_id"],
            "worker_status": fresh["worker_status"],
            "task_session_id": fresh["session_id"],
        }

    def note_pressure(self, task_id: int, *, used_tokens: int, budget_tokens: int) -> dict[str, Any]:
        if budget_tokens <= 0 or (used_tokens / budget_tokens) < PRESSURE_RATIO:
            return {"prepared": False, "resumed": False, "killed": False, "reason": "within_budget"}
        packet = self.prepare_handoff(task_id, reason="context_pressure")
        task = self.plane.task(task_id)
        kept = task["worker_status"] in {"running", "needs_input"}
        with self.plane._conn() as db:
            emit(
                db,
                self.plane,
                task_id=task_id,
                job_id=int(task["job_id"]),
                session_id=task["session_id"] or "",
                event_type="context_pressure",
                message="context pressure prepared a handoff without stopping the session",
                detail={"packet_id": packet["id"], "kept_healthy_session": kept, "killed": False},
            )
        return {
            "prepared": True,
            "resumed": False,
            "killed": False,
            "kept_healthy_session": kept,
            "packet_id": packet["id"],
        }

    def handoff(self, packet_id: int) -> dict[str, Any]:
        with self.plane._conn() as db:
            row = _must(db, "handoff_packets", packet_id)
        return _public_handoff(row)

    def list_handoffs(self, task_id: int) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            rows = db.execute("SELECT * FROM handoff_packets WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [_public_handoff(row) for row in rows]

    def timeline(self, task_id: int) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            typed = db.execute("SELECT * FROM plane_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            legacy = db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        rows = [_public_event(row) for row in typed]
        rows.extend(
            {
                "id": int(row["id"]),
                "event_type": "note",
                "message": redact(row["message"]),
                "created_at": row["created_at"],
                "source": "events",
            }
            for row in legacy
        )
        return rows

    def events(self) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            rows = db.execute("SELECT * FROM plane_events ORDER BY id").fetchall()
        return [_public_event(row) for row in rows]

    def explain_route(
        self,
        task_id: int,
        *,
        chosen: str | None = None,
        summary: str | None = None,
        considered: list[dict[str, Any]] | None = None,
        record: bool = False,
    ) -> list[dict[str, Any]]:
        with self.plane._conn() as db:
            task, _job = _task_job(db, task_id)
            if considered is None:
                decision = db.execute("SELECT * FROM route_decisions WHERE task_id=?", (task_id,)).fetchone()
                if decision is None:
                    chosen, summary, considered = None, "not routed yet", []
                else:
                    chosen = decision["account_id"]
                    summary = decision["summary"]
                    considered = json.loads(decision["detail_json"])
            provider = ""
            if chosen:
                account = db.execute("SELECT provider FROM accounts WHERE id=?", (chosen,)).fetchone()
                provider = account["provider"] if account else ""
            skills = self.resolve_skills(task_id)
            memories = db.execute("SELECT status, authority FROM memories").fetchall()
            conflicts = db.execute("SELECT resolution FROM memory_conflicts").fetchall()
            pack = db.execute(
                "SELECT id, used_tokens, budget_tokens FROM context_packs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            reasons = _route_reasons(
                provider=provider,
                chosen=chosen,
                summary=summary or "",
                considered=considered or [],
                skills=skills,
                memory_count=len(memories),
                conflict_count=sum(1 for row in conflicts if row["resolution"] == "unresolved"),
                pack=pack,
            )
            if record:
                emit(
                    db,
                    self.plane,
                    task_id=task_id,
                    job_id=int(task["job_id"]),
                    session_id=task["session_id"] or "",
                    event_type="route_explained",
                    message=summary or "route explained",
                    detail={"kinds": [item["kind"] for item in reasons]},
                )
        return reasons

    def _refuse_inactive(self, row, *, owner_confirmed: bool) -> None:
        if row["status"] == "quarantined":
            raise ControlPlaneError("quarantined", "quarantined skill cannot be activated")
        if row["status"] in {"rolled_back", "deprecated"}:
            raise ControlPlaneError("inactive", f"{row['status']} skill cannot be activated")
        if row["risk"] == "blocked":
            raise ControlPlaneError("risk_blocked", "blocked skill cannot be activated")
        if row["canary_status"] != "passed":
            raise ControlPlaneError("canary_required", "skill cannot activate before a passing canary")
        if row["secrets_removed"] and not owner_confirmed:
            raise ControlPlaneError("owner_confirmation_required", "redacted skill text needs owner confirmation")
        if row["source"] == "learned":
            if not row["learned_from_task_id"]:
                raise ControlPlaneError("learned_unverified", "learned skill has no source task")
            source = self.plane.task(int(row["learned_from_task_id"]))
            if source["verification_status"] != "passed":
                raise ControlPlaneError("learned_unverified", "learned skill waits for passed verification")

    def _set_skill_status(self, version_id: int, status: str, event_type: str, reason: str) -> dict[str, Any]:
        with self.plane._conn() as db:
            row = _must(db, "skill_versions", version_id)
            db.execute("UPDATE skill_versions SET status=? WHERE id=?", (status, version_id))
            db.execute(
                "UPDATE skill_activations SET status=? WHERE version_id=? AND status='active'",
                (status, version_id),
            )
            emit(
                db,
                self.plane,
                task_id=row["learned_from_task_id"],
                job_id=None,
                session_id="",
                event_type=event_type,
                message=f"{status} {row['skill_key']} v{row['version']}: {redact(reason)[:200]}",
                detail={"version_id": version_id, "reason": redact(reason)[:200]},
            )
        return self.skill(version_id)

    def _candidates(self, db, task, job) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        sections: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        objective = " ".join(
            part
            for part in (job["goal"], task["title"], task["acceptance_criteria"] or task["prompt"])
            if part
        )
        _add(sections, "objective", 100, redact(objective)[:1500])
        constraints = f"policy={task['policy']} protected={task['protected']} owner_only={task['owner_only']} import_hold={task['import_hold']}"
        _add(sections, "constraints", 95, constraints)
        _add(sections, "current_state", 90, f"worker={task['worker_status']} verification={task['verification_status']}")
        blockers = []
        if task["owner_only"]:
            blockers.append(task["owner_blocker"] or "owner_only")
        if task["protected"]:
            blockers.append("protected")
        if task["import_hold"]:
            blockers.append("import_hold")
        conflict_rows = db.execute("SELECT topic, resolution FROM memory_conflicts WHERE resolution='unresolved'").fetchall()
        blockers.extend(f"unresolved memory:{row['topic']}" for row in conflict_rows)
        _add(sections, "blockers", 85, "; ".join(blockers) or "none")
        decisions = []
        relevant = []
        now = _now()
        for row in db.execute("SELECT * FROM memories ORDER BY id").fetchall():
            if row["expires_at"] and row["expires_at"] <= now:
                stale.append({"id": int(row["id"]), "topic": row["topic"]})
                omitted.append({"key": "relevant_memory", "reason": "stale", "id": int(row["id"]), "tokens": tokens(row["statement"])})
                continue
            if row["authority"] == "untrusted_content" or row["status"] == "disputed":
                omitted.append({"key": "relevant_memory", "reason": "low_authority" if row["authority"] == "untrusted_content" else "conflict_unresolved", "id": int(row["id"]), "tokens": tokens(row["statement"])})
                continue
            if row["status"] != "active":
                continue
            line = f"[{row['authority']}] {row['topic']}: {row['statement']}"
            if row["authority"] in HIGH_AUTHORITY:
                decisions.append(line)
            else:
                relevant.append(line)
        _add(sections, "decisions", 80, " | ".join(decisions) or "no verified decisions")
        verification = db.execute(
            "SELECT status, evidence_json FROM verifications WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        evidence = "no verification row"
        verification_text = "not_started"
        if verification is not None:
            evidence = redact(verification["evidence_json"])[:240]
            verification_text = verification["status"]
        _add(sections, "evidence", 75, evidence)
        _add(sections, "verification", 70, verification_text)
        _add(sections, "next_action", 65, _next_action(task))
        skills = self.resolve_skills(int(task["id"]))
        skill_text = ", ".join(f"{item['skill_key']} v{item['version']} ({item['why']})" for item in skills["active"]) or "no active skill"
        _add(sections, "active_skills", 50, skill_text)
        _add(sections, "relevant_memory", 40, " | ".join(relevant) or "none")
        questions = [
            row["name"]
            for row in db.execute("SELECT name FROM work_attempts WHERE task_id=? AND kind='question' AND status='open'", (task["id"],)).fetchall()
        ]
        _add(sections, "open_questions", 30, "; ".join(questions) or "none")
        _add(sections, "risks", 25, "protected task" if task["protected"] else "none")
        if task["worker_output"]:
            omitted.append({"key": "worker_output", "reason": "raw_log_excluded", "tokens": tokens(task["worker_output"])})
        return sections, omitted, stale

    def _packet(self, db, task, job, reason: str) -> dict[str, Any]:
        attempts = db.execute("SELECT * FROM work_attempts WHERE task_id=? ORDER BY id", (task["id"],)).fetchall()
        skills = self.resolve_skills(int(task["id"]))
        decisions = []
        relevant = []
        for row in db.execute("SELECT * FROM memories WHERE status='active'").fetchall():
            if row["authority"] == "untrusted_content":
                continue
            item = {"topic": row["topic"], "authority": row["authority"], "statement": row["statement"]}
            if row["authority"] in HIGH_AUTHORITY:
                decisions.append(item)
            else:
                relevant.append(item)
        verification = db.execute(
            "SELECT status, evidence_json FROM verifications WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        approvals = db.execute("SELECT id, status, consumed FROM approvals WHERE task_id=? ORDER BY id", (task["id"],)).fetchall()
        commands = []
        files = []
        attempted = []
        failed = []
        questions = []
        for row in attempts:
            detail = json.loads(row["detail_json"] or "{}")
            item = {"kind": row["kind"], "name": row["name"], "status": row["status"], "exit_code": detail.get("exit_code")}
            attempted.append(item)
            if row["status"] in {"failed", "error"}:
                failed.append(item)
            if row["kind"] == "command":
                commands.append({"name": row["name"], "status": row["status"], "exit_code": detail.get("exit_code")})
            if row["kind"] == "file":
                files.append(row["name"])
            if row["kind"] == "question" and row["status"] == "open":
                questions.append(row["name"])
        verification_status = verification["status"] if verification else task["verification_status"]
        instructions = [
            "Use this packet. Do not reload raw worker output or full tool logs.",
            f"Start a new session id. Do not reuse {task['session_id'] or '(none)'}.",
        ]
        if verification_status == "passed":
            instructions.append("Do not repeat passed verification.")
        return {
            "objective": {"goal": job["goal"], "title": task["title"], "acceptance": redact(task["acceptance_criteria"] or task["prompt"])[:500]},
            "current_state": {"worker_status": task["worker_status"], "verification_status": task["verification_status"], "session_id": task["session_id"]},
            "decisions": decisions,
            "evidence": [{"status": verification_status, "summary": redact(verification["evidence_json"])[:240] if verification else ""}],
            "blockers": [name for name, flag in (("owner_only", task["owner_only"]), ("protected", task["protected"]), ("import_hold", task["import_hold"])) if flag],
            "attempted": attempted,
            "failed": failed,
            "next_action": _next_action(task),
            "files_changed": files,
            "commands_run": commands,
            "verification": {"status": verification_status},
            "risks": [task["policy"]],
            "open_questions": questions,
            "owner_approvals": [{"id": int(row["id"]), "status": row["status"], "consumed": row["consumed"]} for row in approvals],
            "active_skills": skills["active"],
            "relevant_memory": relevant,
            "context_budget": {"budget_tokens": 1200, "estimator": "chars/4"},
            "resume_instructions": instructions,
            "reason": reason,
        }


def _memory_outcome(authority: str, contradictions: list[Any]) -> tuple[str, str, str]:
    if not contradictions:
        return "active", "", "stored"
    high_existing = any(row["authority"] in HIGH_AUTHORITY for row in contradictions)
    new_high = authority in HIGH_AUTHORITY
    if not new_high:
        resolution = "untrusted_rejected" if authority == "untrusted_content" else "higher_authority_kept"
        applied = "rejected" if authority == "untrusted_content" else "kept_existing"
        return "disputed", resolution, applied
    if high_existing:
        return "disputed", "unresolved", "conflict"
    return "active", "superseded", "superseded"


def _route_reasons(*, provider, chosen, summary, considered, skills, memory_count, conflict_count, pack) -> list[dict[str, Any]]:
    quota_notes = [item for item in considered if item.get("reason") not in {None, "eligible", "fresh"}]
    quota_summary = quota_notes[0]["reason"] if quota_notes else "no fresh quota sample; missing stays unknown, not zero"
    if pack is None:
        context_summary = "no context pack yet"
    else:
        context_summary = f"pack {pack['id']} used {pack['used_tokens']}/{pack['budget_tokens']}"
    return [
        {"kind": "provider", "summary": provider or "unassigned", "account_id": chosen},
        {"kind": "account", "summary": summary, "account_id": chosen},
        {"kind": "quota", "summary": quota_summary, "account_id": quota_notes[0]["account_id"] if quota_notes else chosen},
        {"kind": "skill", "summary": f"{len(skills['active'])} active skill(s)", "skill_keys": [item["skill_key"] for item in skills["active"]], "skipped": skills["skipped"]},
        {"kind": "memory", "summary": f"{memory_count} memory row(s), {conflict_count} unresolved conflict(s)", "conflicts": conflict_count},
        {"kind": "context", "summary": context_summary},
    ]


def _fit(sections: list[dict[str, Any]], omitted: list[dict[str, Any]], stale: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    reserve = min(24, max(8, budget // 5))
    usable = budget - reserve
    included = []
    spent = 0
    pending = sorted(sections, key=lambda item: (-int(item["priority"]), item["key"]))
    extra_omitted = list(omitted)
    for section in pending:
        cost = int(section["tokens"])
        if spent + cost <= usable:
            included.append({**section, "included": True})
            spent += cost
            continue
        remaining = usable - spent
        if remaining >= 8 and int(section["priority"]) >= 70:
            shrunk = section["text"][: remaining * 4]
            shrunk_tokens = min(remaining, tokens(shrunk))
            included.append({**section, "text": shrunk, "tokens": shrunk_tokens, "included": True, "truncated": True})
            spent += shrunk_tokens
            extra_omitted.append({"key": section["key"], "reason": "truncated_over_budget", "tokens": cost - shrunk_tokens})
            continue
        extra_omitted.append({"key": section["key"], "reason": section.get("omit_reason") or "over_budget", "tokens": cost})
    index = "; ".join(f"{item['key']}:{item['reason']}" for item in extra_omitted) or "none"
    index_tokens = min(reserve, tokens(index))
    included.append({"key": "omitted", "priority": 1, "text": index[: index_tokens * 4], "tokens": index_tokens, "included": True})
    spent += index_tokens
    return {
        "sections": included,
        "omitted": extra_omitted,
        "stale": stale,
        "used_tokens": spent,
        "budget_tokens": budget,
    }


def _packet_errors(packet: dict[str, Any], packet_hash: str) -> list[str]:
    errors = []
    for key in PACKET_KEYS:
        if key not in packet:
            errors.append(f"missing:{key}")
    blob = canonical(packet)
    if digest(packet) != packet_hash:
        errors.append("hash_mismatch")
    if contains_secret(blob):
        errors.append("secret_present")
    for command in packet.get("commands_run") or []:
        if isinstance(command, dict) and any(key in command for key in ("stdout", "stderr", "env")):
            errors.append("raw_command_output")
    if "worker_output" in packet:
        errors.append("raw_log_present")
    return errors


def _next_action(task) -> str:
    if task["verification_status"] == "passed":
        return "continue downstream; do not repeat passed verification"
    if task["worker_status"] == "candidate_complete":
        return "run verification before treating the worker claim as done"
    if task["verification_status"] == "revision_required":
        return "fix the failed checks and verify again"
    if task["worker_status"] in {"running", "needs_input"}:
        return "keep the current session; do not kill it to save tokens"
    return "start the next session through the normal Vicoa session flow"


def _add(sections: list[dict[str, Any]], key: str, priority: int, text: str) -> None:
    cleaned, _removed = clean_text(text or "", limit=1500)
    sections.append({"key": key, "priority": priority, "text": cleaned, "tokens": tokens(cleaned)})


def _scopes(task, job) -> dict[str, str]:
    return {
        "session": task["session_id"] or "",
        "account": task["account_id"] or "",
        "repo": task["project"] or job["project"] or "",
        "project": job["vicoa_project_id"] or job["project"] or "",
        "task_type": task["policy"] or "",
        "global": "",
    }


def _scope_matches(scope: str, scope_ref: str, scopes: dict[str, str]) -> bool:
    if scope == "global":
        return True
    if scope not in scopes:
        return False
    return bool(scope_ref) and scope_ref == scopes[scope]


def _task_job(db, task_id: int):
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise ControlPlaneError("not_found", f"task {task_id} not found")
    job = db.execute("SELECT * FROM jobs WHERE id=?", (task["job_id"],)).fetchone()
    if job is None:
        raise ControlPlaneError("not_found", f"job {task['job_id']} not found")
    return task, job


def _must(db, table: str, row_id: int):
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise ControlPlaneError("not_found", f"{table} {row_id} not found")
    return row


def _slug(value: str) -> str:
    cleaned = redact(value or "").strip().lower().replace(" ", "-")
    if not cleaned or contains_secret(cleaned):
        raise ControlPlaneError("invalid", "key is empty or unsafe")
    return cleaned[:80]


def _redact_list(values: list[Any]) -> list[Any]:
    cleaned = []
    for value in values:
        if isinstance(value, str):
            cleaned.append(redact(value)[:300])
        else:
            cleaned.append(value)
    return cleaned


def _public_skill(row, *, include_body: bool) -> dict[str, Any]:
    public = {
        "id": int(row["id"]),
        "skill_key": row["skill_key"],
        "version": int(row["version"]),
        "name": row["name"],
        "description": row["description"],
        "scope": row["scope"],
        "scope_ref": row["scope_ref"],
        "risk": row["risk"],
        "status": row["status"],
        "source": row["source"],
        "canary_status": row["canary_status"],
        "content_hash": row["content_hash"],
        "secrets_removed": int(row["secrets_removed"]),
        "required_secret_names": json.loads(row["required_secret_names_json"]),
        "compatible_agents": json.loads(row["compatible_agents_json"]),
        "required_tools": json.loads(row["required_tools_json"]),
        "created_at": row["created_at"],
    }
    if include_body:
        public["body"] = row["body"]
    return public


def _public_memory(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "topic": row["topic"],
        "scope": row["scope"],
        "scope_ref": row["scope_ref"],
        "authority": row["authority"],
        "statement": row["statement"],
        "confidence": row["confidence"],
        "status": row["status"],
        "version": int(row["version"]),
        "evidence": json.loads(row["evidence_json"]),
    }


def _public_handoff(row) -> dict[str, Any]:
    packet = json.loads(row["packet_json"])
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "job_id": int(row["job_id"]),
        "from_session_id": row["from_session_id"],
        "to_session_id": row["to_session_id"],
        "reason": row["reason"],
        "status": row["status"],
        "packet_hash": row["packet_hash"],
        "packet": packet,
        "validation": json.loads(row["validation_json"] or "{}"),
        "resumed_at": row["resumed_at"],
    }


def _public_event(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "task_id": row["task_id"],
        "job_id": row["job_id"],
        "session_id": row["session_id"],
        "event_type": row["event_type"],
        "level": row["level"],
        "message": row["message"],
        "detail": row["detail_json"],
        "created_at": row["created_at"],
        "source": "plane_events",
    }
