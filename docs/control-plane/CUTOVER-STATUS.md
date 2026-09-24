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

## After the authority switch

Checked again at `2026-09-23T22:33:27Z`. No new Agent Control task was created after the switch. Two existing queued tasks logged `No route` because both accounts are draining. Task 114 was reconciled from `working` back to `needs_input` on its existing session. That was not a new assignment.

A new Vicoa job, 104, wrote `POST-CUTOVER-OK` through `vicoa-agy-1` and verified. Agent Control did not receive that job.

Imported Vicoa account records named `agy-1` and `agy-2` pointed at the live runtime homes. They are now drained in the Vicoa database so new routing cannot select them. A route check selected `vicoa-agy-1`.

## Recovered legacy evidence

Task 113 has no `BATCHIDEO-BASELINE-READINESS.md`. The paid baseline render remains an owner blocker. Vicoa job 106 records that successor and is not started.

Task 114 has `agent-control-evidence/TILLPRESS-MONOREPO-CURRENT-STATE.md`. The OrderRing activation path now verifies the server payload before recording `was_active`. StoreCanvas branch `fix/storecanvas-license-verify-order` (`e0e24c3`) and OrderBay branch `fix/orderbay-license-verify-order` (`d4a0a69`) have the same ordering fix. Neither is merged or deployed. The shared template and Checkout Sentinel still have the old order; those successor jobs are held.

## Live session map

Read-only:

- `agy-1` tmux: `task-113`, needs-input. Process cwd is that worktree.
- `agy-2` tmux: `task-6` paused, and `task-114` needs-input.
- The running `agy` processes are the existing 113 and 114 sessions, inside the orchestrator cgroup.
- Task 6 has a paused Atrium session and no separate new process was started.

113 and 114 can be abandoned later only after confirming task 6 does not share their process. That was not tested by stopping anything.

