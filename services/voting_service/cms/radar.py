"""CMS API — product radar (portfolio workload & signals)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.domain.product_radar_refresh import (
    PRODUCT_RADAR_PARTITION_SIZE,
    refresh_product_radar_partition,
)
from services.voting_service._http_shared import CmsPrincipal, _audit, require_permission
from services.voting_service.cms_rbac import PERM_PLANNER_VIEW
from services.voting_service.cms_store.product_radars import DEFAULT_PRODUCT_RADAR_JQL, DEFAULT_PRODUCT_RADAR_NAME

logger = logging.getLogger(__name__)

router = APIRouter()

ProductRadarRefreshPhase = Literal["start", "partition"]


class ProductRadarCreateRequest(BaseModel):
    name: str = Field(default=DEFAULT_PRODUCT_RADAR_NAME, max_length=200)
    jql: str = Field(default=DEFAULT_PRODUCT_RADAR_JQL, max_length=4000)


class ProductRadarUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    jql: str | None = Field(default=None, max_length=4000)


class ProductRadarRefreshRequest(BaseModel):
    phase: ProductRadarRefreshPhase = "start"
    partition_size: int = Field(default=PRODUCT_RADAR_PARTITION_SIZE, ge=5, le=80)


def _get_cms_store(request: Request):
    return request.app.state.cms_store


def _refresh_progress(snapshot: dict) -> dict:
    state = snapshot.get("refresh_state") or {}
    total = int(state.get("total") or snapshot.get("issue_count") or 0)
    enriched = int(state.get("enriched_count") or 0)
    next_index = int(state.get("next_index") or 0)
    status = str(state.get("status") or ("complete" if snapshot.get("enrichment_complete") else "in_progress"))
    return {
        "status": status,
        "total": total,
        "loaded": total,
        "enriched": enriched if status != "complete" else total,
        "next_index": next_index,
        "partition_size": int(state.get("partition_size") or PRODUCT_RADAR_PARTITION_SIZE),
        "percent": 100 if status == "complete" or not total else min(100, round((next_index / total) * 100)),
    }


@router.get("/cms/product-radars")
async def cms_list_product_radars(
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    items = await store.list_product_radars()
    return {"items": items}


@router.post("/cms/product-radars")
async def cms_create_product_radar(
    body: ProductRadarCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    name = body.name.strip()
    jql = body.jql.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите название")
    if not jql:
        raise HTTPException(status_code=400, detail="Укажите JQL")

    store = _get_cms_store(request)
    radar = await store.create_product_radar(name=name, jql=jql, created_by=actor.id)
    await _audit(request, "cms.product_radar.create", actor.username, "ok", {"radar_id": radar["id"]})
    return {"radar": radar}


@router.get("/cms/product-radars/{radar_id}")
async def cms_get_product_radar(
    radar_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    radar = await store.get_product_radar(radar_id)
    if not radar:
        raise HTTPException(status_code=404, detail="Product radar not found")
    return {"radar": radar}


@router.patch("/cms/product-radars/{radar_id}")
async def cms_update_product_radar(
    radar_id: int,
    body: ProductRadarUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    existing = await store.get_product_radar(radar_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Product radar not found")
    name = body.name.strip() if body.name is not None else None
    jql = body.jql.strip() if body.jql is not None else None
    if name is not None and not name:
        raise HTTPException(status_code=400, detail="Укажите название")
    if jql is not None and not jql:
        raise HTTPException(status_code=400, detail="Укажите JQL")
    updated = await store.update_product_radar(radar_id, name=name, jql=jql)
    await _audit(request, "cms.product_radar.update", actor.username, "ok", {"radar_id": radar_id})
    return {"radar": updated}


@router.delete("/cms/product-radars/{radar_id}")
async def cms_delete_product_radar(
    radar_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    existing = await store.get_product_radar(radar_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Product radar not found")
    deleted = await store.delete_product_radar(radar_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Product radar not found")
    await _audit(request, "cms.product_radar.delete", actor.username, "ok", {"radar_id": radar_id})
    return {"ok": True}


@router.post("/cms/product-radars/{radar_id}/refresh")
async def cms_refresh_product_radar(
    radar_id: int,
    request: Request,
    body: ProductRadarRefreshRequest | None = None,
    force_refresh: bool = Query(False, alias="force"),
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    radar = await store.get_product_radar(radar_id)
    if not radar:
        raise HTTPException(status_code=404, detail="Product radar not found")

    jql = str(radar.get("jql") or "").strip()
    if not jql:
        raise HTTPException(status_code=400, detail="JQL не задан")

    payload = body or ProductRadarRefreshRequest()
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        snapshot = await refresh_product_radar_partition(
            jql=jql,
            client=client,
            previous_snapshot=radar.get("snapshot") if payload.phase == "partition" else None,
            phase=payload.phase,
            partition_size=payload.partition_size,
            force_refresh=force_refresh,
        )
    except RuntimeError as exc:
        logger.warning("product radar refresh failed radar_id=%s error=%s", radar_id, exc)
        raise HTTPException(status_code=503, detail="Jira не ответила — snapshot не обновлён") from exc
    finally:
        await client.close()

    updated = await store.save_product_radar_snapshot(radar_id, snapshot)
    if not updated:
        raise HTTPException(status_code=404, detail="Product radar not found")

    progress = _refresh_progress(snapshot)
    await _audit(
        request,
        "cms.product_radar.refresh",
        actor.username,
        "ok",
        {
            "radar_id": radar_id,
            "phase": payload.phase,
            "issue_count": snapshot.get("issue_count"),
            "health_status": snapshot.get("health_status"),
            "refresh_status": progress.get("status"),
        },
    )
    return {"radar": updated, "refresh_progress": progress}
