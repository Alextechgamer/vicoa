# Skills, memory, context, and handoff

Vicoa owns this layer. It is not a copy of Hermes skill files, memory files, or session transcripts, and it is not a second scheduler. Jobs, tasks, sessions, verification, and routing stay on the control plane in `backend/src/shared/control_plane`.

## What shipped

- Versioned skills with scope, risk, tools, secret names, and a canary gate.
- Durable memory with authority, evidence, supersession, and visible conflicts.
- Context packs that stay inside a token budget and record every omission.
- Handoff packets that resume on a new session without launching a worker.
- Typed events on the same task timeline.
- Read-only HTTP and MCP views. Shell, SQL, and raw filesystem tools stay rejected.

## Use

```python
from shared.control_plane.store import ControlPlane

plane = ControlPlane(path)
skill = plane.knowledge.register_skill(
    skill_key="check-marker",
    name="Check marker",
    description="Verify the local marker",
    body="Check the marker file.",
    scope="global",
    risk="low",
)
plane.knowledge.record_canary(skill["id"], passed=True, evidence={"summary": "marker existed"})
plane.knowledge.activate_skill(skill["id"])
plane.knowledge.remember(
    topic="deploy",
    statement="ship from main only",
    authority="owner",
    scope="project",
    scope_ref="canary-project",
)
pack = plane.knowledge.build_context(task_id, budget_tokens=1200)
packet = plane.knowledge.prepare_handoff(task_id, reason="manual")
plane.knowledge.resume_from_handoff(packet["id"], new_session_id="session-2")
```

`resume_from_handoff` is the low-level bookkeeping primitive and does not launch a worker. The production adapter is `AntigravityTaskWorker`: once a task is started through that adapter, it measures context use, prepares the handoff, closes Session A, launches Session B, binds the canonical handoff, restores the compact Context Pack, and continues the same task/worktree. The old session id is refused after rollover.

```python
from shared.control_plane.antigravity_worker import AntigravityTaskWorker

runner = AntigravityTaskWorker(
    plane,
    task_id,
    cwd=worktree,
    rollover_budget_tokens=120_000,
)
await runner.start()
await runner.deliver(task_prompt)
```

This adapter is not a scheduler. The caller still owns task dispatch; Vicoa owns routing, context, session lineage, messages, and verification.

Read views:

- `GET /api/v1/control-plane/skills`
- `GET /api/v1/control-plane/memories`
- `GET /api/v1/control-plane/tasks/{id}/context`
- `GET /api/v1/control-plane/tasks/{id}/handoff`
- `GET /api/v1/control-plane/tasks/{id}/timeline`
- `GET /api/v1/control-plane/tasks/{id}/routing`

MCP tools with the same names are read-only. `shell`, `sql`, and raw filesystem tools are still rejected.

## Automatic handoff

These paths prepare a packet and do not kill a healthy session:

| Trigger | Reason |
| --- | --- |
| Context use at or above 85% of budget | `context_pressure` via `note_pressure` |
| `crash_worker` or restart recovery | `crash` |
| Verification `revision_required` | `verification_failure` |
| Account drain while a task is running or needs input | `account_switch` |
| Provider failure while a task is running or needs input | `provider_switch` |
| Owner pause or a scheduled checkpoint | caller passes `owner_pause` or `scheduled` |

A protected task still requires an override reason. If the automatic path cannot prepare a packet, the original crash, drain, or verification result still stands.

## Failure behavior

| Code | What happens |
| --- | --- |
| `canary_required` | Activation stops. The skill stays a candidate. |
| `risk_blocked` | A blocked skill cannot be activated or given a passing canary. |
| `quarantined` | Activation stops until a new version exists. |
| `owner_confirmation_required` | A redacted skill body needs an explicit owner confirmation. |
| `learned_unverified` | A learned skill waits until the source task verification passes. |
| `secret_rejected` | A secret value in a secret-name field is refused. |
| `handoff_invalid` | Resume stops. The packet is marked rejected. |
| `session_reused` | The old session cannot start again after a resumed handoff. |
| `worker_running` | Resume stops. The running session is left alone. |
| `protected` | Handoff and memory writes stop unless an override reason is recorded. |

Conflicts between two owner or verified memories stay `unresolved`. Untrusted content cannot replace either. Agent inference loses to owner or verified evidence, and that replacement is an event, not a silent edit.

Context omissions use `over_budget`, `truncated_over_budget`, `stale`, `low_authority`, `conflict_unresolved`, `duplicate`, `raw_log_excluded`, `compacted`, or `skill_body_deferred`. Raw worker output is never copied into a pack or a packet.

## What was not copied

- Hermes `SKILL.md` files and the skill directory layout.
- Hermes user or agent memory files.
- Hermes session transcripts.
- A standing allow-all rule.
- A second job scheduler or worker supervisor.
- The full Command Center UI. The read views above are the current surface.

Migration `b4e7c2a91d18` creates and drops these tables. The store also creates them on open so a disposable SQLite file works without Alembic. Canary evidence is in `CANARY-SKILLS-MEMORY.md`. The live import record is `LIVE-SKILLS-HANDOFF.md`; the automatic two-session proof and exact context metrics are in `AUTOMATIC-ROLLOVER-CANARY.md`.
