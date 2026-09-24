# Private Command Center

The Command Center is a page in the existing Vicoa web app at `/dashboard/command`. It reads the Vicoa control plane. It is not a second product, and it does not replace the portfolio page.

## Production runtime

The live page is the Next.js standalone production server, not `next dev`.

- Unit: `vicoa-command-center.service` (user systemd)
- Working directory: `apps/web/.next/standalone`
- Bind: `127.0.0.1:3012`
- Environment file: `/home/alex/.vicoa/command-center.env`
- Operator password file, outside git: `/home/alex/.vicoa/command-center.password`

Build:

```bash
cd apps/web
NEXT_PUBLIC_AUTH_PROVIDER=builtin pnpm build
mkdir -p .next/standalone/.next
cp -a .next/static .next/standalone/.next/static
cp -a public .next/standalone/public
```

Operate only this unit:

```bash
systemctl --user status vicoa-command-center.service
systemctl --user restart vicoa-command-center.service
journalctl --user -u vicoa-command-center.service -n 50
```

Rollback to the `fa77820` development server means stopping this unit and starting `next dev --port 3012` from that commit. Do not stop the four legacy Agent Control services.

## Authentication

Option B. The hosted built-in user store is not running on this VM, and standing it up would add an unrelated stack. This is an explicit single-operator mode for the private VM, not hosted built-in auth.

- The password is stored as an scrypt verifier in the environment file. The plaintext password is not in that file.
- The session cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` in production.
- Sessions expire after 8 hours.
- Login is limited to 5 attempts per 15 minutes per client address. Failures are a generic `unauthorized`.
- Logout is `DELETE /api/control-plane/command-center/session` with the `X-CSRF-Token` returned by login.
- The control-plane bearer token stays on the server.

Rotate the operator password by writing a new verifier and restarting only this unit. Restarting the unit invalidates old cookies because the signing secret can be rotated in the same file.

## Event stream

The earlier stream closed itself after 20 seconds. The browser then reconnected. The production handler stays open, sends a heartbeat comment every 15 seconds, and includes an event id. A reconnect sends `Last-Event-ID` or `after` and does not replay events already delivered.

The production proof held one connection for 610.7 seconds and received 39 heartbeats. Two disposable canary events arrived while it was open. The reconnect resumed from the saved cursor.

## Task detail

Conversation, activity, verification, and files come from the control plane. Vicoa did not retain a full provider transcript, so the conversation view shows session boundaries and stored messages instead of inventing chat text. File diffs are limited to the task worktree. Path traversal and symlink escapes are rejected.

## Tailscale

The existing Serve route `/` to `127.0.0.1:8081` is the legacy Agent Control proxy. It is not replaced. The Command Center is served on HTTPS port 8443, tailnet only:

`https://alex-vmware-virtual-platform.tail489f9c.ts.net:8443/dashboard/command`

Funnel is not enabled.

## Safety

Locked tasks, including task 6, tasks 113 and 114, task 124, and jobs 70, 71, 106, and 111, are refused by the server. There is no override button.
