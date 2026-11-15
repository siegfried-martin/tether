# Tether

A self-hosted, single-user aggregator for email, calendars and tasks, with an MCP connector
for Claude. Tether pulls several Gmail inboxes into one triage list, merges calendars into one
hub calendar, turns mail into tasks and follow-ups, and lets Claude search mail and manage
tasks on your behalf. The design spec is the source of truth:
[`docs/initial_design_spec.md`](docs/initial_design_spec.md).

## Layout

- `src/tether/`: FastAPI app, service layer, connectors, pollers. SQLite schema in `migrations/`.
- `web/`: React PWA (Vite). Built into `web/dist`, served by the same process.
- `deploy/`: build-and-install script, systemd unit, nginx site template.
- `tests/`: `uv run pytest`.

## Configuration

Everything site-specific lives in environment files, never in the repo:

| File | Read by | Holds |
| --- | --- | --- |
| `.env` (from `.env.example`) | the app, in development | secrets, OAuth client, owner identity, `TETHER_BASE_URL`, `TETHER_HOST`, `TETHER_PORT` |
| `/etc/tether/tether.env` on the server | the app, in production | the same variables, with production values |
| `deploy/.env` (from `deploy/.env.example`) | `deploy/deploy.sh` | `TETHER_DEPLOY_HOST`, the ssh target |

You also need a Google Cloud OAuth client (Web application) with `<TETHER_BASE_URL>/auth/google/callback`
as an authorized redirect URI, and the Gmail and Calendar APIs enabled.

## Develop

```bash
uv sync
cp .env.example .env            # fill it in; see the comments
(cd web && npm install && npm run build)
uv run python -m tether serve --reload
```

Open `TETHER_BASE_URL` (http://localhost:8000 by default). For frontend work, also run
`npm run dev` in `web/` (port 5173, proxies the API to `TETHER_PORT`).

**Always one worker.** The pollers run inside the process; a second worker would duplicate
them. `python -m tether serve` always starts exactly one, so don't run uvicorn by hand with
`--workers`.

## Deploy

Targets an Ubuntu server with Python 3.12+, nginx and certbot, reached over ssh with sudo.

1. Point a DNS record at the server.
2. On the server, create `/etc/tether/tether.env` from `.env.example` with production values:
   `TETHER_BASE_URL=https://<your domain>`, a free `TETHER_PORT`,
   `TETHER_DB_PATH=/var/lib/tether/tether.db` and `TETHER_STATIC_DIR=/opt/tether/web/dist`.
3. Locally, `cp deploy/.env.example deploy/.env` and set `TETHER_DEPLOY_HOST`.
4. Run `deploy/deploy.sh`. It deploys only committed code, and records the revision in
   `/opt/tether/REVISION`.
5. The first time only, add TLS: `sudo certbot --nginx -d <your domain> --redirect`.

The app runs as its own `tether` system user. The database and env file are readable only by
that user, so other apps on the same server can't read them.
