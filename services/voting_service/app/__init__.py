"""Manager app API — route modules register on ``app_router`` at import time."""

from __future__ import annotations

from services.voting_service._http_shared import (
    _audit,
    _get_repo_session,
    _mutate_repo_session,
    _publish_state,
)
from services.voting_service.app._common import (
    AppSessionCreateRequest,
    AppSessionRenameRequest,
    AppSessionStartRequest,
    FinalEstimateRequest,
    ReopenCompletedRequest,
    _create_invite_token,
    _manager_dep,
    _manager_session_payload,
    _resolve_session_title,
    _serialize_completed_task,
    _stored_session_row,
    _stored_session_title,
    app_router,
)
from services.voting_service.app import invite as _invite  # noqa: F401
from services.voting_service.app import jira_export as _jira_export  # noqa: F401
from services.voting_service.app import sessions as _sessions  # noqa: F401
from services.voting_service.app import tasks as _tasks  # noqa: F401
from services.voting_service.app.jira_export import (
    _csv_ai_summary_fields,
    _csv_report,
    _markdown_report,
    _summary_payload,
)
from services.voting_service.app.sessions import app_next_task, app_skip_task
from services.voting_service.session_finish_notify import maybe_notify_session_finished

__all__ = [
    "AppSessionCreateRequest",
    "AppSessionRenameRequest",
    "AppSessionStartRequest",
    "FinalEstimateRequest",
    "ReopenCompletedRequest",
    "app_router",
    "app_next_task",
    "app_skip_task",
    "maybe_notify_session_finished",
    "_audit",
    "_create_invite_token",
    "_csv_ai_summary_fields",
    "_csv_report",
    "_get_repo_session",
    "_manager_dep",
    "_manager_session_payload",
    "_markdown_report",
    "_mutate_repo_session",
    "_publish_state",
    "_resolve_session_title",
    "_serialize_completed_task",
    "_stored_session_row",
    "_stored_session_title",
    "_summary_payload",
]
