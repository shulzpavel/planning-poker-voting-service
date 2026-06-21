"""CMS authentication endpoints."""

from __future__ import annotations

import json
import secrets
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from services.voting_service.cms_auth import build_cms_token_payload
from services.voting_service.rate_limit import enforce_rate_limit
from services.voting_service._http_shared import (
    ALLOWED_THEME_PREFERENCES,
    AuthDep,
    CMS_COOKIE_NAME,
    CMS_COOKIE_SECURE,
    CMS_LOGIN_IP_MAX_ATTEMPTS,
    CMS_LOGIN_IP_WINDOW_SECONDS,
    CMS_LOGIN_MAX_ATTEMPTS,
    CMS_LOGIN_WINDOW_SECONDS,
    CMS_TOKEN_TTL,
    CmsPrincipal,
    _audit,
    _client_ip,
    _extract_bearer,
    _get_cms_store,
    _get_redis,
)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str

async def _ensure_login_not_limited(redis_client: aioredis.Redis, username: str, ip: str) -> str:
    key = f"cms_login_fail:{username}:{ip}"
    attempts_raw = await redis_client.get(key)
    attempts = int(attempts_raw or 0)
    if attempts >= CMS_LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts")
    return key


async def _record_login_failure(redis_client: aioredis.Redis, key: str) -> None:
    attempts = await redis_client.incr(key)
    if attempts == 1:
        await redis_client.expire(key, CMS_LOGIN_WINDOW_SECONDS)


@router.post("/cms/auth/login")
async def cms_login(body: LoginRequest, request: Request, response: Response) -> dict:
    redis_client = await _get_redis(request)
    ip = _client_ip(request)
    # Defence-in-depth: cap total login attempts from a single IP
    # regardless of username, so username enumeration from one source
    # cannot stay under the per-username quota indefinitely.
    await enforce_rate_limit(
        redis_client,
        key=f"rl:login:ip:{ip}",
        limit=CMS_LOGIN_IP_MAX_ATTEMPTS,
        window_seconds=CMS_LOGIN_IP_WINDOW_SECONDS,
        error_detail="Too many login attempts",
    )
    fail_key = await _ensure_login_not_limited(redis_client, body.username, ip)

    principal_record = await _get_cms_store(request).verify_admin_login(body.username, body.password)
    if not principal_record:
        await _record_login_failure(redis_client, fail_key)
        await _audit(request, "cms.login", body.username, "failed", {"reason": "invalid_credentials"})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    await redis_client.delete(fail_key)
    token = secrets.token_urlsafe(32)
    token_payload = build_cms_token_payload(
        admin_id=int(principal_record["id"]),
        username=principal_record["username"],
        ip=ip,
        token_version=int(principal_record.get("token_version", 1)),
        ttl_seconds=CMS_TOKEN_TTL,
    )
    await redis_client.setex(
        f"cms_token:{token}",
        CMS_TOKEN_TTL,
        json.dumps(token_payload),
    )
    response.set_cookie(
        CMS_COOKIE_NAME,
        token,
        max_age=CMS_TOKEN_TTL,
        httponly=True,
        secure=CMS_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    await _audit(request, "cms.login", principal_record["username"], "ok")
    return {"ok": True, "expires_in": CMS_TOKEN_TTL}


@router.post("/cms/auth/logout")
async def cms_logout(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(default=None),
    cookie_token: Optional[str] = Cookie(default=None, alias=CMS_COOKIE_NAME),
    actor: CmsPrincipal = AuthDep,
) -> dict:
    token = cookie_token or _extract_bearer(authorization)
    if token:
        redis_client = await _get_redis(request)
        await redis_client.delete(f"cms_token:{token}")
    response.delete_cookie(CMS_COOKIE_NAME, path="/")
    await _audit(request, "cms.logout", actor.username, "ok")
    return {"ok": True}


@router.get("/cms/auth/me")
async def cms_me(actor: CmsPrincipal = AuthDep) -> dict:
    return {
        "id": actor.id,
        "username": actor.username,
        "display_name": actor.display_name,
        "is_superuser": actor.is_superuser,
        "permissions": sorted(actor.permissions),
        "roles": list(actor.roles),
        "pages": list(actor.pages),
        "teams": list(actor.teams),
        "team_ids": sorted(actor.team_ids),
        "theme_preference": actor.theme_preference,
    }


class PreferencesUpdateRequest(BaseModel):
    theme_preference: str = Field(pattern=r"^(dark|light|system)$")


@router.patch("/cms/auth/me/preferences")
async def cms_update_preferences(
    body: PreferencesUpdateRequest,
    request: Request,
    actor: CmsPrincipal = AuthDep,
) -> dict:
    """Persist UI preferences (currently: theme) for the authenticated CMS user."""
    theme = body.theme_preference
    if theme not in ALLOWED_THEME_PREFERENCES:
        raise HTTPException(status_code=400, detail="Invalid theme_preference")
    store = _get_cms_store(request)
    try:
        ok = await store.update_admin_theme_preference(actor.id, theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        # Account is missing or deactivated — treat as unauthorized rather than 404
        # to avoid leaking account state.
        raise HTTPException(status_code=401, detail="Account is no longer active")
    await _audit(
        request,
        "cms.preferences.update",
        actor.username,
        "ok",
        {"theme_preference": theme},
    )
    return {"ok": True, "theme_preference": theme}

