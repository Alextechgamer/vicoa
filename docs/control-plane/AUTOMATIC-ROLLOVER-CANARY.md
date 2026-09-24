# Automatic Antigravity rollover canary

Run: 2026-09-23 21:09 PDT against the authoritative SQLite control plane at `/home/alex/.vicoa/control-plane.db`.

## Implementation

`AntigravityTaskWorker` in `backend/src/shared/control_plane/antigravity_worker.py` is a Vicoa-owned adapter around the existing Antigravity session integration. It is not a scheduler. Vicoa remains authoritative for routing, tasks, Context Packs, handoffs, messages, lineage, and verification.

The adapter:

- launches only eligible Vicoa Antigravity profiles;
- rejects legacy `agy-1` and `agy-2` for new work;
- isolates each profile through its own `HOME` without mutating process-global environment;
- measures provider-reported context use, with the conservative local estimate as a floor;
- automatically prepares a structured handoff when the configured threshold is reached;
- closes Session A, releases its worker slot, and launches exactly one Session B;
- resumes the same Vicoa task and worktree with a compact Context Pack;
- delivers queued messages in order and acknowledges them only after successful delivery;
- runs deterministic verification before recording completion.

## Live proof

The disposable live task was Vicoa task `131`, job `118`, in `/tmp/vicoa-auto-rollover-20260924T040720Z`.

- Session A: `2c8b7de5-245f-4d68-a659-0f6281df1ab2` on `vicoa-agy-1`.
- Session B: `17d33e0e-c6a7-4141-91b7-311a68f945b6` on `vicoa-agy-2`.
- Canonical handoff: `7`, reason `context_pressure`, status `resumed`.
- Session B's parent is Session A and its `handoff_id` is `7`.
- The task id and working repository stayed unchanged.

Session A created `phase-a.txt` containing exactly `PHASE-A-OK`. Session B inspected but did not rewrite it, then created `phase-b.txt` containing exactly `PHASE-B-OK`. The SHA-256 of Phase A was identical before and after Session B. Both deterministic `file_contains` checks passed.

The first live attempt, task `130`, correctly failed verification when Antigravity soft-denied the file-writing tool under `acceptEdits`. The successful successor used the disposable repository with `bypassPermissions`; no production repository was involved.

## Message continuity

Three idempotent messages were used:

| Message | Session | Final state | Attempts |
| --- | --- | --- | --- |
| `ack-me` | A | `acknowledged` | 1 |
| `keep-me` | B | `acknowledged` | 1 |
| `during-handoff` | B | `acknowledged` | 1 |

`ack-me` was not replayed to Session B. The two queued messages were delivered to B in original order, once each. No historical Agent Control message appeared.

## Context efficiency

| Measurement | Result |
| --- | ---: |
| Session A pre-rollover context | 16,111 estimated/provider tokens |
| Structured handoff | 3,142 bytes; about 785 tokens |
| Session B starting Context Pack | 524 estimated tokens |
| Approximate reduction | 15,587 tokens; about 96.7% |
| Session B Context Pack omissions | 0 |

The threshold was deliberately low for this canary. The conversation was not padded. The regular conservative rollover calculation still applies a `1.25` safety margin to the character estimate.

## Failover and restart recovery

The canary temporarily drained `vicoa-agy-1` after the handoff was prepared. Routing selected only the other legitimate Vicoa profile, `vicoa-agy-2`; neither legacy profile was eligible. `vicoa-agy-1` was restored immediately after the disposable proof.

A disposable restart test prepares the handoff, reopens the same control-plane database, runs recovery, and then starts one continuation. The prepared handoff and lineage survive; a competing continuation is refused; exactly two provider sessions exist.

## Routing correction found by the canary

The live generic rollover pack exposed an over-broad selection: a PHP testing skill was included because it also carried a generic verification domain. The final code treats `php`, `wordpress`, and `tillpress` as specialized domains. A generic verification task now excludes the PHP skill with reason `domain_excluded`, while a Tillpress PHP signature task still selects it. This deterministic correction did not require another paid provider run.

## Structured status

The task timeline records `worker_session_started`, `handoff_started`, `handoff_prepared`, `context_pressure`, `session_rolled_over`, `rollover_ready`, `route_explained`, `session_opened`, `session_resumed`, `rollover_completed`, `session_closed`, and `worker_verified`. The future Command Center can read those events, session lineage, selected skills, context use, verification, blockers, and profile state without asking an agent to report status.

## Safety outcome

- Legacy `agy-1` and `agy-2` remained drained.
- Task 6 remained paused, protected, and untouched.
- Legacy tasks 113 and 114 were not restarted and their queued prompts were not delivered.
- Job 106 remained blocked; no paid Batchideo render ran.
- No Tillpress or Checkout Sentinel production deployment or publication occurred.
- The four legacy Agent Control services were not restarted or stopped.
- No disposable database, repository, login data, provider transcript, or raw result file is committed.
