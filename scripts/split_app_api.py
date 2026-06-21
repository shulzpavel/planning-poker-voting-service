#!/usr/bin/env python3
"""One-shot script to split app_api.py into services/voting_service/app/."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "services/voting_service/app_api.py"
PKG = ROOT / "services/voting_service/app"

lines = SRC.read_text().splitlines(keepends=True)


def slice_lines(start: int, end: int) -> str:
    return "".join(lines[start - 1 : end])


COMMON_HEADER = '''"""Shared app API router, models, and helpers."""

from __future__ import annotations

'''

COMMON_BODY = slice_lines(5, 97).replace(
    '__all__ = ["app_router", "_publish_state"]\n\n',
    "",
)
COMMON_TAIL = slice_lines(99, 548)

SESSIONS_HEADER = '''"""App session lifecycle and voting control endpoints."""

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

'''

INVITE_HEADER = '''"""App invite token regeneration."""

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

'''

TASKS_HEADER = '''"""App task queue and Jira import endpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import Depends, Query, Request

from app.domain.session import Session
from app.domain.task import Task
from app.usecases.manage_tasks import (
    AddManualTaskUseCase,
    DeleteTaskUseCase,
    MoveTaskUseCase,
    ReorderTasksUseCase,
    TaskMutationResult,
    TaskQueueError,
    UpdateTaskUseCase,
)
from services.voting_service._http_shared import (
    CmsPrincipal,
    JiraImportRequest,
    JiraPreviewRequest,
    TaskCreateRequest,
    TaskMoveRequest,
    TaskReorderRequest,
    TaskUpdateRequest,
    _audit,
    _existing_jira_keys,
    _fetch_jira_description,
    _get_repo_session,
    _jira_preview,
    _jira_preview_payload,
    _mutate_repo_session,
    _mutation_payload,
    _publish_state,
    _raise_task_error,
)
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT
from services.voting_service.app._common import (
    _require_manager_session,
    _task_page,
    app_router,
)

logger = logging.getLogger(__name__)

'''

JIRA_EXPORT_HEADER = '''"""App Jira sync and session summary export endpoints."""

from __future__ import annotations

import csv
import io
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.domain.estimation import estimation_mode_payload
from app.domain.session import Session
from services.voting_service._http_shared import CmsPrincipal, _audit, _get_repo_session
from services.voting_service.app._common import (
    COMPLETED_DEFAULT_LIMIT,
    COMPLETED_MAX_LIMIT,
    _completed_in_batch,
    _final_estimate_label,
    _paginate_completed_in_batch,
    _participant_report_rows,
    _require_manager_session,
    _resolve_session_title,
    _serialize_completed_task,
    _stored_session_title,
    app_router,
)

'''

INIT = '''"""Main web app API for facilitated Planning Poker sessions."""

from __future__ import annotations

from services.voting_service._http_shared import _publish_state
from services.voting_service.app._common import app_router
from services.voting_service.app import invite, jira_export, sessions, tasks  # noqa: F401

from services.voting_service.app._common import (
    AppSessionCreateRequest,
    AppSessionRenameRequest,
    AppSessionStartRequest,
    FinalEstimateRequest,
    ReopenCompletedRequest,
    _manager_session_payload,
    _serialize_completed_task,
)
from services.voting_service.app.jira_export import (
    _csv_ai_summary_fields,
    _csv_report,
    _markdown_report,
    _summary_payload,
)
from services.voting_service.app.sessions import app_next_task, app_skip_task

__all__ = [
    "AppSessionCreateRequest",
    "AppSessionRenameRequest",
    "AppSessionStartRequest",
    "FinalEstimateRequest",
    "ReopenCompletedRequest",
    "_csv_ai_summary_fields",
    "_csv_report",
    "_manager_session_payload",
    "_markdown_report",
    "_publish_state",
    "_serialize_completed_task",
    "_summary_payload",
    "app_next_task",
    "app_router",
    "app_skip_task",
]
'''

SHIM = '''"""Backward-compatible shim — app routes live in ``services.voting_service.app``."""

from services.voting_service._http_shared import (
    _audit,
    _ensure_current_task_description,
    _get_repo_session,
    _mutate_repo_session,
    _publish_state,
)
from services.voting_service.app import *  # noqa: F403
from services.voting_service.app import app_next_task, app_router, app_skip_task
from services.voting_service.session_finish_notify import maybe_notify_session_finished
'''

if __name__ == "__main__":
    PKG.mkdir(parents=True, exist_ok=True)
    (PKG / "_common.py").write_text(COMMON_HEADER + COMMON_BODY + COMMON_TAIL)
    (PKG / "sessions.py").write_text(
        SESSIONS_HEADER + slice_lines(550, 704) + slice_lines(749, 789) + slice_lines(1075, 1473)
    )
    (PKG / "invite.py").write_text(INVITE_HEADER + slice_lines(705, 747))
    (PKG / "tasks.py").write_text(TASKS_HEADER + slice_lines(791, 1073))
    (PKG / "jira_export.py").write_text(JIRA_EXPORT_HEADER + slice_lines(1475, 2098))
    (PKG / "__init__.py").write_text(INIT)
    SRC.write_text(SHIM)
    for path in sorted(PKG.glob("*.py")) + [SRC]:
        print(f"{path.relative_to(ROOT)}: {len(path.read_text().splitlines())} lines")
