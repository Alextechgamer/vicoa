# Cutover qualification

Checked on 2026-09-23 against `feat/unified-control-plane`. PASS means a command or browser produced the result. Source existing is not PASS.

| Gate | Result | Evidence |
|---|---|---|
| Postgres persistence | PASS | Revision `a8c1e4b72d09` upgraded, downgraded, and upgraded again on local database `vicoa_control_plane`. A second connection read the written task and queued message. |
| Migration import | PASS | Read-only snapshot import test. Live path is refused. Imported jobs stay shadowed. |
| LocalWorker | PASS | `test_local_worker.py` created a real git worktree and ran a local process. |
| Real AntigravitySession | BLOCKED | `agy` has no profile flag. New homes have no `.gemini` state. Signing in would be an owner action. `AntigravitySession.start()` was not called. |
| agy account #1 | BLOCKED | `/home/alex/.vicoa/runtimes/vicoa-agy-1` exists and is empty. Not signed in. |
| agy account #2 | BLOCKED | `/home/alex/.vicoa/runtimes/vicoa-agy-2` exists and is empty. Not signed in. |
| Simultaneous same-provider profiles | BLOCKED | No authenticated pair to launch. Routing around a drained disposable profile passed in `test_profiles.py`. |
| Worktree isolation | PASS | Local canary worktree was not the live project checkout. |
| DAG | PASS | Local A did not unlock B until verification passed. Not proven on Antigravity. |
| Deterministic verification | PASS | File and marker checks passed in the local canary. |
| Failed verification | PASS | Wrong marker stayed `revision_required` and did not start the next task. |
| Message queue | PASS | HTTP and store tests left a prompt `queued` until the worker was accepting. |
| Acknowledgement | PASS | Local canary moved one message to `acknowledged` only after an explicit ack. |
| Approvals | PASS | Approve-once did not create a standing allow rule unless `confirm_permanent` was set. |
| Restart recovery | PASS | Running local worker became `interrupted`. No second worker started. |
| Quota routing | PASS | Missing percentage stayed null. Conserve-constrained account was not selected when a healthier one existed. |
| Account failure isolation | PASS | Draining `vicoa-agy-1` left `vicoa-agy-2` routable. |
| MCP | PASS | FastAPI TestClient hit the real router: status, create, claim, heartbeat-safe claim, shell rejected, protected message rejected. |
| Portfolio UI desktop | BLOCKED | Chrome opened `http://127.0.0.1:3010/dashboard/portfolio`. The existing root auth gate crashed first: Supabase URL and key are not configured. The portfolio cards did not render. |
| Portfolio UI mobile | BLOCKED | Same Supabase gate at 390x844. Overflow of that error overlay is not a portfolio layout result. |
| Protected tasks | PASS | Fixture refused message, route, kill, reap, and approve. Live task 6 was not used. |
| Imported jobs | PASS | Import hold and shadow mode kept imported tasks from starting. |
| Credential isolation | PASS | Status JSON did not include the runtime path. Provisioning copied zero credential files. |
| Production boundary | PASS | Live task 6 still paused. Jobs 70 and 71 still planned. Four live services still active. |
| Test suite | PASS | 27 passed, 0 failed, 0 skipped. |
| Lint | PASS | Ruff issues introduced in this package were fixed. `row.keys()` remains because `sqlite3.Row` iteration yields values, not keys. Prettier is not a dependency of `apps/web`; its lint script is `tsc`. |
| Secret scan | PASS | gitleaks 8.28.0 scanned the control-plane sources and docs. No leaks found. |

## Retirement

NOT ELIGIBLE — OWNER AUTHENTICATION REQUIRED

The owner action is in `docs/control-plane/OWNER-AUTH.md`. Do not stop the old services before both new homes are signed in and a disposable `AntigravitySession` canary passes.

## Reversible cutover, not executed

1. Snapshot `/opt/agent-control/state/agent-control.db` again.
2. Confirm task 6 is still paused on `agy-2`.
3. Stop new dispatch on the old orchestrator only after the owner says so. Do not stop it in this run.
4. Leave jobs 70 and 71 held.
5. Import only a fresh read-only snapshot into the Vicoa control plane. Keep `shadow=1`.
6. Start Vicoa worker ownership only for a new disposable task, not task 6 or jobs 70/71.
7. Confirm `vicoa-agy-1` and `vicoa-agy-2` have separate `HOME/.gemini` trees.
8. Call the control-plane MCP status and claim tools. Confirm claim does not start workers.
9. Open `/dashboard/portfolio` with the real Supabase session and confirm the cards.
10. Confirm no second worker was started for an imported session.
11. Watch one disposable cycle.
12. Only later stop `agent-control`, `agent-orchestrator`, `agent-reaper`, and `agent-mcp`.

Rollback: leave the old services running, drop the Vicoa control-plane tables from the migration downgrade, and delete only `/home/alex/.vicoa/runtimes/vicoa-agy-*` if those sign-ins should be discarded. Do not delete `/home/agentctl/runtimes`.
