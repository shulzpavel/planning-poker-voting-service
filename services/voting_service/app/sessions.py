"""App session lifecycle and voting control endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request

from app.domain.estimation import (
    MAX_STORY_POINTS,
    VALID_ESTIMATION_MODES,
    clear_task_votes,
    get_mode_config,
    is_split_mode,
    normalise_estimation_mode,
)
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.close_session import CloseSessionUseCase
from app.usecases.manage_tasks import ReopenCompletedTaskUseCase, TaskQueueError
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _ensure_current_task_description,
    _get_repo_session,
    _mutate_repo_session,
    _publish_state,
    _raise_task_error,
)
from services.voting_service.ai_summary_llm import (
    LlmSummaryError,
    fetch_jira_issue_context,
    generate_ai_summary_llm,
)
from services.voting_service.cms_team_access import resolve_create_team_id
from services.voting_service.rate_limit import enforce_rate_limit
from services.voting_service.session_finish_notify import maybe_notify_session_finished
from services.voting_service.app._common import (
    AI_SUMMARY_RATE_LIMIT_MAX,
    AI_SUMMARY_RATE_LIMIT_WINDOW_SECONDS,
    COMPLETED_DEFAULT_LIMIT,
    COMPLETED_MAX_LIMIT,
    DEMO_CHAT_ID,
    DEMO_JQL,
    DEMO_TITLE,
    AppSessionCreateRequest,
    AppSessionRenameRequest,
    AppSessionStartRequest,
    FinalEstimateRequest,
    ReopenCompletedRequest,
    _create_invite_token,
    _demo_enabled,
    _manager_dep,
    _manager_session_payload,
    _new_app_chat_id,
    _paginate_completed_in_batch,
    _require_manager_session,
    _resolve_session_title,
    _stored_session_row,
    _stored_session_title,
    app_router,
)

logger = logging.getLogger(__name__)

