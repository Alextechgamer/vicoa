# Self-hosted control plane

The intended install is the existing Vicoa self-host stack, not a second
control plane and not production Supabase.

## Auth

`AUTH_PROVIDER=builtin` is the default in `.env.example` and
`docker-compose.selfhost.yml`. Identity stays in that stack's Postgres.
`NEXT_PUBLIC_AUTH_PROVIDER` must be `builtin` in the same deployment. The
hosted default remains Supabase unless that variable is set explicitly.

Do not point a self-hosted checkout at the production Supabase project.

Docker is the documented way to start Postgres, the backend, and the web app:

```bash
cp .env.example .env
./backend/scripts/generate-jwt-keys.sh selfhost/keys
docker compose -f docker-compose.selfhost.yml up -d
```

This machine has no `docker.service`, so that stack was not started here.
The portfolio check used the same built-in provider on a local Next server.
`VICOA_DEV_SESSION=1` is a development-only cookie helper. It returns 404
unless built-in auth is explicit, the flag is set, and `NODE_ENV` is not
`production`. It is not a production login.

## Control plane

Set these on the web server. Do not commit the token.

```text
VICOA_CONTROL_PLANE_TOKEN
VICOA_CONTROL_PLANE_URL
VICOA_CONTROL_PLANE_DB
```

The token stays server-side. The portfolio page calls
`/api/control-plane/status`, which proxies with that token. Runtime homes and
credentials are not returned.

Profiles are empty homes until the owner signs in. `agy` has no profile flag,
so each home is selected with `HOME`:

```bash
HOME=/home/alex/.vicoa/runtimes/vicoa-agy-1 agy
HOME=/home/alex/.vicoa/runtimes/vicoa-agy-2 agy
```

Use a different Google account in each home. Do not reuse
`/home/agentctl/runtimes/agy-1` or `agy-2`.

The constrained MCP surface is `POST /api/v1/control-plane/mcp` with the same
bearer token. It does not expose shell, SQL, or process kill.
