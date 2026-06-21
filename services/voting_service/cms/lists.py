"""CMS list endpoints: users, votes, tokens, web participants, events."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.voting_service.cms_rbac import (
    PERM_EVENTS_VIEW,
    PERM_TOKENS_VIEW,
    PERM_USERS_VIEW,
    PERM_VOTES_VIEW,
    PERM_WEB_PARTICIPANTS_DELETE,
    PERM_WEB_VIEW,
    PERM_APP_SESSIONS_MANAGE,
)
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT, token_hash as compute_token_hash
from services.voting_service.cms_team_access import assert_record_access, assert_user_sessions_access
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _get_cms_store,
    require_permission,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class ParticipantHardDeleteRequest(BaseModel):
    user_id: Optional[str] = Field(default=None, min_length=1, max_length=32)
    confirm_name: str = Field(min_length=1, max_length=200)

@router.delete("/cms/tokens/{token_id}")
async def cms_revoke_token(
    token_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Revoke a single invite token immediately.

    Only the read-model token_hash is recorded in Postgres, so we can't
    construct the original ``web:<token>`` Redis key from the hash. Instead we
    scan Redis once and drop the matching ``web:`` key by comparing hashes.
    """
    store = _get_cms_store(request)
    token = await store.get_web_token(token_id)
    if not token or not token.get("is_active"):
        raise HTTPException(status_code=404, detail="Token not found or already expired")
    assert_record_access(actor, token)

    revoked_hash = await store.revoke_web_token(token_id)
    if not revoked_hash:
        raise HTTPException(status_code=404, detail="Token not found or already expired")

    redis_client = getattr(request.app.state, "web_redis", None)
    if redis_client:
        try:
            async for key in redis_client.scan_iter(match="web:*", count=200):
                token = key.removeprefix("web:")
                if compute_token_hash(token) == revoked_hash:
                    await redis_client.delete(key)
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis revoke for token id=%s failed: %s", token_id, exc)

    await _audit(
        request,
        "cms.token.revoke",
        actor.username,
        "ok",
        {"token_id": token_id},
    )
    return {"ok": True, "token_id": token_id, "revoked": True}


@router.get("/cms/users")
async def cms_users(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    role: Optional[str] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_USERS_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_users(limit=limit, cursor=cursor, q=q, role=role)


@router.delete("/cms/users")
@router.delete("/cms/users/{path_user_id}")
async def cms_hard_delete_user(
    body: ParticipantHardDeleteRequest,
    request: Request,
    path_user_id: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_WEB_PARTICIPANTS_DELETE)),
) -> dict:
    store = _get_cms_store(request)
    raw_user_id = body.user_id or path_user_id
    if raw_user_id is None:
        raise HTTPException(status_code=400, detail="Invalid participant id")
    try:
        user_id = int(raw_user_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid participant id") from exc

    team_ids = await store.get_user_session_team_ids(user_id)
    if team_ids:
        assert_user_sessions_access(actor, team_ids)

    try:
        deleted = await store.hard_delete_user(user_id, body.confirm_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Type the participant name exactly to confirm deletion") from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    return {
        "ok": True,
        "user_id": deleted["user_id"],
        "deleted": True,
        "votes_deleted": deleted["votes_deleted"],
        "session_participants_deleted": deleted["session_participants_deleted"],
        "web_participants_deleted": deleted["web_participants_deleted"],
        "actor": actor.username,
    }


@router.get("/cms/votes")
async def cms_votes(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    user_id: Optional[int] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_VOTES_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_votes(
        limit=limit,
        cursor=cursor,
        session_id=session_id,
        task_id=task_id,
        user_id=user_id,
    )


@router.get("/cms/tokens")
async def cms_tokens(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    active: Optional[bool] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_TOKENS_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_web_tokens(limit=limit, cursor=cursor, active=active)


@router.get("/cms/web-participants")
async def cms_web_participants(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    token_hash: Optional[str] = None,
    active: Optional[bool] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_WEB_VIEW)),
) -> dict:
    return await _get_cms_store(request).list_web_participants(
        limit=limit,
        cursor=cursor,
        token_hash_filter=token_hash,
        active=active,
    )


@router.get("/cms/events")
async def cms_events(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    actor: Optional[str] = Query(default=None, max_length=120),
    ts_from: Optional[datetime] = None,
    ts_to: Optional[datetime] = None,
    _: CmsPrincipal = Depends(require_permission(PERM_EVENTS_VIEW)),
) -> dict:
    """Paged audit-events feed.

    ``actor`` filters by exact username (case sensitive). ``ts_from`` and
    ``ts_to`` are inclusive bounds and are applied before cursor pagination.
    """
    return await _get_cms_store(request).list_audit_events(
        limit=limit,
        cursor=cursor,
        action=action,
        status=status,
        actor=actor,
        ts_from=ts_from,
        ts_to=ts_to,
    )

