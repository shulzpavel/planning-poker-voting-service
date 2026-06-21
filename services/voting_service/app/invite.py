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

