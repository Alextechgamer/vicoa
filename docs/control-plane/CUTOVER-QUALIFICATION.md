# Cutover qualification

Checked on 2026-09-23 after both isolated homes were signed in. PASS means a command produced the result. A class existing in source is not PASS.

Starting commit: `97cac2f`.

| Gate | Result | Evidence |
|---|---|---|
| Postgres | PASS | Revision `a8c1e4b72d09` upgraded, downgraded, and upgraded again on local `vicoa_control_plane`. |
| migration | PASS | Read-only snapshot import. Live database path is refused. |
| persistence | PASS | A second connection read the row written by the first. |
| LocalWorker | PASS | Earlier disposable worktree canary. Not used as a substitute for the rows below. |
| AntigravitySession.start | PASS | Real `start()` on `/home/alex/.local/bin/agy`. Example conversation `711e83a9-2e7d-43e6-aa76-d91ee407a650`. |
| vicoa-agy-1 | PASS | `agy1-canary.txt` is exactly `VICOA-AGY-1-OK`. Token file exists. No secret printed. |
| vicoa-agy-2 | PASS | `sim-2/agy2-canary.txt` is exactly `VICOA-AGY-2-OK`. Token hash differs from agy-1. |
| simultaneous same-provider accounts | PASS | Conversations `808e9fc4-184d-4dc6-b014-02ffe4698942` and `64957a9b-822c-4999-89c5-efcd8412b6a2`. PIDs `1317108` and `1317156`. |
| credential isolation | PASS | Separate token files. Hashes differ. Contents not printed. |
| session isolation | PASS | Distinct conversation ids and process ids. |
| worktree isolation | PASS | Markers landed in the disposable repo passed as `cwd`, not in `/opt/projects` or `/home/agentctl`. |
| real A->B->C DAG | PASS | B blocked before A. After A finished, B stayed locked until verification passed. Same for C. Files contain `A-VERIFIED`, `B-VERIFIED`, `C-VERIFIED`. |
| failed verification | PASS | Wrong marker stayed `revision_required`. Dependent task returned `dependency`. |
| message queue | PASS | Three messages stayed `queued` until the worker was accepting. |
| message acknowledgement | PASS | One became `acknowledged` only after `AntigravitySession.deliver_user_message` returned. The other two stayed `queued`. |
| approvals | NOT EXERCISED | Default-mode `AntigravitySession` wrote the file instead of asking. The state machine, HTTP approve-once, deny, and no standing rule are tested. Headless `agy` has no prompt channel. Not cutover-critical. |
| restart recovery | PASS | `recover_after_restart` marked the task `interrupted`, kept the same pid, and did not start a second session. |
| quota routing | NOT AVAILABLE | No supported fresh percentage was observed. Missing telemetry stays `unknown` and is not stored as 0. Pool routing refuses every account instead of guessing. Not cutover-critical. |
| account failure isolation | PASS | MCP drain of `agy-a` routed the next disposable task to `agy-b`. |
| MCP | PASS | Real router: status, quota, job, claim, heartbeat, DAG, profiles, blockers, approve once, deny, drain, enable. Shell, SQL, and process kill returned 403. No standing allow rule. |
| portfolio UI desktop | PASS | Chrome at 1440px rendered Profiles, Jobs, Tasks, Approvals, messages, and Blockers through `AUTH_PROVIDER=builtin`. Both profiles showed `quota unknown`. No secrets. |
| portfolio UI mobile | PASS | Chrome at 390px rendered the same page. Text wrapped. No horizontal page overflow. |
| protected tasks | PASS | Fixture refusal in the control-plane suite. Live task 6 was not used. |
| imported jobs | PASS | Snapshot import remains held. Jobs 70 and 71 were not released. |
| task-6 preservation | PASS | Still `paused` on `agy-2`, verification `pending`. |
| jobs 70/71 preservation | PASS | Both still `planned`. Queued prompts were not delivered. |
| production boundaries | PASS | No live service was stopped. No production Supabase or paid render was used. |
| tests | PASS | 28 passed, 0 failed, 0 skipped. |
| Ruff | PASS | `ruff check` on the control-plane Python is clean. |
| TypeScript | PASS | `pnpm exec tsc --noEmit` in `apps/web` exited 0. |
| secret scan | PASS | gitleaks 8.28.0 found no leaks in the control-plane tree. |

## Eligible for cutover

Critical gates are proven. New-work authority was transferred at `2026-09-23T21:28:36Z`. See `CUTOVER-STATUS.md`. The four legacy services were not stopped.

Non-critical gaps:

- A live Antigravity approval prompt was not emitted. The tested fail-closed approval path is enough for cutover.
- No fresh numeric quota percentage is available from a supported source. Unknown stays unknown, and routing refuses rather than guessing.
- `docker.service` is not installed here, so the Compose stack was not started. The page used the documented built-in provider instead of production Supabase.

## Prepared, not executed

1. Copy `/opt/agent-control/state/agent-control.db` to a timestamped snapshot.
2. Read task 6 and confirm it is still paused.
3. Read jobs 70 and 71 and confirm they are still held.
4. Stop new dispatch on the old orchestrator only after the owner says so.
5. Confirm no second worker is attached to task 6, 113, or 114.
6. Start Vicoa with `VICOA_CONTROL_PLANE_TOKEN` set and a new database, not the live Agent Control file.
7. Import the snapshot. Leave `shadow=1`.
8. Do not release task 6 or jobs 70 and 71.
9. Confirm `vicoa-agy-1` and `vicoa-agy-2` still have separate token files.
10. Call MCP `status` and confirm shell is rejected.
11. Open `/dashboard/portfolio` with `NEXT_PUBLIC_AUTH_PROVIDER=builtin`. Do not use production Supabase.
12. Watch one harmless disposable job before any live job moves.
13. Retirement order, later: orchestrator, then reaper, then API, then MCP. Not in this run.
14. Roll back if a live task changes state, a second worker appears, or an imported job starts.
15. Rollback is: stop the new Vicoa process, leave the four old services running, and do not delete the snapshot.

Do not run that list until the owner says so.
