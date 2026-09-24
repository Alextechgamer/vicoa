# Vicoa control-plane release checkpoint

This record covers the completed control-plane, shared-context, rollover, and Command Center work on `feat/private-tailscale-command-center`. It is a review checkpoint. It is not a merge and not a public release.

## Authority

New jobs belong to Vicoa as of `2026-09-23T21:28:36Z`. Legacy Agent Control remains the caretaker for existing sessions. The four services `agent-control`, `agent-orchestrator`, `agent-reaper`, and `agent-mcp` stay running.

Legitimate new-work profiles are `vicoa-agy-1` and `vicoa-agy-2`. Legacy `agy-1` and `agy-2` stay draining.

## What this branch adds

- Native control-plane jobs, tasks, routing, verification, and protected holds.
- Shared skills, memory authority, context budgets, and structured handoffs.
- Automatic Antigravity rollover that continues the same task and worktree.
- Exactly-once queued messages across a handoff.
- Restart recovery and failover that refuse legacy profiles.
- A private Command Center served by the production Next.js unit `vicoa-command-center.service`.

## Canonical proof

Task 131 is the successful live rollover. Session A `2c8b7de5-245f-4d68-a659-0f6281df1ab2` rolled over on `vicoa-agy-1`. Session B `17d33e0e-c6a7-4141-91b7-311a68f945b6` completed on `vicoa-agy-2` under handoff 7. Verification passed.

Tasks 127, 128, and 130 are earlier canary evidence. Task 129 is superseded by task 131. None of those tasks should be scheduled again. There is no typed annotation field that can record this without a schema change, so this document is the annotation.

## Private access

The Command Center is tailnet-only on HTTPS port 8443. The existing Tailscale `/` route still proxies Agent Control on port 8081. Funnel is off.

Operator login is a local scrypt verifier. The password, hash, and signing secret are not in git.

## Rollback

The feature branch can be left unmerged. Stopping only `vicoa-command-center.service` removes the private page. Do not stop the four legacy services. Do not delete the control-plane database.

## Held work

Task 6 stays paused and protected. Tasks 113 and 114 stay `needs_input` and held. Job 106 stays blocked. Job 111 stays planned. Task 124 stays queued under an import hold.

## Not done by this checkpoint

No merge. No public release. No product deploy. No paid render. No restart of the finished skills, memory, rollover, or Command Center implementation.
