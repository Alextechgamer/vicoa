# Session lifecycle

Vicoa already owns projects, human tasks, agent sessions, worktrees, and
automations. The control plane does not add a second session supervisor.

## What stays native

- A worker session is a Vicoa agent instance, not an Atrium session id.
- The worktree is the git worktree Vicoa already records on that instance.
- Verification runs against that worktree path. It does not run against main.
- Evidence rows stay in `verifications` after session cleanup.
- Active work (`running` or `needs_input`) is refused by cleanup.

## What Atrium is, for now

The live VM still uses Atrium to supervise the existing Antigravity workers.
That is the running system. This branch does not delete it and does not start
a replacement session.

Imported Agent Control sessions are shadow copies. `jobs.shadow=1` refuses
`start_task` even after an import hold is released. Turning shadow off is an
explicit cutover step, and protected tasks still refuse mutation without an
override reason.

## Queued messages

`steer_messages.state` is `queued`, `sent`, `acknowledged`, or `failed`.

Queueing does not mean the worker consumed the prompt. Delivery moves the
oldest queued row to `sent` only when the caller says the worker is accepting.
Acknowledgement is a separate call. A repeated idempotency key returns the
same row. A failed row can be retried without creating another row.

## Restart

`recover_after_restart` marks `running` workers `interrupted` and clears
account capacity. It does not start a new worker. Queued messages, approvals,
and verification rows stay.
