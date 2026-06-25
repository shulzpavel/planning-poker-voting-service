"""CMS scope board endpoints and helpers."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.domain.scope_board import (
    apply_priority_queue_comment,
    apply_priority_queue_ranked_update,
    apply_priority_queue_reorder,
    build_release_context,
    build_scope_snapshot,
    clear_priority_queue_ranked,
    compute_scope_metrics,
    compute_scope_metrics_from_sections,
    compute_scope_report,
    compute_scope_report_from_sections,
    infer_scope_report_type,
    infer_release_version_lookup,
    is_scope_creep,
    jql_has_status_filter,
    merge_priority_queue,
    merge_jira_role_fields_configured,
    merge_scope_issues,
    normalise_workload_mode,
    normalize_scope_issue,
    normalize_scope_sections,
    normalize_version_meta,
    pause_supplement_jql,
    refresh_scope_snapshot_metrics,
    priority_queue_label,
    priority_queue_milestone_targets,
    queue_significance_positions,
    release_scope_sections,
    sync_legacy_jql_from_sections,
)
from app.domain.scope_flow_pace import (
    apply_flow_pace_chart_order,
    compute_scope_flow_pace,
    normalize_flow_pace_chart_order,
)
from services.voting_service.cms_rbac import PERM_PLANNER_VIEW
from services.voting_service.cms_team_access import assert_record_access, resolve_create_team_id, team_scope
from services.voting_service.rate_limit import enforce_rate_limit
from services.voting_service.scope_ai_jira_export import normalize_plan_epic_key
from services.voting_service._http_shared import (
    CmsPrincipal,
    _audit,
    _get_cms_store,
    _get_redis,
    require_permission,
)
from planning_poker_common.scope.team_questions import (
    extract_team_scope_questions_from_snapshot,
    manual_question_with_release_meta,
    merge_team_scope_questions_into_snapshot,
    register_open_jira_questions,
    resolved_question_with_release_meta,
    snapshot_open_jira_question_ids,
    team_scope_questions_empty,
    union_team_scope_questions,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_release_scope_team(board: dict[str, Any]) -> bool:
    team = board.get("team") or {}
    return infer_scope_report_type(team.get("slug")) == "release"


def _board_release_label(board: dict[str, Any], snapshot: dict[str, Any] | None = None) -> str:
    snap = snapshot if snapshot is not None else (board.get("snapshot") or {})
    release_context = snap.get("release_context") or {}
    current = release_context.get("current") or {}
    version_meta = current.get("version_meta") or {}
    for candidate in (
        version_meta.get("name"),
        current.get("version_name"),
        current.get("label"),
        board.get("name"),
    ):
        cleaned = str(candidate or "").strip()
        if cleaned:
            return cleaned
    return "Релиз"


async def _ensure_team_scope_questions(store: Any, team_id: int) -> dict[str, Any]:
    questions = await store.get_team_scope_questions(team_id)
    if team_scope_questions_empty(questions):
        backfilled = await store.backfill_team_scope_questions_from_boards(team_id)
        if not team_scope_questions_empty(backfilled):
            await store.save_team_scope_questions(team_id, backfilled)
            return backfilled
    return questions


async def _apply_release_team_questions(store: Any, board: dict[str, Any]) -> dict[str, Any]:
    if not _is_release_scope_team(board):
        return board
    team_id = board.get("team_id")
    if not team_id:
        return board
    snapshot = board.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {
            "plan_issues": [],
            "unplan_issues": [],
            "metrics": {},
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
    team_questions = await _ensure_team_scope_questions(store, int(team_id))
    open_ids = snapshot_open_jira_question_ids(snapshot)
    registered = register_open_jira_questions(
        team_questions,
        question_ids=open_ids,
        release_name=_board_release_label(board, snapshot),
    )
    if registered != team_questions:
        await store.save_team_scope_questions(int(team_id), registered)
        team_questions = registered
    merged = merge_team_scope_questions_into_snapshot(snapshot, team_questions, open_jira_ids=open_ids)
    return {**board, "snapshot": merged}


async def _sync_release_team_questions(store: Any, board: dict[str, Any], snapshot: dict[str, Any]) -> None:
    if not _is_release_scope_team(board):
        return
    team_id = board.get("team_id")
    if not team_id:
        return
    existing = await store.get_team_scope_questions(int(team_id))
    incoming = extract_team_scope_questions_from_snapshot(snapshot)
    merged = union_team_scope_questions(existing, incoming)
    await store.save_team_scope_questions(int(team_id), merged)


async def _get_scope_board_for_mutation(store: Any, board_id: int) -> Optional[dict[str, Any]]:
    board = await store.get_scope_board(board_id)
    if not board:
        return None
    return await _apply_release_team_questions(store, board)


class ScopeSectionConfigRequest(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    jql: str = Field(min_length=1, max_length=4000)
    kind: Literal["planned", "unplanned"] = "planned"
    order: int = Field(ge=0, le=99)


class ScopeBoardCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    month: str = Field(min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$")
    capacity_sp: float = Field(ge=0, le=99999)
    capacity_sp_dev: Optional[float] = Field(default=None, ge=0, le=99999)
    capacity_sp_test: Optional[float] = Field(default=None, ge=0, le=99999)
    workload_mode: Literal["sp", "sp_dev_test"] = "sp"
    scope_sections: list[ScopeSectionConfigRequest] = Field(min_length=1, max_length=20)
    plan_jql: str = Field(default="", max_length=4000)
    unplan_jql: str = Field(default="", max_length=4000)
    todo_jql: str = Field(default="", max_length=4000)
    test_jql: str = Field(default="", max_length=4000)
    previous_release_jql: str = Field(default="", max_length=4000)
    next_release_jql: str = Field(default="", max_length=4000)
    custom_release_name: str = Field(default="", max_length=200)
    custom_release_jql: str = Field(default="", max_length=4000)
    release_queries: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    release_comment: str = Field(default="", max_length=8000)
    previous_release_comment: str = Field(default="", max_length=8000)
    next_release_comment: str = Field(default="", max_length=8000)
    custom_release_comment: str = Field(default="", max_length=8000)
    plan_epic_key: str = Field(default="", max_length=64)
    team_id: Optional[int] = None


class ScopeBoardUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    month: str = Field(min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$")
    capacity_sp: float = Field(ge=0, le=99999)
    capacity_sp_dev: Optional[float] = Field(default=None, ge=0, le=99999)
    capacity_sp_test: Optional[float] = Field(default=None, ge=0, le=99999)
    workload_mode: Literal["sp", "sp_dev_test"] = "sp"
    scope_sections: list[ScopeSectionConfigRequest] = Field(min_length=1, max_length=20)
    plan_jql: str = Field(default="", max_length=4000)
    unplan_jql: str = Field(default="", max_length=4000)
    todo_jql: str = Field(default="", max_length=4000)
    test_jql: str = Field(default="", max_length=4000)
    previous_release_jql: str = Field(default="", max_length=4000)
    next_release_jql: str = Field(default="", max_length=4000)
    custom_release_name: str = Field(default="", max_length=200)
    custom_release_jql: str = Field(default="", max_length=4000)
    release_queries: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    release_comment: str = Field(default="", max_length=8000)
    previous_release_comment: str = Field(default="", max_length=8000)
    next_release_comment: str = Field(default="", max_length=8000)
    custom_release_comment: str = Field(default="", max_length=8000)
    plan_epic_key: str = Field(default="", max_length=64)


class ScopeBoardReleaseCommentsRequest(BaseModel):
    release_comment: str = Field(default="", max_length=8000)
    previous_release_comment: str = Field(default="", max_length=8000)
    next_release_comment: str = Field(default="", max_length=8000)
    custom_release_comment: str = Field(default="", max_length=8000)


class ScopeBoardLayoutRequest(BaseModel):
    layout_order: list[str] = Field(min_length=1, max_length=20)


class ScopeBoardFlowPaceChartOrderRequest(BaseModel):
    chart_order: list[str] = Field(min_length=1, max_length=12)


SCOPE_LAYOUT_BLOCK_KEYS = frozenset({
    "topItems",
    "capacity",
    "roleWorkload",
    "planInsights",
    "flowPace",
    "aiSummary",
    "report",
    "priorityQueues",
    "activity",
    "snapshotSections",
    "settings",
})

DEFAULT_SCOPE_LAYOUT_ORDER = [
    "topItems",
    "capacity",
    "roleWorkload",
    "planInsights",
    "flowPace",
    "aiSummary",
    "report",
    "priorityQueues",
    "activity",
    "snapshotSections",
    "settings",
]


def _normalize_scope_layout_order(layout_order: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in layout_order:
        key = str(item).strip()
        if not key or key not in SCOPE_LAYOUT_BLOCK_KEYS or key in seen:
            continue
        cleaned.append(key)
        seen.add(key)
    for key in DEFAULT_SCOPE_LAYOUT_ORDER:
        if key not in seen:
            cleaned.append(key)
            seen.add(key)
    return cleaned


def _normalize_flow_pace_chart_order(chart_order: list[str]) -> list[str]:
    return normalize_flow_pace_chart_order(chart_order)


class ScopeIssueCommentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ScopeReportCommentRequest(BaseModel):
    text: str = Field(default="", max_length=2000)


class ScopeIssueDueDateRequest(BaseModel):
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ScopeManualQuestionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ScopeTopItemRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ScopeTodoItemRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ScopeTodoItemUpdateRequest(BaseModel):
    done: bool


class ScopeResolveQuestionRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)


class ScopeQueueReorderRequest(BaseModel):
    order: list[str] = Field(default_factory=list, max_length=500)
    comment: str = Field(default="", max_length=4000)
    moved_key: str = Field(min_length=1, max_length=64)

SCOPE_JQL_MAX_RESULTS = max(1, int(os.getenv("SCOPE_JQL_MAX_RESULTS", "500")))


@dataclass
class _ScopeJqlFetchResult:
    jql: str
    issues: list[dict[str, Any]]
    failed: bool = False
    truncated: bool = False
    jira_role_fields_configured: dict[str, bool] | None = None


def _count_snapshot_issues(snapshot: dict[str, Any] | None) -> int:
    if not snapshot:
        return 0
    total = 0
    for section in snapshot.get("sections") or []:
        total += len(section.get("issues") or [])
    for bucket in ("plan_issues", "unplan_issues"):
        total += len(snapshot.get(bucket) or [])
    for queue_name in ("todo", "test"):
        queue = (snapshot.get("priority_queues") or {}).get(queue_name) or {}
        total += len(queue.get("issues") or [])
    return total


async def _fetch_scope_issues(
    jql: str,
    client: Any,
    *,
    force_refresh: bool = False,
    milestone_status_targets: list[str] | None = None,
    enrich_changelog: bool = False,
) -> _ScopeJqlFetchResult:
    cleaned = (jql or "").strip()
    if not cleaned:
        return _ScopeJqlFetchResult(jql="", issues=[])
    try:
        raw_payload = await client.parse_jira_scope_issues(
            cleaned,
            max_results=SCOPE_JQL_MAX_RESULTS,
            force_refresh=force_refresh,
            milestone_status_targets=milestone_status_targets,
            enrich_changelog=enrich_changelog,
        )
    except Exception as exc:
        logger.warning("scope jql fetch failed jql=%s error=%s", cleaned, exc)
        return _ScopeJqlFetchResult(jql=cleaned, issues=[], failed=True)
    if raw_payload is None:
        return _ScopeJqlFetchResult(jql=cleaned, issues=[], failed=True)
    if isinstance(raw_payload, dict):
        raw_issues = raw_payload.get("issues") or []
        configured = raw_payload.get("jira_role_fields_configured") or {}
    else:
        raw_issues = raw_payload
        configured = {}
    issues = [normalize_scope_issue(issue) for issue in raw_issues]
    return _ScopeJqlFetchResult(
        jql=cleaned,
        issues=issues,
        truncated=len(issues) >= SCOPE_JQL_MAX_RESULTS,
        jira_role_fields_configured=configured,
    )


async def _fetch_scope_section(
    section: dict[str, Any],
    client: Any,
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], list[_ScopeJqlFetchResult]]:
    jql = str(section.get("jql") or "").strip()
    if not jql:
        return {**section, "issues": []}, []

    if jql_has_status_filter(jql):
        base_outcome, pause_outcome = await asyncio.gather(
            _fetch_scope_issues(
                jql,
                client,
                force_refresh=force_refresh,
                enrich_changelog=True,
            ),
            _fetch_scope_issues(
                pause_supplement_jql(jql),
                client,
                force_refresh=force_refresh,
                enrich_changelog=True,
            ),
        )
        return (
            {**section, "issues": merge_scope_issues(base_outcome.issues, pause_outcome.issues)},
            [base_outcome, pause_outcome],
        )

    base_outcome = await _fetch_scope_issues(
        jql,
        client,
        force_refresh=force_refresh,
        enrich_changelog=True,
    )
    return {**section, "issues": base_outcome.issues}, [base_outcome]


async def _fetch_scope_sections(
    sections: list[dict[str, Any]],
    client: Any,
    *,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[_ScopeJqlFetchResult]]:
    if not sections:
        return [], []

    results = await asyncio.gather(
        *[
            _fetch_scope_section(section, client, force_refresh=force_refresh)
            for section in sections
        ]
    )
    fetched_sections = [item[0] for item in results]
    outcomes = [outcome for item in results for outcome in item[1]]
    return fetched_sections, outcomes


def _scope_sections_from_request(body: ScopeBoardCreateRequest | ScopeBoardUpdateRequest) -> list[dict[str, Any]]:
    if body.scope_sections:
        return normalize_scope_sections([section.model_dump() for section in body.scope_sections])
    return normalize_scope_sections(None, plan_jql=body.plan_jql, unplan_jql=body.unplan_jql)


async def _resolve_scope_report_type(store: Any, team_id: Optional[int]) -> str:
    if team_id is None:
        return "monthly"
    team = await store.get_team(team_id)
    if not team:
        return "monthly"
    return infer_scope_report_type(team.get("slug"), team.get("name"))


def _ensure_release_scope_sections(scope_sections: list[dict[str, Any]]) -> None:
    release_jql = str((scope_sections[0] if scope_sections else {}).get("jql") or "").strip()
    if not release_jql:
        raise HTTPException(
            status_code=400,
            detail="Укажите JQL релиза, например: project = AIG2 AND fixVersion = 12076",
        )


def _primary_release_jql(scope_sections: list[dict[str, Any]]) -> str:
    if not scope_sections:
        return ""
    return str(scope_sections[0].get("jql") or "").strip()


async def _fetch_release_version_meta(
    client: Any,
    jql: str,
    issues: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    lookup = infer_release_version_lookup(jql, issues)
    version_id = lookup.get("version_id", "")
    version_name = lookup.get("version_name", "")
    project_key = lookup.get("project_key", "")
    if not version_id and not version_name:
        return None
    try:
        raw = await client.resolve_version(
            project_key=project_key,
            version_id=version_id,
            version_name=version_name,
            force_refresh=True,
        )
    except Exception as exc:
        logger.warning(
            "release version meta fetch failed project=%s version_id=%s version_name=%s error=%s",
            project_key,
            version_id,
            version_name,
            exc,
        )
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_version_meta(raw, project_key=project_key)


async def _fetch_release_version_meta_map(
    client: Any,
    jql_by_slot: dict[str, str],
    issues_by_slot: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> dict[str, dict[str, Any]]:
    issues_by_slot = issues_by_slot or {}
    tasks = {
        slot: _fetch_release_version_meta(client, jql, issues=issues_by_slot.get(slot))
        for slot, jql in jql_by_slot.items()
        if (jql or "").strip()
    }
    if not tasks:
        return {}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    meta_map: dict[str, dict[str, Any]] = {}
    for (slot, _), result in zip(tasks.items(), results, strict=True):
        if isinstance(result, dict):
            meta_map[slot] = result
    return meta_map


def _normalize_release_queries(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        jql = str(item.get("jql") or "").strip()
        if not jql:
            continue
        relation = str(item.get("type") or item.get("relation") or "future").strip().lower()
        if relation not in {"past", "future"}:
            relation = "future"
        item_id = str(item.get("id") or "").strip() or f"release-{index + 1}-{secrets.token_hex(3)}"
        while item_id in seen:
            item_id = f"{item_id}-{secrets.token_hex(2)}"
        seen.add(item_id)
        label = str(item.get("label") or "").strip()
        result.append(
            {
                "id": item_id[:120],
                "type": relation,
                "label": label[:200],
                "jql": jql[:4000],
            }
        )
    return result


def _scope_board_payload_from_request(
    body: ScopeBoardCreateRequest | ScopeBoardUpdateRequest,
    *,
    report_type: str,
) -> dict[str, Any]:
    scope_sections = _scope_sections_from_request(body)
    if report_type == "release":
        release_jql = _primary_release_jql(scope_sections)
        scope_sections = release_scope_sections(release_jql, label=scope_sections[0].get("name") if scope_sections else None)
        _ensure_release_scope_sections(scope_sections)
    plan_jql, unplan_jql = sync_legacy_jql_from_sections(scope_sections)
    return {
        "name": body.name,
        "month": body.month,
        "capacity_sp": body.capacity_sp,
        "capacity_sp_dev": body.capacity_sp_dev,
        "capacity_sp_test": body.capacity_sp_test,
        "workload_mode": normalise_workload_mode(body.workload_mode),
        "plan_jql": plan_jql,
        "unplan_jql": unplan_jql,
        "todo_jql": body.todo_jql,
        "test_jql": body.test_jql,
        "report_type": report_type,
        "previous_release_jql": body.previous_release_jql,
        "next_release_jql": body.next_release_jql,
        "custom_release_name": body.custom_release_name,
        "custom_release_jql": body.custom_release_jql,
        "release_queries": _normalize_release_queries(body.release_queries),
        "release_comment": body.release_comment,
        "previous_release_comment": body.previous_release_comment,
        "next_release_comment": body.next_release_comment,
        "custom_release_comment": body.custom_release_comment,
        "scope_sections": scope_sections,
        "plan_epic_key": normalize_plan_epic_key(body.plan_epic_key),
    }


def _scope_fetch_warnings(outcomes: list[_ScopeJqlFetchResult]) -> list[dict[str, Any]]:
    return [
        {"jql": outcome.jql, "truncated": True, "count": len(outcome.issues)}
        for outcome in outcomes
        if outcome.jql and outcome.truncated
    ]


def _scope_sections_from_board(board: dict[str, Any]) -> list[dict[str, Any]]:
    return normalize_scope_sections(
        board.get("scope_sections"),
        plan_jql=str(board.get("plan_jql") or ""),
        unplan_jql=str(board.get("unplan_jql") or ""),
    )


def _release_queries_from_board(board: dict[str, Any]) -> list[dict[str, str]]:
    return _normalize_release_queries(board.get("release_queries"))


async def _post_jira_issue_comment(issue_key: str, text: str) -> dict[str, Any]:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.add_issue_comment(issue_key, text)
    finally:
        await client.close()


async def _put_jira_issue_due_date(issue_key: str, due_date: str) -> bool:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.update_due_date(issue_key, due_date)
    finally:
        await client.close()


async def _put_jira_issue_significance(issue_key: str, significance: int) -> bool:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.update_significance(issue_key, significance)
    finally:
        await client.close()


async def _clear_jira_issue_significance(issue_key: str) -> bool:
    from app.adapters.jira_service_client import JiraServiceHttpClient

    client = JiraServiceHttpClient()
    try:
        return await client.clear_significance(issue_key)
    finally:
        await client.close()


async def _sync_queue_significance_to_jira(order: list[str], *, moved_key: Optional[str] = None) -> list[str]:
    """Best-effort Jira sync; returns issue keys that could not be updated."""
    positions = queue_significance_positions(order)
    failures: list[str] = []
    for issue_key, significance in positions.items():
        try:
            saved = await _put_jira_issue_significance(issue_key, significance)
        except Exception as exc:
            logger.warning(
                "scope queue significance Jira update failed key=%s significance=%s error=%s",
                issue_key,
                significance,
                exc,
            )
            failures.append(issue_key)
            continue
        if not saved:
            logger.warning(
                "scope queue significance Jira update rejected key=%s significance=%s",
                issue_key,
                significance,
            )
            failures.append(issue_key)
    if failures:
        logger.warning(
            "scope queue significance partial sync moved_key=%s failed_count=%s failed_keys=%s",
            moved_key,
            len(failures),
            failures[:10],
        )
    return failures


async def _clear_queue_significance_in_jira(issue_keys: list[str]) -> list[str]:
    failures: list[str] = []
    for issue_key in issue_keys:
        try:
            cleared = await _clear_jira_issue_significance(issue_key)
        except Exception as exc:
            logger.warning(
                "scope queue significance Jira clear failed key=%s error=%s",
                issue_key,
                exc,
            )
            failures.append(issue_key)
            continue
        if not cleared:
            logger.warning("scope queue significance Jira clear rejected key=%s", issue_key)
            failures.append(issue_key)
    return failures


def _scope_board_mutation_response(board: dict[str, Any], *, snapshot_keys: list[str]) -> dict[str, Any]:
    """Return a lightweight board payload for snapshot mutations (client merges via snapshot_partial)."""
    if not board:
        return board
    snapshot = board.get("snapshot") or {}
    patch = {key: snapshot[key] for key in snapshot_keys if key in snapshot}
    return {
        **board,
        "snapshot": patch,
        "snapshot_partial": True,
        "ai_summary": None,
        "ai_summary_history": [],
    }


def _scope_board_metadata_response(board: dict[str, Any]) -> dict[str, Any]:
    """Return board metadata without snapshot/AI payloads."""
    if not board:
        return board
    return {
        **board,
        "snapshot": None,
        "snapshot_partial": True,
        "ai_summary": None,
        "ai_summary_history": [],
    }


def _snapshot_shallow_copy(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    return dict(snapshot or {})


def _scope_snapshot_has_issue(snapshot: dict[str, Any], issue_key: str) -> bool:
    target = issue_key.upper()
    for section in snapshot.get("sections") or []:
        for issue in section.get("issues") or []:
            if str(issue.get("key") or "").upper() == target:
                return True
    for section in ("plan_issues", "unplan_issues"):
        for issue in snapshot.get(section) or []:
            if str(issue.get("key") or "").upper() == target:
                return True
    return False


def _scope_snapshot_with_due_date(snapshot: dict[str, Any], *, issue_key: str, due_date: str) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    target = issue_key.upper()

    for section in updated.get("sections") or []:
        for issue in section.get("issues") or []:
            if str(issue.get("key") or "").upper() == target:
                issue["due_date"] = due_date

    for section_name in ("plan_issues", "unplan_issues"):
        for issue in updated.get(section_name) or []:
            if str(issue.get("key") or "").upper() == target:
                issue["due_date"] = due_date

    queues = dict(updated.get("priority_queues") or {})
    for queue_name, queue in queues.items():
        next_queue = dict(queue or {})
        next_issues = []
        for issue in next_queue.get("issues") or []:
            next_issue = dict(issue)
            if str(next_issue.get("key") or "").upper() == target:
                next_issue["due_date"] = due_date
            next_issues.append(next_issue)
        next_queue["issues"] = next_issues
        queues[queue_name] = next_queue
    updated["priority_queues"] = queues
    return updated


def _scope_snapshot_has_queue_issue(snapshot: dict[str, Any], issue_key: str, queue_kind: str) -> bool:
    queues = snapshot.get("priority_queues") or {}
    queue = queues.get(queue_kind) or {}
    target = issue_key.upper()
    for issue in queue.get("issues") or []:
        if str(issue.get("key") or "").upper() == target:
            return True
    return False


def _scope_snapshot_with_comment(
    snapshot: dict[str, Any],
    *,
    issue_key: str,
    text: str,
    actor_name: str,
    commented_at: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(snapshot)
    target = issue_key.upper()
    for section_name in ("sections",):
        for section in updated.get(section_name) or []:
            for issue in section.get("issues") or []:
                if str(issue.get("key") or "").upper() != target:
                    continue
                issue["last_comment"] = text
                issue["last_comment_author"] = actor_name
                issue["last_comment_at"] = commented_at
    for section in ("plan_issues", "unplan_issues"):
        for issue in updated.get(section) or []:
            if str(issue.get("key") or "").upper() != target:
                continue
            issue["last_comment"] = text
            issue["last_comment_author"] = actor_name
            issue["last_comment_at"] = commented_at
    sections = updated.get("sections") or []
    if sections:
        updated["report"] = compute_scope_report_from_sections(sections)
    else:
        updated["report"] = compute_scope_report(
            updated.get("plan_issues") or [],
            updated.get("unplan_issues") or [],
        )
    return updated


def _scope_report_comments(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = snapshot.get("report_comments")
    if not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def _scope_snapshot_has_report_issue(snapshot: dict[str, Any], issue_key: str) -> bool:
    if _scope_snapshot_has_issue(snapshot, issue_key):
        return True
    target = issue_key.upper()
    report = snapshot.get("report") or {}
    report_sections: list[dict[str, Any]] = list(report.get("sections") or [])
    for legacy in (report.get("plan"), report.get("unplan")):
        if isinstance(legacy, dict):
            report_sections.append(legacy)
    for section in report_sections:
        for column in ("in_work", "in_test", "done"):
            for issue in section.get(column) or []:
                if str(issue.get("key") or "").upper() == target:
                    return True
    release_context = snapshot.get("release_context") or {}
    buckets: list[dict[str, Any]] = []
    for slot in ("current", "previous", "next", "custom"):
        bucket = release_context.get(slot)
        if isinstance(bucket, dict):
            buckets.append(bucket)
    buckets.extend([bucket for bucket in release_context.get("releases") or [] if isinstance(bucket, dict)])
    for bucket in buckets:
        for column in ("in_work", "in_test", "done", "issues", "open_questions"):
            for issue in bucket.get(column) or []:
                if str(issue.get("key") or "").upper() == target:
                    return True
    return False


def _scope_snapshot_with_report_comment(
    snapshot: dict[str, Any],
    *,
    issue_key: str,
    text: str,
    actor_name: str,
    commented_at: str,
) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    comments = _scope_report_comments(updated)
    target = issue_key.upper()
    canonical_key = next((key for key in comments if key.upper() == target), issue_key.upper())
    cleaned = text.strip()
    if cleaned:
        comments[canonical_key] = {"text": cleaned, "by": actor_name, "at": commented_at}
    else:
        comments.pop(canonical_key, None)
        for key in list(comments):
            if key.upper() == target:
                comments.pop(key, None)
    updated["report_comments"] = comments
    return updated


def _grooming_jira_comment(queue_label: str, comment: str, *, moved_from: Optional[int] = None, moved_to: Optional[int] = None) -> str:
    prefix = f"[Scope grooming — {queue_label}]"
    if moved_from is not None and moved_to is not None:
        return f"{prefix} Позиция {moved_from + 1} → {moved_to + 1}: {comment}"
    return f"{prefix} {comment}"


def _scope_question_id(value: str) -> str:
    return value.strip()


def _scope_snapshot_with_manual_question(
    snapshot: dict[str, Any],
    *,
    text: str,
    actor_name: str,
    created_at: str,
    release_name: str = "",
) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    manual = list(updated.get("manual_questions") or [])
    question_id = f"manual-{secrets.token_hex(6)}"
    if release_name.strip():
        manual.append(
            manual_question_with_release_meta(
                text=text,
                actor_name=actor_name,
                question_id=question_id,
                release_name=release_name,
                created_at=created_at,
            )
        )
    else:
        manual.append(
            {
                "id": question_id,
                "summary": text,
                "created_by": actor_name,
                "created_at": created_at,
            }
        )
    updated["manual_questions"] = manual
    return updated


def _scope_snapshot_with_top_item(
    snapshot: dict[str, Any],
    *,
    text: str,
    actor_name: str,
    created_at: str,
) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    top_items = list(updated.get("top_items") or [])
    if len(top_items) >= 10:
        raise HTTPException(status_code=400, detail="Можно добавить не более 10 пунктов")
    top_items.append(
        {
            "id": f"top-{secrets.token_hex(6)}",
            "text": text,
            "created_by": actor_name,
            "created_at": created_at,
        }
    )
    updated["top_items"] = top_items
    return updated


def _scope_snapshot_without_top_item(snapshot: dict[str, Any], *, item_id: str) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    target = item_id.strip()
    top_items = [
        item
        for item in (updated.get("top_items") or [])
        if str(item.get("id") or "") != target
    ]
    if len(top_items) == len(updated.get("top_items") or []):
        raise HTTPException(status_code=404, detail="Top item not found in scope board snapshot")
    updated["top_items"] = top_items
    return updated


def _scope_snapshot_with_todo_item(
    snapshot: dict[str, Any],
    *,
    text: str,
    actor_name: str,
    created_at: str,
) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    todo_items = list(updated.get("todo_items") or [])
    if len(todo_items) >= 100:
        raise HTTPException(status_code=400, detail="Можно добавить не более 100 todo")
    todo_items.insert(
        0,
        {
            "id": f"todo-{secrets.token_hex(6)}",
            "text": text,
            "done": False,
            "created_by": actor_name,
            "created_at": created_at,
        },
    )
    updated["todo_items"] = todo_items
    return updated


def _scope_snapshot_with_todo_done(
    snapshot: dict[str, Any],
    *,
    item_id: str,
    done: bool,
    actor_name: str,
    changed_at: str,
) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    target = item_id.strip()
    changed = False
    todo_items: list[dict[str, Any]] = []
    for item in updated.get("todo_items") or []:
        if str(item.get("id") or "") != target:
            todo_items.append(item)
            continue
        next_item = {**item, "done": done}
        if done:
            next_item["done_by"] = actor_name
            next_item["done_at"] = changed_at
        else:
            next_item.pop("done_by", None)
            next_item.pop("done_at", None)
        todo_items.append(next_item)
        changed = True
    if not changed:
        raise HTTPException(status_code=404, detail="Todo item not found in scope board snapshot")
    updated["todo_items"] = todo_items
    return updated


def _scope_snapshot_without_todo_item(snapshot: dict[str, Any], *, item_id: str) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    target = item_id.strip()
    todo_items = [
        item
        for item in (updated.get("todo_items") or [])
        if str(item.get("id") or "") != target
    ]
    if len(todo_items) == len(updated.get("todo_items") or []):
        raise HTTPException(status_code=404, detail="Todo item not found in scope board snapshot")
    updated["todo_items"] = todo_items
    return updated


def _scope_snapshot_with_resolved_question(
    snapshot: dict[str, Any],
    *,
    question_id: str,
    comment: str,
    actor_name: str,
    resolved_at: str,
    release_name: str = "",
) -> dict[str, Any]:
    updated = _snapshot_shallow_copy(snapshot)
    target = _scope_question_id(question_id)
    manual = []
    resolved_source: Optional[dict[str, Any]] = None
    question_meta = updated.get("question_meta") or {}

    for question in updated.get("manual_questions") or []:
        if str(question.get("id") or "") == target:
            resolved_source = {**question, "kind": "manual"}
            continue
        manual.append(question)

    if resolved_source is None:
        for snapshot_section in updated.get("sections") or []:
            for issue in snapshot_section.get("issues") or []:
                if str(issue.get("key") or "").upper() != target.upper():
                    continue
                issue["last_comment"] = comment
                issue["last_comment_author"] = actor_name
                issue["last_comment_at"] = resolved_at
                resolved_source = {
                    "id": issue.get("key"),
                    "key": issue.get("key"),
                    "summary": issue.get("summary"),
                    "url": issue.get("url"),
                    "status": issue.get("status"),
                    "priority": issue.get("priority"),
                    "assignee": issue.get("assignee"),
                    "bucket": snapshot_section.get("id"),
                    "section_id": snapshot_section.get("id"),
                    "section_name": snapshot_section.get("name"),
                    "section_kind": snapshot_section.get("kind"),
                    "kind": "jira",
                }
                break
            if resolved_source is not None:
                break

    if resolved_source is None:
        for section in ("plan_issues", "unplan_issues"):
            for issue in updated.get(section) or []:
                if str(issue.get("key") or "").upper() != target.upper():
                    continue
                issue["last_comment"] = comment
                issue["last_comment_author"] = actor_name
                issue["last_comment_at"] = resolved_at
                resolved_source = {
                    "id": issue.get("key"),
                    "key": issue.get("key"),
                    "summary": issue.get("summary"),
                    "url": issue.get("url"),
                    "status": issue.get("status"),
                    "priority": issue.get("priority"),
                    "assignee": issue.get("assignee"),
                    "bucket": "plan" if section == "plan_issues" else "unplan",
                    "kind": "jira",
                }

    if resolved_source is None:
        raise HTTPException(status_code=404, detail="Question not found in scope board snapshot")

    tracked_meta = question_meta.get(target) if isinstance(question_meta, dict) else None
    if isinstance(tracked_meta, dict):
        resolved_source = {**resolved_source, "_tracked_meta": tracked_meta}

    updated["manual_questions"] = manual
    resolved = list(updated.get("resolved_questions") or [])
    if release_name.strip():
        resolved.append(
            resolved_question_with_release_meta(
                resolved_source,
                question_id=target,
                comment=comment,
                actor_name=actor_name,
                release_name=release_name,
                resolved_at=resolved_at,
            )
        )
    else:
        resolved.append(
            {
                **{key: value for key, value in resolved_source.items() if key != "_tracked_meta"},
                "id": target,
                "comment": comment,
                "resolved_by": actor_name,
                "resolved_at": resolved_at,
            }
        )
    if isinstance(question_meta, dict) and target in question_meta:
        next_meta = dict(question_meta)
        next_meta.pop(target, None)
        updated["question_meta"] = next_meta
    updated["resolved_questions"] = sorted(resolved, key=lambda item: str(item.get("resolved_at") or ""), reverse=True)[
        :100
    ]
    sections = updated.get("sections") or []
    if sections:
        updated["report"] = compute_scope_report_from_sections(sections)
    else:
        updated["report"] = compute_scope_report(
            updated.get("plan_issues") or [],
            updated.get("unplan_issues") or [],
        )
    return updated


# ---------------------------------------------------------------------------
# Monthly scope boards (plan / unplan buffer dashboard).
# ---------------------------------------------------------------------------


@router.get("/cms/scope-boards")
async def cms_list_scope_boards(
    request: Request,
    team_id: Optional[int] = None,
    sort: Optional[str] = Query(default=None, pattern="^(team_then_updated)?$"),
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    scope = team_scope(actor)
    items = await _get_cms_store(request).list_scope_boards(
        team_id=team_id,
        sort_team=sort == "team_then_updated" and actor.is_superuser,
        **scope,
    )
    return {"items": items}


@router.post("/cms/scope-boards")
async def cms_create_scope_board(
    body: ScopeBoardCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    if resolved_team_id is None:
        raise HTTPException(
            status_code=400,
            detail="Выберите команду — отчёт без команды видят все админы",
        )
    store = _get_cms_store(request)
    report_type = await _resolve_scope_report_type(store, resolved_team_id)
    payload = _scope_board_payload_from_request(body, report_type=report_type)
    board = await store.create_scope_board(
        created_by=actor.id,
        team_id=resolved_team_id,
        **payload,
    )
    board = await _apply_release_team_questions(store, board)
    if _is_release_scope_team(board) and isinstance(board.get("snapshot"), dict):
        board = await store.save_scope_board_snapshot(int(board["id"]), board["snapshot"]) or board
    await _audit(
        request,
        "cms.scope_board.create",
        actor.username,
        "ok",
        {"board_id": board["id"], "name": board["name"]},
    )
    return await _apply_release_team_questions(store, board)


@router.get("/cms/scope-boards/{board_id}")
async def cms_get_scope_board(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    board = await store.get_scope_board(board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, board)
    return await _apply_release_team_questions(store, board)


@router.get("/cms/scope-boards/{board_id}/ai-summary/jira-export")
async def cms_get_scope_board_ai_jira_export(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)
    payload = await _get_cms_store(request).get_scope_board_ai_jira_export(board_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Scope board not found")
    return payload


@router.patch("/cms/scope-boards/{board_id}/layout")
@router.patch("/cms/scope-boards/{board_id}/layout/")
async def cms_update_scope_board_layout(
    board_id: int,
    body: ScopeBoardLayoutRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    normalized = _normalize_scope_layout_order(body.layout_order)
    try:
        board = await _get_cms_store(request).update_scope_board_layout(board_id, normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.layout_update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return _scope_board_metadata_response(board)


@router.patch("/cms/scope-boards/{board_id}/flow-pace-chart-order")
@router.patch("/cms/scope-boards/{board_id}/flow-pace-chart-order/")
async def cms_update_scope_board_flow_pace_chart_order(
    board_id: int,
    body: ScopeBoardFlowPaceChartOrderRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    normalized = _normalize_flow_pace_chart_order(body.chart_order)
    try:
        board = await _get_cms_store(request).update_scope_board_flow_pace_chart_order(board_id, normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.flow_pace_chart_order_update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return _scope_board_metadata_response(board)


@router.patch("/cms/scope-boards/{board_id}/release-comments")
@router.patch("/cms/scope-boards/{board_id}/release-comments/")
async def cms_update_scope_board_release_comments(
    board_id: int,
    body: ScopeBoardReleaseCommentsRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    board = await _get_cms_store(request).update_scope_board_release_comments(
        board_id,
        release_comment=body.release_comment,
        previous_release_comment=body.previous_release_comment,
        next_release_comment=body.next_release_comment,
        custom_release_comment=body.custom_release_comment,
    )
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.release_comments_update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return _scope_board_metadata_response(board)


@router.patch("/cms/scope-boards/{board_id}")
async def cms_update_scope_board(
    board_id: int,
    body: ScopeBoardUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)
    report_type = str(existing.get("report_type") or "monthly")
    payload = _scope_board_payload_from_request(body, report_type=report_type)
    board = await _get_cms_store(request).update_scope_board(
        board_id,
        **payload,
    )
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    snapshot = board.get("snapshot")
    if snapshot:
        refreshed_snapshot = refresh_scope_snapshot_metrics(
            snapshot,
            capacity_sp=board["capacity_sp"],
            month=board["month"],
            workload_mode=board.get("workload_mode"),
            capacity_sp_dev=board.get("capacity_sp_dev"),
            capacity_sp_test=board.get("capacity_sp_test"),
        )
        board = await _get_cms_store(request).save_scope_board_snapshot(board_id, refreshed_snapshot)
        if not board:
            raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.update",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return board


@router.post("/cms/scope-boards/{board_id}/refresh")
async def cms_refresh_scope_board(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:scope_refresh:actor:{actor.username}",
        limit=int(os.getenv("SCOPE_REFRESH_RATE_MAX", "30")),
        window_seconds=int(os.getenv("SCOPE_REFRESH_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Слишком много обновлений из Jira — попробуйте позже",
    )
    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:scope_refresh:board:{board_id}",
        limit=int(os.getenv("SCOPE_REFRESH_BOARD_RATE_MAX", "12")),
        window_seconds=int(os.getenv("SCOPE_REFRESH_BOARD_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Этот отчёт уже часто обновляли — подождите немного",
    )

    from app.adapters.jira_service_client import JiraServiceHttpClient

    previous_snapshot = existing.get("snapshot") or {}
    previous_issue_count = _count_snapshot_issues(previous_snapshot)
    scope_sections = _scope_sections_from_board(existing)
    refreshed_at = datetime.now(timezone.utc).isoformat()
    fetch_outcomes: list[_ScopeJqlFetchResult] = []
    release_outcomes: dict[str, _ScopeJqlFetchResult] = {}
    release_version_meta_map: dict[str, dict[str, Any]] = {}

    client = JiraServiceHttpClient()
    try:
        fetched_sections, section_outcomes = await _fetch_scope_sections(
            scope_sections,
            client,
            force_refresh=True,
        )
        fetch_outcomes.extend(section_outcomes)

        todo_outcome = _ScopeJqlFetchResult(jql="", issues=[])
        test_outcome = _ScopeJqlFetchResult(jql="", issues=[])
        queue_tasks: list[Any] = []
        if (existing.get("todo_jql") or "").strip():
            queue_tasks.append(
                _fetch_scope_issues(
                    existing.get("todo_jql") or "",
                    client,
                    force_refresh=True,
                    milestone_status_targets=priority_queue_milestone_targets("todo"),
                    enrich_changelog=False,
                )
            )
        if (existing.get("test_jql") or "").strip():
            queue_tasks.append(
                _fetch_scope_issues(
                    existing.get("test_jql") or "",
                    client,
                    force_refresh=True,
                    milestone_status_targets=priority_queue_milestone_targets("test"),
                    enrich_changelog=False,
                )
            )
        release_outcomes = {}
        release_queries = _release_queries_from_board(existing) if existing.get("report_type") == "release" else []
        if existing.get("report_type") == "release":
            release_tasks = {
                query["id"]: _fetch_scope_issues(query["jql"], client, force_refresh=True, enrich_changelog=True)
                for query in release_queries
                if (query.get("jql") or "").strip()
            }
            if release_tasks:
                release_results = await asyncio.gather(*release_tasks.values())
                for (key, _), outcome in zip(release_tasks.items(), release_results, strict=True):
                    release_outcomes[key] = outcome
                    fetch_outcomes.append(outcome)
        if existing.get("report_type") == "release":
            version_jql_by_slot = {
                "current": _primary_release_jql(scope_sections),
            }
            version_jql_by_slot.update({query["id"]: query["jql"] for query in release_queries})
            issues_by_slot = {
                "current": (fetched_sections[0].get("issues") if fetched_sections else []) or [],
            }
            issues_by_slot.update({slot: outcome.issues for slot, outcome in release_outcomes.items()})
            release_version_meta_map = await _fetch_release_version_meta_map(
                client,
                version_jql_by_slot,
                issues_by_slot,
            )
        if queue_tasks:
            queue_results = await asyncio.gather(*queue_tasks)
            index = 0
            if (existing.get("todo_jql") or "").strip():
                todo_outcome = queue_results[index]
                fetch_outcomes.append(todo_outcome)
                index += 1
            if (existing.get("test_jql") or "").strip():
                test_outcome = queue_results[index]
                fetch_outcomes.append(test_outcome)
    finally:
        await client.close()

    configured_outcomes = [outcome for outcome in fetch_outcomes if outcome.jql]
    if configured_outcomes and all(outcome.failed for outcome in configured_outcomes):
        raise HTTPException(
            status_code=503,
            detail="Jira недоступна — snapshot не изменён",
        )
    if previous_issue_count > 0 and any(outcome.failed for outcome in configured_outcomes):
        raise HTTPException(
            status_code=503,
            detail="Часть JQL не загрузилась из Jira — snapshot не изменён",
        )

    todo_issues = todo_outcome.issues
    test_issues = test_outcome.issues

    for section in fetched_sections:
        for issue in section.get("issues") or []:
            issue["scope_creep"] = is_scope_creep(str(issue.get("created") or "") or None, existing["month"])
    metrics = compute_scope_metrics_from_sections(
        existing["capacity_sp"],
        fetched_sections,
        existing["month"],
        workload_mode=existing.get("workload_mode"),
        capacity_sp_dev=existing.get("capacity_sp_dev"),
        capacity_sp_test=existing.get("capacity_sp_test"),
    )
    snapshot = build_scope_snapshot(
        sections=fetched_sections,
        metrics=metrics,
        refreshed_at=refreshed_at,
        previous_snapshot=previous_snapshot,
    )
    snapshot["jira_role_fields_configured"] = merge_jira_role_fields_configured(
        snapshot.get("jira_role_fields_configured"),
        *[outcome.jira_role_fields_configured for outcome in fetch_outcomes],
    )
    if _is_release_scope_team(existing) and existing.get("team_id"):
        release_store = _get_cms_store(request)
        team_questions = await _ensure_team_scope_questions(release_store, int(existing["team_id"]))
        team_questions = union_team_scope_questions(
            team_questions,
            extract_team_scope_questions_from_snapshot(previous_snapshot),
        )
        open_ids = snapshot_open_jira_question_ids(snapshot)
        team_questions = register_open_jira_questions(
            team_questions,
            question_ids=open_ids,
            release_name=_board_release_label(existing, snapshot),
            registered_at=refreshed_at,
        )
        await release_store.save_team_scope_questions(int(existing["team_id"]), team_questions)
        snapshot = merge_team_scope_questions_into_snapshot(snapshot, team_questions, open_jira_ids=open_ids)
    else:
        snapshot["manual_questions"] = previous_snapshot.get("manual_questions") or []
        snapshot["resolved_questions"] = previous_snapshot.get("resolved_questions") or []
    snapshot["top_items"] = previous_snapshot.get("top_items") or []
    snapshot["todo_items"] = previous_snapshot.get("todo_items") or []
    snapshot["report_comments"] = previous_snapshot.get("report_comments") or {}
    snapshot["jira_fetch_warnings"] = _scope_fetch_warnings(fetch_outcomes)
    prev_queues = previous_snapshot.get("priority_queues") or {}
    snapshot["priority_queues"] = {
        "todo": merge_priority_queue(
            todo_issues,
            prev_queues.get("todo"),
            queue_label=priority_queue_label("todo"),
            refreshed_at=refreshed_at,
        ),
        "test": merge_priority_queue(
            test_issues,
            prev_queues.get("test"),
            queue_label=priority_queue_label("test"),
            refreshed_at=refreshed_at,
        ),
    }
    if existing.get("report_type") == "release":
        current_issues = (fetched_sections[0].get("issues") if fetched_sections else []) or []
        snapshot["release_context"] = build_release_context(
            current_jql=_primary_release_jql(scope_sections),
            current_issues=current_issues,
            previous_jql=existing.get("previous_release_jql") or "",
            previous_issues=(release_outcomes.get("previous").issues if release_outcomes.get("previous") else []),
            next_jql=existing.get("next_release_jql") or "",
            next_issues=(release_outcomes.get("next").issues if release_outcomes.get("next") else []),
            custom_name=existing.get("custom_release_name") or "",
            custom_jql=existing.get("custom_release_jql") or "",
            custom_issues=(release_outcomes.get("custom").issues if release_outcomes.get("custom") else []),
            release_queries=release_queries,
            release_issues_by_slot={slot: outcome.issues for slot, outcome in release_outcomes.items()},
            version_meta_by_slot=release_version_meta_map,
        )
    team_slug = (existing.get("team") or {}).get("slug")
    if not team_slug:
        board_name = str(existing.get("name") or "").lower()
        if "igaming rip" in board_name:
            team_slug = "igaming-rip"
    flow_pace = compute_scope_flow_pace(snapshot, team_slug=team_slug)
    if flow_pace is not None:
        flow_pace = apply_flow_pace_chart_order(flow_pace, existing.get("flow_pace_chart_order"))
        snapshot["flow_pace"] = flow_pace
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.refresh",
        actor.username,
        "ok",
        {"board_id": board_id, "intake_status": metrics["intake_status"]},
    )
    return board


@router.post("/cms/scope-boards/{board_id}/issues/{issue_key}/comment")
async def cms_add_scope_issue_comment(
    board_id: int,
    issue_key: str,
    body: ScopeIssueCommentRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_issue(snapshot, issue_key):
        raise HTTPException(status_code=404, detail="Issue not found in scope board snapshot")

    cleaned_text = body.text.strip()
    if not cleaned_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    commented_at = datetime.now(timezone.utc).isoformat()
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_comment(
        snapshot,
        issue_key=issue_key,
        text=cleaned_text,
        actor_name=actor_name,
        commented_at=commented_at,
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    try:
        await _post_jira_issue_comment(issue_key, cleaned_text)
    except Exception as exc:
        logger.warning("scope issue comment saved locally but Jira failed key=%s error=%s", issue_key, exc)
        raise HTTPException(
            status_code=502,
            detail="Snapshot сохранён, но комментарий в Jira не отправлен",
        ) from exc
    await _audit(
        request,
        "cms.scope_board.issue_comment",
        actor.username,
        "ok",
        {"board_id": board_id, "issue_key": issue_key},
    )
    return _scope_board_mutation_response(
        board,
        snapshot_keys=["sections", "plan_issues", "unplan_issues", "report"],
    )


@router.put("/cms/scope-boards/{board_id}/issues/{issue_key}/report-comment")
async def cms_update_scope_report_comment(
    board_id: int,
    issue_key: str,
    body: ScopeReportCommentRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_report_issue(snapshot, issue_key):
        raise HTTPException(status_code=404, detail="Issue not found in scope board report")

    commented_at = datetime.now(timezone.utc).isoformat()
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_report_comment(
        snapshot,
        issue_key=issue_key,
        text=body.text,
        actor_name=actor_name,
        commented_at=commented_at,
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.report_comment",
        actor.username,
        "ok",
        {"board_id": board_id, "issue_key": issue_key},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["report_comments"])


@router.post("/cms/scope-boards/{board_id}/questions")
async def cms_add_scope_manual_question(
    board_id: int,
    body: ScopeManualQuestionRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_scope_board_for_mutation(_get_cms_store(request), board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    actor_name = actor.display_name or actor.username
    created_at = datetime.now(timezone.utc).isoformat()
    release_name = _board_release_label(existing, snapshot) if _is_release_scope_team(existing) else ""
    next_snapshot = _scope_snapshot_with_manual_question(
        snapshot,
        text=body.text.strip(),
        actor_name=actor_name,
        created_at=created_at,
        release_name=release_name,
    )
    store = _get_cms_store(request)
    board = await store.save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _sync_release_team_questions(store, existing, next_snapshot)
    await _audit(
        request,
        "cms.scope_board.question_create",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["manual_questions"])


@router.post("/cms/scope-boards/{board_id}/top-items")
async def cms_add_scope_top_item(
    board_id: int,
    body: ScopeTopItemRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_top_item(
        snapshot,
        text=body.text.strip(),
        actor_name=actor_name,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.top_item_create",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["top_items"])


@router.delete("/cms/scope-boards/{board_id}/top-items/{item_id}")
async def cms_delete_scope_top_item(
    board_id: int,
    item_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    next_snapshot = _scope_snapshot_without_top_item(snapshot, item_id=item_id)
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.top_item_delete",
        actor.username,
        "ok",
        {"board_id": board_id, "item_id": item_id},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["top_items"])


@router.post("/cms/scope-boards/{board_id}/todo-items")
async def cms_add_scope_todo_item(
    board_id: int,
    body: ScopeTodoItemRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {
        "plan_issues": [],
        "unplan_issues": [],
        "metrics": {},
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_todo_item(
        snapshot,
        text=body.text.strip(),
        actor_name=actor_name,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.todo_create",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["todo_items"])


@router.patch("/cms/scope-boards/{board_id}/todo-items/{item_id}")
async def cms_update_scope_todo_item(
    board_id: int,
    item_id: str,
    body: ScopeTodoItemUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    actor_name = actor.display_name or actor.username
    next_snapshot = _scope_snapshot_with_todo_done(
        snapshot,
        item_id=item_id,
        done=body.done,
        actor_name=actor_name,
        changed_at=datetime.now(timezone.utc).isoformat(),
    )
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.todo_update",
        actor.username,
        "ok",
        {"board_id": board_id, "item_id": item_id, "done": body.done},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["todo_items"])


@router.delete("/cms/scope-boards/{board_id}/todo-items/{item_id}")
async def cms_delete_scope_todo_item(
    board_id: int,
    item_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    next_snapshot = _scope_snapshot_without_todo_item(snapshot, item_id=item_id)
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.todo_delete",
        actor.username,
        "ok",
        {"board_id": board_id, "item_id": item_id},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["todo_items"])


@router.post("/cms/scope-boards/{board_id}/questions/{question_id}/resolve")
async def cms_resolve_scope_question(
    board_id: int,
    question_id: str,
    body: ScopeResolveQuestionRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_scope_board_for_mutation(_get_cms_store(request), board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    cleaned_comment = body.comment.strip()
    if not cleaned_comment:
        raise HTTPException(status_code=400, detail="Comment text is required")

    if _scope_snapshot_has_issue(snapshot, question_id):
        await _post_jira_issue_comment(question_id, cleaned_comment)

    actor_name = actor.display_name or actor.username
    resolved_at = datetime.now(timezone.utc).isoformat()
    release_name = _board_release_label(existing, snapshot) if _is_release_scope_team(existing) else ""
    next_snapshot = _scope_snapshot_with_resolved_question(
        snapshot,
        question_id=question_id,
        comment=cleaned_comment,
        actor_name=actor_name,
        resolved_at=resolved_at,
        release_name=release_name,
    )
    store = _get_cms_store(request)
    board = await store.save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _sync_release_team_questions(store, existing, next_snapshot)
    await _audit(
        request,
        "cms.scope_board.question_resolve",
        actor.username,
        "ok",
        {"board_id": board_id, "question_id": question_id},
    )
    return _scope_board_mutation_response(
        board,
        snapshot_keys=["manual_questions", "resolved_questions", "report"],
    )


def _parse_priority_queue_kind(raw: str) -> str:
    kind = raw.strip().lower()
    if kind not in {"todo", "test"}:
        raise HTTPException(status_code=400, detail="Queue must be todo or test")
    return kind


@router.post("/cms/scope-boards/{board_id}/queues/{queue_kind}/reorder")
async def cms_reorder_scope_priority_queue(
    board_id: int,
    queue_kind: str,
    body: ScopeQueueReorderRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    queues = dict(snapshot.get("priority_queues") or {})
    current_queue = queues.get(kind) or {"ranked_order": [], "issues": [], "history": []}
    cleaned_comment = body.comment.strip()

    actor_name = actor.display_name or actor.username
    changed_at = datetime.now(timezone.utc).isoformat()
    queue_label = priority_queue_label(kind)  # type: ignore[arg-type]
    try:
        next_queue = apply_priority_queue_ranked_update(
            current_queue,
            ranked_order=body.order,
            comment=cleaned_comment,
            actor_name=actor_name,
            changed_at=changed_at,
            queue_label=queue_label,
            moved_key=body.moved_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    moved_key = None
    moved_from = None
    moved_to = None
    for entry in next_queue.get("history") or []:
        if entry.get("type") == "reorder" and entry.get("at") == changed_at:
            moved_key = entry.get("issue_key")
            moved_from = entry.get("from_index")
            moved_to = entry.get("to_index")
            break
    jira_comment = None
    if moved_key and cleaned_comment:
        jira_comment = _grooming_jira_comment(
            queue_label,
            cleaned_comment,
            moved_from=moved_from if isinstance(moved_from, int) else None,
            moved_to=moved_to if isinstance(moved_to, int) else None,
        )

    next_snapshot = _snapshot_shallow_copy(snapshot)
    next_queues = dict(next_snapshot.get("priority_queues") or {})
    next_queues[kind] = next_queue
    next_snapshot["priority_queues"] = next_queues
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    ranked_order = list(next_queue.get("ranked_order") or [])
    removed_from_ranked = list(next_queue.get("removed_from_ranked") or [])
    significance_failures = await _sync_queue_significance_to_jira(
        ranked_order,
        moved_key=str(moved_key) if moved_key else None,
    )
    if removed_from_ranked:
        clear_failures = await _clear_queue_significance_in_jira(removed_from_ranked)
        significance_failures = [*significance_failures, *clear_failures]
    if moved_key and jira_comment:
        try:
            await _post_jira_issue_comment(str(moved_key), jira_comment)
        except Exception as exc:
            logger.warning(
                "scope queue reorder saved locally but Jira failed key=%s error=%s",
                moved_key,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="Порядок сохранён, но комментарий в Jira не отправлен",
            ) from exc
    await _audit(
        request,
        "cms.scope_board.queue_reorder",
        actor.username,
        "ok",
        {
            "board_id": board_id,
            "queue": kind,
            "issue_key": moved_key,
            "significance_failures": significance_failures[:20],
        },
    )
    return _scope_board_mutation_response(board, snapshot_keys=["priority_queues"])


@router.post("/cms/scope-boards/{board_id}/queues/{queue_kind}/reset-ranked")
async def cms_reset_scope_priority_queue_ranked(
    board_id: int,
    queue_kind: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    queues = dict(snapshot.get("priority_queues") or {})
    current_queue = queues.get(kind) or {"ranked_order": [], "issues": [], "history": []}
    next_queue, keys_to_clear = clear_priority_queue_ranked(current_queue)

    next_snapshot = _snapshot_shallow_copy(snapshot)
    next_queues = dict(next_snapshot.get("priority_queues") or {})
    next_queues[kind] = next_queue
    next_snapshot["priority_queues"] = next_queues
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")

    significance_failures: list[str] = []
    if keys_to_clear:
        significance_failures = await _clear_queue_significance_in_jira(keys_to_clear)

    await _audit(
        request,
        "cms.scope_board.queue_reset_ranked",
        actor.username,
        "ok",
        {
            "board_id": board_id,
            "queue": kind,
            "cleared_count": len(keys_to_clear),
            "significance_failures": significance_failures[:20],
        },
    )
    return _scope_board_mutation_response(board, snapshot_keys=["priority_queues"])


@router.post("/cms/scope-boards/{board_id}/queues/{queue_kind}/issues/{issue_key}/comment")
async def cms_add_scope_queue_issue_comment(
    board_id: int,
    queue_kind: str,
    issue_key: str,
    body: ScopeIssueCommentRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_queue_issue(snapshot, issue_key, kind):
        raise HTTPException(status_code=404, detail="Issue not found in queue")

    cleaned_text = body.text.strip()
    if not cleaned_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    queue_label = priority_queue_label(kind)  # type: ignore[arg-type]
    actor_name = actor.display_name or actor.username
    changed_at = datetime.now(timezone.utc).isoformat()
    queues = dict(snapshot.get("priority_queues") or {})
    current_queue = queues.get(kind) or {"order": [], "issues": [], "history": []}
    try:
        next_queue = apply_priority_queue_comment(
            current_queue,
            issue_key=issue_key,
            comment=cleaned_text,
            actor_name=actor_name,
            changed_at=changed_at,
            queue_label=queue_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    next_snapshot = _snapshot_shallow_copy(snapshot)
    next_queues = dict(next_snapshot.get("priority_queues") or {})
    next_queues[kind] = next_queue
    next_snapshot["priority_queues"] = next_queues
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    try:
        await _post_jira_issue_comment(issue_key, _grooming_jira_comment(queue_label, cleaned_text))
    except Exception as exc:
        logger.warning("scope queue comment saved locally but Jira failed key=%s error=%s", issue_key, exc)
        raise HTTPException(
            status_code=502,
            detail="Комментарий сохранён в отчёте, но не отправлен в Jira",
        ) from exc
    await _audit(
        request,
        "cms.scope_board.queue_comment",
        actor.username,
        "ok",
        {"board_id": board_id, "queue": kind, "issue_key": issue_key},
    )
    return _scope_board_mutation_response(board, snapshot_keys=["priority_queues"])


@router.put("/cms/scope-boards/{board_id}/queues/{queue_kind}/issues/{issue_key}/due-date")
async def cms_update_scope_queue_issue_due_date(
    board_id: int,
    queue_kind: str,
    issue_key: str,
    body: ScopeIssueDueDateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    kind = _parse_priority_queue_kind(queue_kind)
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)

    snapshot = existing.get("snapshot") or {}
    if not _scope_snapshot_has_queue_issue(snapshot, issue_key, kind):
        raise HTTPException(status_code=404, detail="Issue not found in queue")

    try:
        datetime.strptime(body.due_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid due date") from exc

    try:
        saved = await _put_jira_issue_due_date(issue_key, body.due_date)
    except Exception as exc:
        logger.warning("scope queue due date Jira update failed key=%s error=%s", issue_key, exc)
        raise HTTPException(status_code=502, detail="Срок исполнения не сохранён в Jira") from exc
    if not saved:
        raise HTTPException(status_code=502, detail="Срок исполнения не сохранён в Jira")

    next_snapshot = _scope_snapshot_with_due_date(snapshot, issue_key=issue_key, due_date=body.due_date)
    board = await _get_cms_store(request).save_scope_board_snapshot(board_id, next_snapshot)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.queue_due_date_update",
        actor.username,
        "ok",
        {"board_id": board_id, "queue": kind, "issue_key": issue_key, "due_date": body.due_date},
    )
    return _scope_board_mutation_response(
        board,
        snapshot_keys=["priority_queues", "sections", "plan_issues", "unplan_issues"],
    )


@router.post("/cms/scope-boards/{board_id}/analyze")
async def cms_analyze_scope_board(
    board_id: int,
    request: Request,
    async_mode: bool = Query(False, alias="async"),
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    store = _get_cms_store(request)
    board = await store.get_scope_board(board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, board)
    snapshot = board.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=400, detail="Нет snapshot — сначала обновите board из Jira")

    await enforce_rate_limit(
        await _get_redis(request),
        key=f"rl:scope_ai:actor:{actor.username}",
        limit=int(os.getenv("SCOPE_AI_RATE_MAX", "20")),
        window_seconds=int(os.getenv("SCOPE_AI_RATE_WINDOW_SECONDS", "3600")),
        error_detail="Слишком много AI-запросов, попробуйте позже",
    )

    from services.voting_service.ai_job_runners import run_scope_ai_job
    from services.voting_service.ai_jobs import find_cached_scope_summary, get_job, get_or_create_job, job_public_view, spawn_ai_job
    from services.voting_service.scope_ai_llm import LlmScopeError, generate_scope_analysis

    snapshot_refreshed_at = snapshot.get("refreshed_at") if isinstance(snapshot, dict) else None
    cached = find_cached_scope_summary(board, snapshot_refreshed_at)
    if cached:
        from services.voting_service.ai_job_runners import spawn_scope_ai_jira_export

        spawn_scope_ai_jira_export(
            request.app,
            board_id=board_id,
            board=board,
            summary=dict(cached),
            actor_username=actor.username,
        )
        await _audit(
            request,
            "cms.scope_board.analyze",
            actor.username,
            "ok",
            {"board_id": board_id, "health": cached.get("health"), "cached": True},
        )
        return {"ai_summary": cached, "board": board, "cached": True}

    if async_mode:
        redis = await _get_redis(request)
        job_id, is_new = await get_or_create_job(
            redis,
            kind="scope",
            resource_key=f"board:{board_id}",
            actor=actor.username,
        )
        if is_new:
            spawn_ai_job(run_scope_ai_job(request.app, job_id=job_id, board_id=board_id, actor_username=actor.username))
        job_record = await get_job(redis, job_id)
        return job_public_view(job_record or {"job_id": job_id, "status": "queued", "phase": "queued", "message": "В очереди"})

    http_session = getattr(request.app.state, "http_session", None)
    if http_session is None:
        raise HTTPException(status_code=503, detail="AI is not configured")
    try:
        summary = await generate_scope_analysis(http_session, board)
    except LlmScopeError as exc:
        await _audit(
            request,
            "cms.scope_board.analyze",
            actor.username,
            "error",
            {"board_id": board_id, "error": exc.message},
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    updated = await store.save_scope_board_ai_summary(
        board_id,
        summary,
        snapshot_refreshed_at=snapshot_refreshed_at,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Scope board not found")
    from services.voting_service.ai_job_runners import spawn_scope_ai_jira_export

    spawn_scope_ai_jira_export(
        request.app,
        board_id=board_id,
        board=updated,
        summary=dict(summary),
        actor_username=actor.username,
    )
    await _audit(
        request,
        "cms.scope_board.analyze",
        actor.username,
        "ok",
        {"board_id": board_id, "health": summary.get("health")},
    )
    return {"ai_summary": summary, "board": updated}


@router.get("/cms/scope-boards/{board_id}/analyze/jobs/{job_id}")
async def cms_scope_analyze_job_status(
    board_id: int,
    job_id: str,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    board = await _get_cms_store(request).get_scope_board(board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, board)

    from services.voting_service.ai_jobs import get_job_for_poll, job_public_view

    redis = await _get_redis(request)
    job = await get_job_for_poll(redis, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AI job not found")
    if job.get("kind") != "scope" or job.get("resource_key") != f"board:{board_id}":
        raise HTTPException(status_code=404, detail="AI job not found")
    return job_public_view(job)


@router.delete("/cms/scope-boards/{board_id}")
async def cms_delete_scope_board(
    board_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_PLANNER_VIEW)),
) -> dict:
    existing = await _get_cms_store(request).get_scope_board(board_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scope board not found")
    assert_record_access(actor, existing)
    deleted = await _get_cms_store(request).delete_scope_board(board_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scope board not found")
    await _audit(
        request,
        "cms.scope_board.delete",
        actor.username,
        "ok",
        {"board_id": board_id},
    )
    return {"ok": True, "id": board_id}

