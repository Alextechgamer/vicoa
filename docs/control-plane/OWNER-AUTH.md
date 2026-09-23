# Owner authentication for Vicoa Antigravity profiles

Do not run these commands as part of an unattended migration. They start an interactive Google sign-in.

`agy` has no profile flag. On this machine, each live Agent Control runtime keeps its own Gemini state under that runtime's `HOME`:

- `/home/agentctl/runtimes/agy-1/.gemini`
- `/home/agentctl/runtimes/agy-2/.gemini`

Empty Vicoa homes were created and contain no `.gemini` directory and no copied credential files:

- `/home/alex/.vicoa/runtimes/vicoa-agy-1`
- `/home/alex/.vicoa/runtimes/vicoa-agy-2`

They are not authenticated. Sign in only after you intend to spend that account's quota, and only with `HOME` pointed at the new directory:

```bash
HOME=/home/alex/.vicoa/runtimes/vicoa-agy-1 /home/alex/.local/bin/agy
HOME=/home/alex/.vicoa/runtimes/vicoa-agy-2 /home/alex/.local/bin/agy
```

Complete the Google sign-in in that process, then exit. Do not set `HOME` to either live runtime. Do not copy `.gemini` from `/home/agentctl`. Do not run `agy` as `agentctl`.

Until both homes have their own signed-in state, the real `AntigravitySession` canary stays blocked. `LocalWorker` is not a substitute for that gate.
