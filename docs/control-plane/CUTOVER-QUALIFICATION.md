# Cutover qualification

Checked on 2026-09-23 against `feat/unified-control-plane`. PASS means a test or live read produced the result. Implementation alone is not PASS.

| Gate | Result | Evidence |
| --- | --- | --- |
| Postgres persistence | PASS | `test_postgres_migration_roundtrip` wrote a task and queued message, read them from a second connection, then dropped the tables |
| Migration revision | PASS | Revision `a8c1e4b72d09` upgraded, downgraded, and upgraded again on local database `vicoa_control_plane` |
| Multi-profile isolation | PASS | Disposable `agy-1` and `agy-2` homes in `test_same_provider_profiles_stay_isolated_and_live_homes_are_refused`. Runtime paths are hashed, not returned |
| Real worker launch | PASS | `LocalWorker` created a git worktree and ran `python3` to write `EXACT-MARKER`. This is not an Antigravity session |
| Worktree isolation | PASS | Canary tasks used `git worktree add` under a temp root, not `/opt/projects` |
| Message delivery | PASS | Queued prompts stayed queued until `worker_accepting=True`, then one moved queued to sent to acknowledged |
| Approvals | PASS | Approve-once left `allow_rules` empty. A different prompt was not accepted as the same approval |
| DAG gating | PASS | B and C refused to start before upstream verification. A wrong file stayed `revision_required` and did not start B |
| Verification | PASS | File, git, command, timeout, and worker-output checks in the control-plane suite |
| Restart recovery | PASS | A running disposable task became `interrupted`. Capacity returned to zero. The queued message remained. No second worker started |
| Quota routing | PASS | Conserve-constrained profile was avoided. Stale telemetry was not stored as zero |
| MCP | PASS | `drain_profile` and `enable_profile` ran. `shell` was rejected. Claim/heartbeat still return no started tasks |
| Portfolio UI | NOT TESTED | `/dashboard/portfolio` renders profiles, jobs, tasks, approvals, messages, and blockers. No browser session was driven |
| Protected tasks | PASS | Fixture task refuses resume, message, route, kill, reap, approve, and mutate without an override |
| Imported jobs | PASS | Snapshot import remains held and shadow. The importer refuses the live database path |
| Two-account Antigravity | BLOCKED | Live `agy-1` holds task 113. Live `agy-2` holds paused task 6 and task 114. Starting a new session there would spend quota and can touch that work |
| Production boundary | PASS | Tillpress, WordPress, credentials, and customer contact were not used. Live services stayed active |

## Not a cutover

Old Agent Control is not eligible for retirement.

The missing gate is a real two-account Antigravity session that does not reuse task 6, task 113, or task 114. Vicoa's launcher is `AntigravitySession.start` in `backend/src/integrations/headless/antigravity/session.py`. Calling it against the existing runtime homes is the risk, so this run used `LocalWorker` instead.

## Reversible cutover, not executed

1. Keep task 6 paused and jobs 70 and 71 in shadow.
2. Authenticate new Antigravity homes that are not `/home/agentctl/runtimes/agy-1` or `agy-2`.
3. Run the disposable file canary through `AntigravitySession.start` on those new homes.
4. Only after that passes, stop accepting new Agent Control jobs.
5. Leave the old database in place.
6. Do not release imported holds until the owner says so.

Stopping the four live services is an owner action. This run did not do it.
