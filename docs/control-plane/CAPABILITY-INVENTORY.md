# Capability inventory

Native means the behavior lives in `backend/src/shared/control_plane` and is
covered by the tests in that package. It is not a proxy to `/opt/agent-control`.

| Capability | Where it lives now | Notes |
| --- | --- | --- |
| Isolated provider accounts | `ControlPlane.create_account` | Homes are stored, not returned by `status()`. Credentials are rejected. |
| Quota observations | `observe_quota` | Missing percentages stay null. They are not filled with 0. |
| Routing | `route` | A constrained or failed account is skipped. The other account stays enabled. |
| Jobs and DAG | `create_job`, `add_task`, `add_dependency` | A dependency unlocks only when upstream `verification_status` is `passed`. |
| Worker vs verification | `complete_worker`, `verify_task` | Worker completion sets `candidate_complete` and leaves verification pending. |
| Checks | file exists, file contains, git changed, command, timeout, worker output | Command checks use an argv list, no shell, and a timeout. |
| One-time approval | `approve_once` | Consumed after one matching fingerprint. A mismatch is stale. |
| Permanent allow | `confirm_permanent=True` | Fingerprint-scoped rule only. Not created by accident. |
| Protected task | `protect` | Blocks resume, message, route, kill, reap, approve, and mutate unless an override reason is recorded. |
| Owner-only blocker | `owner_only` | `automation_tick` stops. It does not clear the blocker. |
| Import hold | `import_agent_control` | Read-only source. Live DB path is refused. Imported work cannot start until released. |
| Steering health | `message`, `health` | Queued prompts stay visible until consumed. |
| Restart | `recover_after_restart` | Running tasks become interrupted. Verification rows stay. |
| Session cleanup | `cleanup_session` | Refuses running and needs-input work. Writes evidence to a directory and keeps verification rows. |
| MCP surface | `mcp_surface.dispatch` | Status, projects, jobs, evidence, verification, approvals, quota, goals, DAG, message, blockers, retry, skills, memories, context, handoff, timeline, routing. Shell, SQL, and raw filesystem tools are rejected. |
| Skills | `knowledge.register_skill` | Versioned and scoped. Activation requires a passing canary. Blocked and quarantined skills cannot activate. |
| Memory | `knowledge.remember` | Owner and verified evidence outrank inference. Untrusted content cannot override them. Equal high-authority conflicts stay unresolved. |
| Context packs | `knowledge.build_context` | A pack stays inside its token budget and records why each section was omitted. Raw worker output is excluded. |
| Handoff | `knowledge.prepare_handoff` | Crash, failed verification, drain, and provider failure prepare a packet. Resume uses a new session and does not launch a worker. |
| HTTP API | `backend/api/control_plane.py` | Bearer token, fail closed if unset. |
| Portfolio page | `/dashboard/portfolio` | Reads the same status API. Shows an error if the token is not configured. |
| Postgres schema | Alembic `a8c1e4b72d09`, `b4e7c2a91d18` | Present. The knowledge revision was proven locally with upgrade and downgrade. It is not a production cutover. |

## Left in Agent Control until a later cutover

These still run in `/opt/agent-control` and were not reimplemented as a live worker supervisor:

- Atrium/tmux session hosting
- the orchestrator daemon's dispatch loop
- the reaper's live retirement gate
- the existing MCP bridge on port 8082

Vicoa already has sessions, worktrees, and automations. This change does not delete them. The control plane is the verification and dependency layer those sessions were missing.

## Explicitly not copied

- Atrium as a required dependency of the new tests
- Agent Control's SQLite file as the long-term database
- any standing "allow all" rule
