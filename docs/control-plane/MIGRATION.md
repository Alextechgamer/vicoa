# Vicoa control plane migration

This fork adds Agent Control's orchestration behavior as native Vicoa code.
It does not wrap the live Agent Control process, and it does not replace it yet.

## What ran

- Fork: `https://github.com/Alextechgamer/vicoa`
- Branch: `feat/unified-control-plane`
- Upstream remote: `vicoa-ai/vicoa`
- Control-plane tests: 11 passed (`backend/src/shared/control_plane/tests/test_control_plane.py`)
- Harmless canary: local files only, two profile records, no paid API
- Snapshot import: 71 jobs, 114 tasks, 2 accounts, 0 tasks started

## Live Agent Control

Left running. Not written.

| Check | Result |
| --- | --- |
| `agent-control.service` | active |
| `agent-orchestrator.service` | active |
| `agent-reaper.service` | active |
| `agent-mcp.service` | active |
| task 6 | still `paused` on `agy-2`, verification `pending` |

A read-only snapshot was taken before import. The importer refuses the live path `/opt/agent-control/state/agent-control.db`.

## Represented, not restarted

Imported copy only. Workers were not messaged.

| Source | Title | Worker | Verification | Hold |
| --- | --- | --- | --- | --- |
| task 6 | para-B | paused, protected | pending | held, 2 queued prompts recorded |
| task 113 / job 70 | Batchideo baseline readiness audit | needs_input | pending | held, 4 queued prompts recorded |
| task 114 / job 71 | Tillpress monorepo current-state audit | needs_input | pending | held, 2 queued prompts recorded |

No `BATCHIDEO-BASELINE-READINESS.md` or `TILLPRESS-MONOREPO-CURRENT-STATE.md` was produced by this migration. Those files are not claimed to exist. No paid render was run.

## Profiles

`agy-1` and `agy-2` have separate runtime homes. Read-only `atrium ls` succeeded on each home in the same window:

- agy-1: `task-113` needs-input
- agy-2: `task-6` paused, `task-114` needs-input

No new Antigravity session was started. That would spend quota and could touch live work. The automated test proves the router can run two accounts at once on disposable tasks.

## Not done, on purpose

- Vicoa Postgres is not running here. Alembic revision `a8c1e4b72d09` is in the repo and was not applied to a server.
- Old Agent Control is not eligible for retirement. Parity of the live worker loop is not proven.
- The control-plane HTTP API is fail-closed until `VICOA_CONTROL_PLANE_TOKEN` is set.
- Permanent allow rules are fingerprint-scoped and require `confirm_permanent`. A normal approve does not create one.

## Owner-only

Do not:

- resume, message, route, kill, reap, or approve task 6
- release the import hold on jobs 70 or 71
- deliver the queued prompts already sitting on those sessions
- stop the four live Agent Control services
- point this API at the live Agent Control database
