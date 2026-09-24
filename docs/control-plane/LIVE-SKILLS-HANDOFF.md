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

## Checkout Sentinel

Job 111 / task 124 is still planned, queued, and held. The success path in `includes/class-csent-license.php` calls `verify_response` before `persist`. A failed check persists `was_active` as false. No patch, merge, or deploy was made.

## Not done

No Command Center page was added. The status payload now includes a `knowledge` object from control-plane state. No production Tillpress change was made. No local tokenizer is installed in the Vicoa runtime.
