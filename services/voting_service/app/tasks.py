"""App task queue and Jira import endpoints."""

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

