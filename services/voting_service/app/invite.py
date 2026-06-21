"""App invite token regeneration."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Request

from services.voting_service._http_shared import CmsPrincipal, _audit, _get_repo_session
from services.voting_service.rate_limit import enforce_rate_limit
from services.voting_service.app._common import (
    APP_INVITE_RATE_LIMIT_MAX,
    APP_INVITE_RATE_LIMIT_WINDOW_SECONDS,
    _create_invite_token,
    _require_manager_session,
    _resolve_session_title,
    _stored_session_title,
    app_router,
)

@app_router.post("/app/sessions/{chat_id}/invite")
async def app_regenerate_invite(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Mint a fresh invite token for an existing manager session.

    Each refresh writes a new ``web:<token>`` key into Redis, so this is the
    public-token equivalent of /web/token but for the authenticated manager
    surface. Rate-limit by actor so a stuck UI loop can't fill Redis even
    behind an authenticated session.

    Web tokens live in Redis with an 8h TTL and may be evicted before the
    session itself is finished (volume reset, manager comes back the next day,
    etc.). Without this endpoint the manager would see a stale invite_url
    cached in localStorage and participants would hit "Session token not found
    or expired" on /s/<token>. The session itself stays the same chat_id.
    """
    await enforce_rate_limit(
        request.app.state.web_redis,
        key=f"rl:app_invite:actor:{actor.username}",
        limit=APP_INVITE_RATE_LIMIT_MAX,
        window_seconds=APP_INVITE_RATE_LIMIT_WINDOW_SECONDS,
        error_detail="Too many invite refresh requests",
    )
    # Touch the session so we know it exists in the repository before binding
    # a new token to its identity (also normalizes lazily-created sessions).
    await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    token, invite_url = await _create_invite_token(request, chat_id, topic_id, resolved_title)
    await _audit(
        request,
        "app.session.invite_regenerate",
        actor.username,
        "ok",
        {"chat_id": chat_id},
    )
    return {"token": token, "invite_url": invite_url}

