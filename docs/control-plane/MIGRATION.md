# Vicoa control plane migration

This fork adds Agent Control's orchestration behavior as native Vicoa code.
It does not wrap the live Agent Control process, and it does not replace it yet.

## What ran

- Fork: `https://github.com/Alextechgamer/vicoa`
- Branch: `feat/unified-control-plane`
- Upstream remote: `vicoa-ai/vicoa`
- Control-plane tests: 18 passed, including a real local Postgres upgrade, downgrade, and re-upgrade of revision `a8c1e4b72d09`
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

- The full Vicoa application migration chain was not applied. Revision `a8c1e4b72d09` was applied, downgraded, and re-applied on local database `vicoa_control_plane`. A row written through the store was readable from a second connection. The test then dropped those tables.
- Old Agent Control is not eligible for retirement. The live worker loop is still Atrium. No new Antigravity session was started.
- The control-plane HTTP API is fail-closed until `VICOA_CONTROL_PLANE_TOKEN` is set.
- Permanent allow rules are fingerprint-scoped, require `confirm_permanent`, and can be revoked. A normal approve does not create one.
- Imported jobs stay in shadow mode. Releasing an import hold does not dispatch them while `jobs.shadow=1`.

## Owner-only

Do not:

- resume, message, route, kill, reap, or approve task 6
- release the import hold on jobs 70 or 71
- deliver the queued prompts already sitting on those sessions
- stop the four live Agent Control services
- point this API at the live Agent Control database
