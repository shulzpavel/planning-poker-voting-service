"""App Jira sync and session summary export endpoints."""

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

