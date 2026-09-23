# Cutover status

Authority for new work changed at `2026-09-23T21:28:36Z`.

| Boundary | Owner |
|---|---|
| New jobs and tasks | Vicoa |
| Live legacy sessions | Agent Control, still running |
| Task 6 | Paused, protected, not executed |
| Jobs 70 and 71, tasks 113 and 114 | Held shadow copies. Live sessions not restarted |

## What changed

Agent Control accounts `agy-1` and `agy-2` were set to `draining` through `POST /api/accounts/{id}/drain`. That is the supported “no new work will be assigned” control. The scheduler only assigns accounts whose status is `available`. Existing sessions were not paused or killed.

No Agent Control service was stopped or restarted.

Vicoa database: `/home/alex/.vicoa/control-plane.db`
Snapshot: `/home/alex/.vicoa/cutover/20260923T212831Z/agent-control.db`
Qualified code: `68db39592d98a782e28526d01db4efddccdc9d9c`

The import copied 71 jobs and 114 tasks as shadow holds. Historical queued-prompt counts are 2, 4, and 2 on source tasks 6, 113, and 114. The deliverable message table has 0 rows for that import.

## Smoke

A new job was created through `POST /api/v1/control-plane/mcp` `submit_dag`, not through Agent Control.

- A on `vicoa-agy-1` wrote `CUTOVER-A-OK` and stayed locked for B until verification passed.
- B on `vicoa-agy-2` wrote `CUTOVER-B-OK` and verified.
- MCP status, quota, jobs, evidence, approvals, claim, and heartbeat returned 200. Shell, SQL, and process kill returned 403.
- Desktop and 390px Chrome rendered `/dashboard/portfolio` through built-in auth. Both Vicoa profiles showed `quota unknown`. Shadow rows are marked held and are not started from the page.

## Legacy services

All four remain active. All four stay caretakers.

| Service | Class | Why |
|---|---|---|
| agent-orchestrator | LEGACY CARETAKER — STILL REQUIRED | Its cgroup contains the live `agy` processes. Stopping it was not tested and could terminate those children. |
| agent-reaper | LEGACY CARETAKER — STILL REQUIRED | Not proven safe relative to task 6. It was not stopped. |
| agent-control | LEGACY CARETAKER — STILL REQUIRED | Still the legacy state API. New assignment is blocked by the drain. |
| agent-mcp | LEGACY CARETAKER — STILL REQUIRED | Still attached to the legacy API. New assignment is blocked by the drain. |

## Rollback

Do not kill task 6. To restore old new-work assignment, call the existing enable endpoint for `agy-1` and `agy-2`. Leave the four services running. Do not delete the snapshot. Vicoa’s imported rows stay shadow and held even if old assignment is restored.
