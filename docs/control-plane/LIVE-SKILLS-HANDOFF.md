# Live skills, memory, and handoff

Run against the Vicoa checkout at `feat/shared-skills-memory-context`. The live store is `/home/alex/.vicoa/control-plane.db`. Agent Control was not written. Task 6, jobs 70/71, and the four legacy services were not touched.

## Integration

The authoritative checkout was already this branch, one commit ahead of `feat/unified-control-plane` and not behind it. No unrelated product branch was merged. The running portfolio process had loaded the older modules, so it was reloaded after the schema change.

Backup: `/home/alex/.vicoa/backups/20260924T032300Z/control-plane.db` (180224 bytes).

The live database is SQLite. It has no Alembic version table. Opening `ControlPlane` applies `KNOWLEDGE_SCHEMA`, which is the same SQL as revision `b4e7c2a91d18`. That revision is recorded in `schema_revisions`. Accounts stayed 4, jobs 111, tasks 124, and steer messages 0. Task 6 stayed paused and protected.

## Registry

Discovered 218 source files. Imported 130 skill versions. Active: 12. Inactive imports: 110. Skipped: 93. Duplicates: 1. Conflicts: 0.

Redacted Hermes skills stayed inactive. They need owner confirmation before activation: `plan`, `github-pr-workflow`, `github-issues`, `github-code-review`, `test-driven-development`, `systematic-debugging`, `grounded-citations`, and `wordpress-rest-ops`.

The active set covers planning, git reuse, review, security, testing, research, WordPress/PHP, Tillpress verification, and handoff. Hermes-only skills were not given to Antigravity. YouTube and Batchideo sources were not activated.

## Canaries

Tillpress PHP routing selected `vicoa-tillpress-verification` and did not select it for a research task. Owner memory stayed active. Untrusted content was `untrusted_rejected`. Agent inference was `higher_authority_kept`.

Real rollover on task 128, profile `vicoa-agy-1`:

- Session A: `f1cbfedf-7043-4a9f-97b2-55ef9c1cc13c`, wrote `PHASE-A-OK`
- Session B: `cfeb96d4-bb08-4ea6-b3a2-412935b7a0b4`, wrote `PHASE-B-OK`
- Phase A was unchanged after Session B
- Verification passed
- `ack-me` was acknowledged before the handoff and was not delivered to B
- B received `keep-me` and `during-handoff` once
- A second continuation for the same handoff was refused
- Restarting Session A was refused with `session_reused`
- The threshold was the measured prompt size, 29 estimated tokens. The conversation was not padded.

Restart and failover were proven on a disposable database. Recovery did not create a second continuation. Draining `vicoa-agy-1` there selected `vicoa-agy-2`. The live profiles were not drained. Legacy `agy-1` and `agy-2` remain draining.

A Hermes adapter rendered the same context pack. It did not copy the skill files.

`cl100k_base` was available only in a temporary environment. On three samples the chars/4 estimate was at or above the tokenizer count, so the estimator was not increased. Rollover still applies a 1.25 margin.

## Automatic live rollover closure

The earlier task 128 proof used real sessions but manual lifecycle calls. Task 131 closed the remaining gate through `AntigravityTaskWorker`, which watches provider context and owns the Session A to Session B transition once invoked by the existing dispatcher. It does not add another scheduler.

- Session A `2c8b7de5-245f-4d68-a659-0f6281df1ab2` ran on `vicoa-agy-1`.
- Session B `17d33e0e-c6a7-4141-91b7-311a68f945b6` ran on `vicoa-agy-2`.
- Handoff 7 was resumed once with correct parent/child lineage.
- The same task 131 and disposable worktree continued.
- Phase A was unchanged; Phase B was added; deterministic verification passed.
- `ack-me`, `keep-me`, and `during-handoff` were each acknowledged in one attempt and correct order.
- Session A measured 16,111 tokens. Session B started from a 524-token Context Pack, about 15,587 tokens smaller.
- The structured handoff was 3,142 bytes, about 785 estimated tokens.
- No Antigravity process remained after completion.

The live canary also exposed an over-broad PHP skill match on a generic verification task. The final routing logic treats PHP, WordPress, and Tillpress as specialized domains. The generic task now records that skill as `domain_excluded`; the Tillpress task still selects it. See `AUTOMATIC-ROLLOVER-CANARY.md`.

Restart recovery is covered with a reopened disposable database: the prepared handoff survives, one Session B claims it, and a competing continuation is refused.

## Final validation

- Control-plane, HTTP/MCP, profile, local-worker, automatic-worker, and RPC skill tests: 64 passed.
- Antigravity event/session/spec tests: 48 passed.
- PostgreSQL Alembic upgrade, downgrade, re-upgrade, and persistence roundtrip: passed.
- Ruff 0.11.13 on every changed Python file: passed.
- Gitleaks 8.28.0 on the staged patch: zero findings. Historical repository findings were unchanged.
- TypeScript: not applicable; no TypeScript changed.

## Checkout Sentinel

Job 111 / task 124 is still planned, queued, and held. The success path in `includes/class-csent-license.php` calls `verify_response` before `persist`. A failed check persists `was_active` as false. No patch, merge, or deploy was made.

## Not done

No Command Center page was added. The status payload now includes a `knowledge` object from control-plane state. No production Tillpress change was made. No local tokenizer is installed in the Vicoa runtime.
