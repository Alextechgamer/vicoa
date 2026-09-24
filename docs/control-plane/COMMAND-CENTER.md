# Private Command Center

The Command Center is a page in the existing Vicoa web app at `/dashboard/command`. It is not a second product and it does not replace the portfolio page.

## Read path

The browser calls same-origin routes under `/api/control-plane/command-center`. Those routes hold the control-plane bearer token and never send it to the browser. The token stays in the server environment.

The snapshot, task view, and event cursor come from `shared.control_plane.command_center`. That module reads the Vicoa control plane. It does not ask an agent to describe status.

## Auth

Tailscale Serve makes the page reachable only on the tailnet. That is not the login.

The page requires an operator session cookie, `vicoa_command_session`. The cookie is httpOnly, SameSite=Lax, and marked Secure when `VICOA_COMMAND_CENTER_SECURE=1`. The password and signing secret live in `/home/alex/.vicoa/command-center.env`, which is not in git. A missing password fails closed.

The hosted built-in user store is not running on this VM, so this local operator gate sits in front of the existing dashboard shell. It does not replace hosted Vicoa identity and it does not weaken production Supabase auth.

Write actions require the `X-Vicoa-Command` header. Locked tasks, including task 6, tasks 113 and 114, task 124, and jobs 70, 71, 106, and 111, are refused by the server even if the page is asked to act.

## Tailscale

The existing Serve route `/` to `127.0.0.1:8081` is the legacy Agent Control proxy. It is not replaced. The Command Center is served on HTTPS port 8443, tailnet only:

`https://alex-vmware-virtual-platform.tail489f9c.ts.net:8443/dashboard/command`

Funnel is not enabled. The operator password is in `/home/alex/.vicoa/command-center.env`. It is not in git and it is not printed here.

## Not included

- A second database or transcript store.
- A button that starts a paid provider session.
- Any restart of the four legacy services.
- A production Tillpress or Checkout Sentinel deploy.
