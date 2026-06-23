# planning-poker-voting-service

FastAPI backend for live planning poker sessions, participant voting, CMS/RBAC, scope boards, retrospectives, and AI orchestration.

**Port:** 8002 (local) · **Prod API:** `https://planning.shults-sync.com/api/v1/*`

## Role in the stack

| Owns | Does **not** |
|---|---|
| Postgres CMS read model, RBAC, team scope | Direct Jira REST calls |
| Redis live sessions, web tokens, AI jobs, rate limits | Static UI (see `planning-poker-web`) |
| Scope/retro domain logic | Jira enrichment (see `planning-poker-jira-service`) |
| HTTP to jira-service, Anthropic, Telegram | |

Canonical architecture: [planning-poker-dev/docs/architecture/SERVICES.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/architecture/SERVICES.md)

## Documentation

| Doc | Topic |
|---|---|
| [TECHNICAL.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/TECHNICAL.md) | Developer entry point |
| [contracts/API.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/contracts/API.md) | All HTTP endpoints |
| [contracts/SCOPE-BOARD.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/contracts/SCOPE-BOARD.md) | Scope snapshot & refresh |
| [contracts/SESSIONS.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/contracts/SESSIONS.md) | Sessions & voting flow |
| [development/GUIDE.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/development/GUIDE.md) | Best practices |

## Run locally

**Recommended** — from `planning-poker-dev`:

```bash
docker compose up -d postgres redis jira-service voting-service
```

**Bare metal** (needs local Postgres + Redis + running jira-service):

```bash
pip install -r requirements.txt
cp .env.example .env
PYTHONPATH=. python -m services.voting_service.main
```

| URL | Purpose |
|---|---|
| http://localhost:8002/health/ready | Readiness (Postgres + Redis) |
| http://localhost:8002/docs | OpenAPI |

## Docker

Build from **repository root** (used by compose and CI):

```bash
docker build -t planning-poker-voting-service .
docker run --rm planning-poker-voting-service python -c "import planning_poker_common"
```

Shared lib is **vendored** under `vendor/planning-poker-common/` and loaded via `PYTHONPATH` ([PYTHON-LIB.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/architecture/PYTHON-LIB.md)).

## Layout

```text
services/voting_service/
  main.py              App wiring, lifespan, health
  cms/                 CMS routers (auth, scope, sessions, planner, …)
  cms_store/           Postgres CMS persistence (mixins)
  app/                 Manager cockpit routes
  web_api.py           Participant voting + WebSocket
  retro_api.py         Retrospectives
  cms_api.py           Shim → cms package
app/domain/            Business logic (scope_board, sessions, …)
app/adapters/          Postgres, Redis, jira_service_client, Anthropic
```

## Rules

- Jira only via `JIRA_SERVICE_URL` → `app/adapters/jira_service_client.py`.
- Reuse `app.state.http_session` — no new `ClientSession` per request.
- Every CMS endpoint: RBAC + team scope (`cms_team_access`).
- Domain logic in `app/domain/`, not in route handlers.

## Tests

```bash
PYTHONPATH=vendor/planning-poker-common:. python -m pytest -q
PYTHONPATH=vendor/planning-poker-common:. python -m compileall -q services app config.py session_store.py
```

From `planning-poker-dev`: `make voting-test` or `make check`.

CI runs pytest, `compileall`, `docker build`, and `import planning_poker_common` in the image. Push to `main` deploys via GitHub Actions (SSH).

## Related repos

- [planning-poker-dev](https://github.com/shulzpavel/planning-poker-dev) — compose, deploy, `sync-vendor-common.sh`
- [planning-poker-jira-service](https://github.com/shulzpavel/planning-poker-jira-service) — Jira adapter
- [planning-poker-web](https://github.com/shulzpavel/planning-poker-web) — UI
