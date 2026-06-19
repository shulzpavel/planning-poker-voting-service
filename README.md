# Planning Poker — Voting Service

FastAPI service for live sessions, web voting, CMS/RBAC, retrospectives, scope boards, and AI features.

## Documentation

Central docs in [planning-poker-dev/docs](https://github.com/shulzpavel/planning-poker-dev/tree/main/docs):

- [TECHNICAL.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/TECHNICAL.md)
- [architecture/SERVICES.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/architecture/SERVICES.md)
- [contracts/API.md](https://github.com/shulzpavel/planning-poker-dev/blob/main/docs/contracts/API.md)

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env
PYTHONPATH=. python -m services.voting_service.main
```

Health: http://localhost:8002/health  
OpenAPI: http://localhost:8002/docs

## Architecture

```text
services/voting_service/   HTTP layer (app_api, cms_api, web_api, retro_api)
app/domain/              Business logic (scope_board.py, …)
app/adapters/            Postgres, Redis, Jira, Anthropic
```

## Key modules

| Module | Responsibility |
|---|---|
| `cms_api.py` | CMS auth, scope boards, sprint plans, RBAC |
| `app_api.py` | Manager cockpit sessions |
| `web_api.py` | Participant voting + WebSocket |
| `retro_api.py` | Retrospectives |
| `scope_ai_llm.py` | Scope board AI analyze |
| `scope_ai_jira_export.py` | Export AI summary to Jira ADF |
| `ai_jobs.py` | Async AI job orchestration (Redis) |

## Tests

```bash
PYTHONPATH=. python -m pytest -q
```

From dev repo: `make backend-test`.
