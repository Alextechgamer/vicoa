# Runtime isolation

Checked read-only on 2026-09-23. No login, no new session, no message.

| Runtime | Path | Owner | Mode |
|---|---|---|---|
| agy-1 | `/home/agentctl/runtimes/agy-1` | agentctl:agentctl | 0700 |
| agy-2 | `/home/agentctl/runtimes/agy-2` | agentctl:agentctl | 0700 |

The directories are distinct. Each has its own `.tmux` directory, so a tmux
socket in one home is not the socket in the other. Credentials were not
copied into this repository and are not returned by the control-plane API.

A constraint, drain, or provider failure on one account is a row on that
account. Routing still considers the other account. That is covered by
`test_constraint_on_one_account_does_not_disable_the_other` and
`test_quota_route_explains_conserve_and_does_not_invent_stale_zero`.

This does not prove a new simultaneous agy-1 and agy-2 session. Starting one
would spend quota and could touch live work, including paused task 6.
