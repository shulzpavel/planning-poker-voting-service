"""CMS admin API package — aggregates sub-routers into ``cms_router``."""

from __future__ import annotations

from fastapi import APIRouter

from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _existing_jira_keys,
    _jira_preview,
    _jira_preview_payload,
    _mutation_payload,
    _publish_state,
    _raise_task_error,
    require_permission,
)
from services.voting_service.cms import access, auth, lists, overview, planner, scope, sessions, standups
from services.voting_service.cms.scope import (
    DEFAULT_SCOPE_LAYOUT_ORDER,
    SCOPE_JQL_MAX_RESULTS,
    _ScopeJqlFetchResult,
    _count_snapshot_issues,
    _normalize_flow_pace_chart_order,
    _normalize_scope_layout_order,
    _scope_fetch_warnings,
    _scope_snapshot_has_report_issue,
    _scope_snapshot_with_report_comment,
    _scope_snapshot_with_todo_done,
    _scope_snapshot_with_todo_item,
    _scope_snapshot_without_todo_item,
)

cms_router = APIRouter()
cms_router.include_router(auth.router)
cms_router.include_router(overview.router)
cms_router.include_router(access.router)
cms_router.include_router(lists.router)
cms_router.include_router(sessions.router)
cms_router.include_router(planner.router)
cms_router.include_router(standups.router)
cms_router.include_router(scope.router)

__all__ = [
    "CmsPrincipal",
    "_ScopeJqlFetchResult",
    "_audit",
    "_count_snapshot_issues",
    "_existing_jira_keys",
    "_jira_preview",
    "_jira_preview_payload",
    "_mutation_payload",
    "_normalize_flow_pace_chart_order",
    "_normalize_scope_layout_order",
    "_publish_state",
    "_raise_task_error",
    "_scope_fetch_warnings",
    "_scope_snapshot_has_report_issue",
    "_scope_snapshot_with_report_comment",
    "_scope_snapshot_with_todo_done",
    "_scope_snapshot_with_todo_item",
    "_scope_snapshot_without_todo_item",
    "DEFAULT_SCOPE_LAYOUT_ORDER",
    "SCOPE_JQL_MAX_RESULTS",
    "cms_router",
    "require_permission",
]
