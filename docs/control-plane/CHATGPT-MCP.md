# ChatGPT MCP adapter

This is a protocol adapter for the native Vicoa control plane. It is not a second scheduler and it does not wrap Agent Control.

## Bind

- Loopback only: `127.0.0.1:8098`
- Bearer token from `VICOA_MCP_TOKEN`, compared on every request
- Requests with `X-Forwarded-For` or `Forwarded` are rejected
- The token is not a tool argument and is scrubbed from responses

## Identity

`vicoa_claim_job` uses `VICOA_MCP_CONTROLLER_ID` (default `chatgpt-main`). The tool has no manager-id argument.

## Not exposed

These Agent Control-shaped actions have no safe existing Vicoa method, so they are absent rather than invented:

- `release_job`: a claim stays until the store's own conflict rule. There is no clear-manager mutation.
- `report_manager_error`: no store method.
- `cancel_task`: `kill()` exists and is excluded. This adapter does not expose a raw stop.
- `set_profile_health`: quota is an observation. Missing quota stays unknown. Drain and enable are the only profile mutations, and both refuse `/home/agentctl/` homes. Drain also refuses the `vicoa-agy-*` homes.

Shell, SQL, filesystem, and worktree deletion are not registered.

## Held work

Writes refuse task ids 6, 113, 114, and 124, job ids 70, 71, 106, 111, and 125, and any task that is protected, owner-only, or import-held. The adapter does not accept an override reason.

## Tunnel

The existing tunnel still serves Agent Control on channel `main` at `127.0.0.1:8082`. A Vicoa channel is a separate local URL. Do not replace `/` on Tailscale Serve and do not enable Funnel.
