from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.intake_analysis import build_analysis_facts, validate_analysis
from app.domain.intake_dashboard import build_intake_snapshot
from services.voting_service._http_shared import CmsPrincipal, _require_auth
from services.voting_service.cms.intake import router
from services.voting_service.intake_dashboard_service import IntakeDashboardService
from services.voting_service.intake_settings import IntakeSettings

class Redis:
    def __init__(self): self.data = {}
    async def get(self, key): return self.data.get(key)
    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data: return False
        self.data[key] = value
        return True
    async def setex(self, key, ttl, value): self.data[key] = value
    async def delete(self, key): self.data.pop(key, None)
    async def eval(self, script, count, key, token):
        if self.data.get(key) == token: self.data.pop(key)

@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("INTAKE_DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("INTAKE_DASHBOARD_OWNER", "owner")
    monkeypatch.setenv("INTAKE_DASHBOARD_JQL", "project = TEST")
    monkeypatch.setenv("INTAKE_DASHBOARD_STATUSES", '["Inbox"]')
    return IntakeSettings.from_env()

def actor(username="owner", superuser=False, allowed=True):
    return CmsPrincipal(1, username, None, superuser, frozenset({"cms.planner.view"} if allowed else set()), (), ())

def app(user=None, service=None):
    value = FastAPI()
    value.include_router(router)
    if user:
        value.dependency_overrides[_require_auth] = lambda: user
    value.state.intake_service = service
    return value

@pytest.mark.parametrize("path,method", [("/cms/dashboards/intake", "get"), ("/cms/dashboards/intake/analysis", "get"), ("/cms/dashboards/intake/analysis", "post")])
def test_other_superuser_cannot_read_or_generate_owner_analytics(settings, path, method):
    with TestClient(app(actor("someone-else", superuser=True))) as client:
        assert getattr(client, method)(path).status_code == 404

def test_owner_endpoint_is_authenticated_and_validates_period(settings):
    assert TestClient(app()).get("/cms/dashboards/intake").status_code == 401
    service = AsyncMock()
    service.snapshot.return_value = {"report": {"received": 1}}
    with TestClient(app(actor(), service)) as client:
        assert client.get("/cms/dashboards/intake?days=90").json()["report"]["received"] == 1
        assert client.get("/cms/dashboards/intake?days=5").status_code == 422

def test_disabled_or_missing_owner_configuration_fails_closed(settings, monkeypatch):
    monkeypatch.setenv("INTAKE_DASHBOARD_OWNER", "")
    assert not IntakeSettings.from_env().permits(actor(superuser=True))
    monkeypatch.setenv("INTAKE_DASHBOARD_OWNER", "owner")
    monkeypatch.setenv("INTAKE_DASHBOARD_ENABLED", "false")
    assert TestClient(app(actor())).get("/cms/dashboards/intake").status_code == 404
    assert not settings.permits(actor(allowed=False))

def source(at):
    return {"fetched_at": at.isoformat(), "issues": [{"key": "TEST-1", "summary": "Request", "status": "Inbox", "history_complete": True,
            "segments": [{"status": "Inbox", "entered_at": (at-timedelta(days=4)).isoformat(), "left_at": at.isoformat()}]}]}

@pytest.mark.asyncio
async def test_refresh_is_deduplicated_and_preserves_last_good_source_on_failure(settings):
    import asyncio, json
    redis = Redis()
    manager = IntakeDashboardService(redis, None, settings)
    now = datetime.now(timezone.utc)
    manager._load_source = AsyncMock(return_value=source(now))
    assert await manager.snapshot(30) is None
    assert await manager.snapshot(30) is None
    await asyncio.gather(*manager.tasks)
    assert manager._load_source.await_count == 1
    good = await manager.snapshot(30)
    assert good["report"]["received"] == 1
    old = source(now-timedelta(minutes=10))
    await redis.setex(f"{manager.prefix}:source", 86400, json.dumps(old))
    manager._load_source = AsyncMock(side_effect=RuntimeError("upstream"))
    stale = await manager.snapshot(30)
    assert stale["freshness"]["stale"] is True
    await asyncio.gather(*manager.tasks)
    after = await manager.snapshot(30)
    assert after["generated_at"] == stale["generated_at"]
    assert after["freshness"]["refresh_error"] is True

@pytest.mark.asyncio
async def test_expired_worker_cannot_publish_over_a_newer_lease(settings):
    manager = IntakeDashboardService(Redis(), None, settings)
    key = f"{manager.prefix}:source"
    await manager.redis.set(f"{key}:lock", "new-worker")
    await manager._run(key, "old-worker", AsyncMock(return_value={"bad": True}))
    assert await manager.redis.get(key) is None
    assert await manager.redis.get(f"{key}:lock") == "new-worker"

def test_model_cannot_link_other_chart_tasks_or_invent_evidence(settings):
    at = datetime.now(timezone.utc)
    data = build_intake_snapshot(source(at)["issues"], now=at, days=30, statuses=settings.statuses)
    facts = build_analysis_facts(data)
    valid = {kind: {"headline": "Вывод", "explanation": "Разбор", "focus": "Наблюдение", "fact_ids": [context["facts"][0]["id"]], "keys": []} for kind, context in facts.items()}
    assert validate_analysis(valid, facts)["flow"]["evidence"]
    valid["flow"]["keys"] = ["TEST-1"]
    with pytest.raises(ValueError, match="issue references"):
        validate_analysis(valid, facts)
    valid["flow"]["keys"] = []
    valid["flow"]["fact_ids"] = ["imaginary"]
    with pytest.raises(ValueError, match="fact references"):
        validate_analysis(valid, facts)
