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

@app_router.get("/app/sessions/{chat_id}/tasks")
async def app_session_tasks(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    return _task_page(session, limit, cursor, q)


@app_router.post("/app/sessions/{chat_id}/tasks")
async def app_create_task(
    chat_id: int,
    body: TaskCreateRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = AddManualTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            summary=body.summary,
            jira_key=body.jira_key,
            url=body.url,
            story_points=body.story_points,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "app.task.create", actor.username, "failed", {"error": str(exc), "chat_id": chat_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    await _audit(request, "app.task.create", actor.username, "ok", {"chat_id": chat_id, "task_id": result.task.task_id if result.task else None})
    return _mutation_payload(result, -1)


@app_router.patch("/app/sessions/{chat_id}/tasks/{task_id}")
async def app_update_task(
    chat_id: int,
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = UpdateTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            summary=body.summary,
            jira_key=body.jira_key,
            url=body.url,
            story_points=body.story_points,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "app.task.update", actor.username, "failed", {"error": str(exc), "chat_id": chat_id, "task_id": task_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    await _audit(request, "app.task.update", actor.username, "ok", {"chat_id": chat_id, "task_id": task_id})
    return _mutation_payload(result, -1)


@app_router.delete("/app/sessions/{chat_id}/tasks/{task_id}")
async def app_delete_task(
    chat_id: int,
    task_id: str,
    request: Request,
    topic_id: Optional[int] = None,
    expected_version: Optional[int] = Query(default=None, ge=0),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = DeleteTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(chat_id=chat_id, topic_id=topic_id, task_id=task_id, expected_version=expected_version)
    except TaskQueueError as exc:
        await _audit(request, "app.task.delete", actor.username, "failed", {"error": str(exc), "chat_id": chat_id, "task_id": task_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    await _audit(request, "app.task.delete", actor.username, "ok", {"chat_id": chat_id, "task_id": task_id})
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/tasks/{task_id}/move")
async def app_move_task(
    chat_id: int,
    task_id: str,
    body: TaskMoveRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = MoveTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            target_index=body.target_index,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "app.task.move", actor.username, "failed", {"error": str(exc), "chat_id": chat_id, "task_id": task_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/tasks/reorder")
async def app_reorder_tasks(
    chat_id: int,
    body: TaskReorderRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = ReorderTasksUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            ordered_task_ids=body.ordered_task_ids,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "app.task.reorder", actor.username, "failed", {"error": str(exc), "chat_id": chat_id})
        _raise_task_error(exc)
    await _publish_state(request, result.session)
    return _mutation_payload(result, -1)


@app_router.post("/app/sessions/{chat_id}/tasks/jira-preview")
async def app_preview_jira_tasks(
    chat_id: int,
    body: JiraPreviewRequest,
    request: Request,
    topic_id: Optional[int] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)
    return _jira_preview_payload(issues, _existing_jira_keys(session))


@app_router.get("/app/debug/jira-description/{issue_key}")
async def app_debug_jira_description(
    issue_key: str,
    request: Request,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Diagnostic endpoint: ask jira-service for the description of a key.

    Lets operators verify in one curl whether the issue actually carries
    a non-empty description, without spinning up an import. Returns the
    raw length and a short preview so it's also safe to share in chat.
    Bounded by manager auth — no anonymous use.
    """
    fetched = await _fetch_jira_description(request.app.state.http_session, issue_key)
    if fetched.is_empty:
        return {"key": issue_key.strip().upper(), "found": False}
    text = fetched.text or ""
    # ``format`` makes the source obvious in one curl: "html" is Jira
    # renderedFields (what the voter UI prefers), "adf" is structured
    # ADF, "plain" is the flat string fallback.
    fmt = "html" if fetched.html else ("adf" if fetched.adf else "plain")
    adf_root_type = fetched.adf.get("type") if isinstance(fetched.adf, dict) else None
    adf_first_block_types: list[str] = []
    if isinstance(fetched.adf, dict):
        for child in (fetched.adf.get("content") or [])[:5]:
            if isinstance(child, dict) and child.get("type"):
                adf_first_block_types.append(str(child.get("type")))
    return {
        "key": issue_key.strip().upper(),
        "found": True,
        "format": fmt,
        "length": len(text),
        "newline_count": text.count("\n"),
        "preview": text[:300],
        "has_adf": bool(fetched.adf),
        "has_html": bool(fetched.html),
        "html_length": len(fetched.html or ""),
        "adf_root_type": adf_root_type,
        "adf_first_block_types": adf_first_block_types,
    }


@app_router.post("/app/sessions/{chat_id}/tasks/jira-import")
async def app_import_jira_tasks(
    chat_id: int,
    body: JiraImportRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    selected = {key.strip().upper() for key in body.selected_keys if key.strip()}
    issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)

    # Pre-fetch the issue body for every selected key so we can store it on
    # the Task at import time. Voters then see the full Jira spec inline on
    # the vote page and the AI summary prompt has a cheap fallback when
    # the per-request context fetch fails. Each call is best-effort and
    # de-duped by the jira-service in-memory cache; failures resolve to
    # ``None`` and never block the import.
    keys_to_fetch = [
        str(issue.get("key") or "").strip().upper()
        for issue in issues
        if str(issue.get("key") or "").strip()
        and (not selected or str(issue.get("key") or "").strip().upper() in selected)
    ]
    fetched_payloads = (
        await asyncio.gather(
            *[
                _fetch_jira_description(request.app.state.http_session, key)
                for key in keys_to_fetch
            ]
        )
        if keys_to_fetch
        else []
    )
    descriptions = dict(zip(keys_to_fetch, fetched_payloads))
    # One summary line per import call: how many keys we tried, how
    # many got plain text, and how many got rich ADF. If both numbers
    # are 0 the ratio gives operators an immediate "Jira returns empty
    # bodies" signal without grepping per-key warnings.
    logger.info(
        "jira import description fetch chat=%s tried=%d filled_text=%d filled_adf=%d filled_html=%d",
        chat_id,
        len(keys_to_fetch),
        sum(1 for v in descriptions.values() if v.text),
        sum(1 for v in descriptions.values() if v.adf),
        sum(1 for v in descriptions.values() if v.html),
    )

    def mutate(session: Session) -> TaskMutationResult:
        if body.expected_version is not None and body.expected_version != session.tasks_version:
            raise TaskQueueError("Task queue was changed. Refresh and try again.", status_code=409)
        existing_keys = _existing_jira_keys(session)
        added: list[Task] = []
        seen: set[str] = set()
        for issue in issues:
            key = str(issue.get("key") or "").strip().upper()
            if not key or key in existing_keys or key in seen:
                continue
            if selected and key not in selected:
                continue
            fetched = descriptions.get(key)
            task = Task(
                jira_key=key,
                summary=issue.get("summary") or key,
                url=issue.get("url"),
                story_points=issue.get("story_points") or None,
                jql=body.jql,
                source="jira",
                description=fetched.text if fetched else None,
                description_adf=fetched.adf if fetched else None,
                description_html=fetched.html if fetched else None,
            )
            session.tasks_queue.append(task)
            added.append(task)
            seen.add(key)
            existing_keys.add(key)
        if not added:
            raise TaskQueueError("No Jira tasks to import")
        session.batch_completed = False
        session.bump_tasks_version()
        return TaskMutationResult(session=session, task=added[-1], tasks=tuple(added))

    try:
        session, result = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    except TaskQueueError as exc:
        await _audit(request, "app.task.jira_import", actor.username, "failed", {"error": str(exc), "chat_id": chat_id})
        _raise_task_error(exc)
    await _publish_state(request, session)
    await _audit(request, "app.task.jira_import", actor.username, "ok", {"chat_id": chat_id, "count": len(result.tasks)})
    return _mutation_payload(result, -1)

