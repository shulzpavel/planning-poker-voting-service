"""CMS product radar list/create/delete API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.voting_service import cms_api
from services.voting_service._http_shared import CmsPrincipal, _require_auth
from services.voting_service.cms_rbac import PERM_PLANNER_VIEW


class FakeCmsStore:
    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._next_id = 1

    async def list_product_radars(self) -> list[dict[str, Any]]:
        return [
            {
                **item,
                "snapshot": (
                    {
                        key: item["snapshot"].get(key)
                        for key in ("issue_count", "active_count", "health_status", "refreshed_at")
                    }
                    if item.get("snapshot")
                    else None
                ),
            }
            for item in self._items
        ]

    async def create_product_radar(self, *, name: str, jql: str, created_by: int | None = None) -> dict[str, Any]:
        radar = {
            "id": self._next_id,
            "name": name,
            "jql": jql,
            "snapshot": {"issue_count": 5, "active_count": 3, "health_status": "ok", "refreshed_at": "2026-06-25"},
            "created_by": created_by,
            "created_at": "2026-06-25T12:00:00+00:00",
            "updated_at": "2026-06-25T12:00:00+00:00",
        }
        self._next_id += 1
        self._items.append(radar)
        return radar

    async def get_product_radar(self, radar_id: int) -> dict[str, Any] | None:
        return next((item for item in self._items if item["id"] == radar_id), None)

    async def update_product_radar(self, radar_id: int, *, name: str | None = None, jql: str | None = None) -> dict[str, Any] | None:
        radar = await self.get_product_radar(radar_id)
        if not radar:
            return None
        if name is not None:
            radar["name"] = name
        if jql is not None:
            radar["jql"] = jql
        return radar

    async def delete_product_radar(self, radar_id: int) -> bool:
        before = len(self._items)
        self._items = [item for item in self._items if item["id"] != radar_id]
        return len(self._items) < before

    async def record_audit_event(self, **kwargs: Any) -> None:
        return None


def _app(store: FakeCmsStore | None = None) -> FastAPI:
    cms_store = store or FakeCmsStore()
    app = FastAPI()

    async def _actor() -> CmsPrincipal:
        return CmsPrincipal(
            id=1,
            username="admin",
            display_name=None,
            is_superuser=True,
            permissions=frozenset({PERM_PLANNER_VIEW}),
            roles=(),
            pages=(),
            team_ids=frozenset(),
            teams=(),
        )

    app.state.cms_store = cms_store
    app.dependency_overrides[_require_auth] = _actor
    app.include_router(cms_api.cms_router, prefix="/api/v1")
    return app


def test_cms_product_radars_list_returns_empty_without_auto_create() -> None:
    with TestClient(_app()) as client:
        response = client.get("/api/v1/cms/product-radars")
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_cms_product_radars_create_requires_name_and_jql() -> None:
    with TestClient(_app()) as client:
        missing_name = client.post("/api/v1/cms/product-radars", json={"name": "  ", "jql": "project = X"})
        missing_jql = client.post("/api/v1/cms/product-radars", json={"name": "Портфель", "jql": "  "})
    assert missing_name.status_code == 400
    assert missing_jql.status_code == 400


def test_cms_product_radars_create_and_delete() -> None:
    store = FakeCmsStore()
    with TestClient(_app(store)) as client:
        created = client.post(
            "/api/v1/cms/product-radars",
            json={"name": "Портфель", "jql": "project = BTBMGLBL"},
        )
        assert created.status_code == 200
        radar_id = created.json()["radar"]["id"]
        listed = client.get("/api/v1/cms/product-radars")
        assert listed.json()["items"][0]["name"] == "Портфель"
        assert listed.json()["items"][0]["snapshot"] == {
            "issue_count": 5,
            "active_count": 3,
            "health_status": "ok",
            "refreshed_at": "2026-06-25",
        }
        blank_jql = client.patch(f"/api/v1/cms/product-radars/{radar_id}", json={"jql": "  "})
        assert blank_jql.status_code == 400
        deleted = client.delete(f"/api/v1/cms/product-radars/{radar_id}")
        assert deleted.status_code == 200
        assert client.get("/api/v1/cms/product-radars").json() == {"items": []}
