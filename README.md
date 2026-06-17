# Planning Poker — Voting Service

FastAPI service for live sessions, web voting, CMS/RBAC, retrospectives, and AI features.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env
PYTHONPATH=. python -m services.voting_service.main
```

Health: `http://localhost:8002/health`
