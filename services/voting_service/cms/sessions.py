"""CMS session management endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.domain.session import Session
from app.domain.task import Task
from app.usecases.close_session import CloseSessionUseCase
from app.usecases.manage_tasks import (
    AddManualTaskUseCase,
    DeleteTaskUseCase,
    MoveTaskUseCase,
    ReorderTasksUseCase,
    TaskMutationResult,
    TaskQueueError,
    UpdateTaskUseCase,
)
from services.voting_service.cms_rbac import (
    PERM_APP_SESSIONS_MANAGE,
    PERM_SESSIONS_VIEW,
    PERM_TASKS_MANAGE,
)
from services.voting_service.cms_store import DEFAULT_LIMIT, MAX_LIMIT
from services.voting_service.cms_team_access import assert_record_access
from services.voting_service.session_finish_notify import maybe_notify_session_finished
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
    _get_cms_store,
    _get_repo_session,
    _jira_preview,
    _jira_preview_payload,
    _mutate_repo_session,
    _mutation_payload,
    _publish_state,
    _raise_task_error,
    require_permission,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SessionRenameRequest(BaseModel):
    """Rename a CMS session. Empty string clears the custom title and
    callers fall back to the technical identifier."""

    title: Optional[str] = Field(default=None, max_length=200)

async def _session_ref(
    request: Request,
    session_id: int,
    actor: CmsPrincipal,
) -> tuple[int, Optional[int]]:
    detail = await _get_cms_store(request).get_session(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    assert_record_access(actor, detail)
    return int(detail["chat_id"]), detail.get("topic_id")

@router.get("/cms/sessions")
async def cms_sessions(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    active: Optional[bool] = None,
    chat_id: Optional[int] = None,
    topic_id: Optional[int] = None,
    team_id: Optional[int] = None,
    sort: Optional[str] = Query(default=None, pattern="^(team_then_updated)?$"),
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    if team_id is not None and not actor.is_superuser:
        raise HTTPException(status_code=403, detail="Forbidden")
    scope = team_scope(actor)
    return await _get_cms_store(request).list_sessions(
        limit=limit,
        cursor=cursor,
        q=q,
        active=active,
        chat_id=chat_id,
        topic_id=topic_id,
        team_id=team_id,
        sort_team=sort == "team_then_updated" and actor.is_superuser,
        **scope,
    )


@router.get("/cms/sessions/{session_id}")
async def cms_session_detail(
    session_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    session = await _get_cms_store(request).get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    assert_record_access(actor, session)
    return session


@router.patch("/cms/sessions/{session_id}")
async def cms_rename_session(
    session_id: int,
    body: SessionRenameRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Rename a CMS session. Title is optional human-readable name shown in
    place of the technical ``chat_id:topic_id`` key. Sending ``null`` or an
    empty string clears the custom title."""
    store = _get_cms_store(request)
    existing = await store.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")
    assert_record_access(actor, existing)
    result = await store.rename_session(session_id, body.title)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")
    _, _, new_title = result
    await _audit(
        request,
        "cms.session.rename",
        actor.username,
        "ok",
        {"session_id": session_id, "title": new_title},
    )
    return {
        "ok": True,
        "session_id": session_id,
        "title": new_title,
    }


@router.get("/cms/sessions/{session_id}/participants")
async def cms_session_participants(
    session_id: int,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    await _session_ref(request, session_id, actor)
    return await _get_cms_store(request).list_session_participants(session_id, limit, cursor)


@router.get("/cms/sessions/{session_id}/tasks")
async def cms_session_tasks(
    session_id: int,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = None,
    bucket: Optional[str] = Query(None, pattern="^(tasks_queue|history|last_batch)$"),
    q: Optional[str] = None,
    actor: CmsPrincipal = Depends(require_permission(PERM_SESSIONS_VIEW)),
) -> dict:
    await _session_ref(request, session_id, actor)
    return await _get_cms_store(request).list_session_tasks(session_id, limit, cursor, bucket, q)


@router.post("/cms/sessions/{session_id}/tasks")
async def cms_create_session_task(
    session_id: int,
    body: TaskCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
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
        await _audit(request, "cms.task.create", actor.username, "failed", {"error": str(exc), "session_id": session_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.create", actor.username, "ok", {"session_id": session_id, "task_id": result.task.task_id if result.task else None})
    return _mutation_payload(result, session_id)


@router.patch("/cms/sessions/{session_id}/tasks/{task_id}")
async def cms_update_session_task(
    session_id: int,
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
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
        await _audit(request, "cms.task.update", actor.username, "failed", {"error": str(exc), "session_id": session_id, "task_id": task_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.update", actor.username, "ok", {"session_id": session_id, "task_id": task_id})
    return _mutation_payload(result, session_id)


@router.delete("/cms/sessions/{session_id}/tasks/{task_id}")
async def cms_delete_session_task(
    session_id: int,
    task_id: str,
    request: Request,
    expected_version: Optional[int] = Query(default=None, ge=0),
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = DeleteTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            expected_version=expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.delete", actor.username, "failed", {"error": str(exc), "session_id": session_id, "task_id": task_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.delete", actor.username, "ok", {"session_id": session_id, "task_id": task_id})
    return _mutation_payload(result, session_id)


@router.post("/cms/sessions/{session_id}/tasks/{task_id}/move")
async def cms_move_session_task(
    session_id: int,
    task_id: str,
    body: TaskMoveRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
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
        await _audit(request, "cms.task.move", actor.username, "failed", {"error": str(exc), "session_id": session_id, "task_id": task_id})
        _raise_task_error(exc)
    await _audit(
        request,
        "cms.task.move",
        actor.username,
        "ok",
        {"session_id": session_id, "task_id": task_id, "target_index": body.target_index},
    )
    return _mutation_payload(result, session_id)


@router.post("/cms/sessions/{session_id}/tasks/reorder")
async def cms_reorder_session_tasks(
    session_id: int,
    body: TaskReorderRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    use_case = ReorderTasksUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            ordered_task_ids=body.ordered_task_ids,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(request, "cms.task.reorder", actor.username, "failed", {"error": str(exc), "session_id": session_id})
        _raise_task_error(exc)
    await _audit(request, "cms.task.reorder", actor.username, "ok", {"session_id": session_id, "count": len(body.ordered_task_ids)})
    return _mutation_payload(result, session_id)


@router.post("/cms/sessions/{session_id}/tasks/jira-preview")
async def cms_preview_jira_tasks(
    session_id: int,
    body: JiraPreviewRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)
    return _jira_preview_payload(issues, _existing_jira_keys(session))


@router.post("/cms/sessions/{session_id}/tasks/jira-import")
async def cms_import_jira_tasks(
    session_id: int,
    body: JiraImportRequest,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_TASKS_MANAGE)),
) -> dict:
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    try:
        selected = {key.strip().upper() for key in body.selected_keys if key.strip()}
        issues = await _jira_preview(request.app.state.http_session, body.jql, body.max_results)

        # Same best-effort description pre-fetch as the manager import path —
        # see app_api.app_import_jira_tasks for the rationale.
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
        # Same import-side log line as the manager path — see app_api.
        logger.info(
            "jira import description fetch (cms) chat=%s tried=%d filled_text=%d filled_adf=%d filled_html=%d",
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
                    story_points=issue.get("story_points"),
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

        _, result = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    except TaskQueueError as exc:
        await _audit(request, "cms.task.jira_import", actor.username, "failed", {"error": str(exc), "session_id": session_id})
        _raise_task_error(exc)

    await _audit(request, "cms.task.jira_import", actor.username, "ok", {"session_id": session_id, "count": len(result.tasks)})
    return _mutation_payload(result, session_id)


async def _broadcast_session_state(request: Request, session: Session) -> None:
    """Publish a fresh state snapshot so participants see CMS-driven changes.

    ``_publish_state`` is itself best-effort (swallows pub/sub failures) but we
    add a second guard here so a CMS-driven mutation surface never returns
    5xx because the broadcast attempt blew up in an unexpected way.
    """
    try:
        await _publish_state(request, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CMS broadcast failed: %s", exc)


async def _purge_redis_tokens_for_session(request: Request, chat_id: int, topic_id: Optional[int]) -> None:
    """Best-effort: drop live ``web:<token>`` keys belonging to the session.

    Database expiry is updated by ``cms_store.revoke_web_token`` per token, but
    that only flips the read-model. The actual short-lived Redis entries
    (which authorize ``GET /web/state/...``) are wiped here so participants
    lose access immediately when an admin removes the session.
    """
    redis_client = getattr(request.app.state, "web_redis", None)
    if not redis_client:
        return
    try:
        async for key in redis_client.scan_iter(match="web:*", count=200):
            raw = await redis_client.get(key)
            if not raw:
                continue
            try:
                info = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if int(info.get("chat_id", -1)) != chat_id:
                continue
            stored_topic = info.get("topic_id")
            if (stored_topic is None) != (topic_id is None):
                continue
            if stored_topic is not None and int(stored_topic) != int(topic_id or 0):
                continue
            await redis_client.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis web-token purge failed: %s", exc)


@router.post("/cms/sessions/{session_id}/close")
async def cms_close_session(
    session_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Force-finish a session from the CMS. Idempotent — safe to call twice.

    Behaves identically to the manager-driven ``POST /app/sessions/{chat_id}/finish``
    — both delegate to ``CloseSessionUseCase``.
    """
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    repo = request.app.state.repository
    before = await _get_repo_session(repo, chat_id, topic_id)
    was_completed = before.batch_completed

    use_case = CloseSessionUseCase(repo)
    refreshed_session, completed = await use_case.execute(chat_id, topic_id)
    await _broadcast_session_state(request, refreshed_session)
    await _audit(
        request,
        "cms.session.close",
        actor.username,
        "ok",
        {"session_id": session_id, "completed_count": len(completed)},
    )
    await maybe_notify_session_finished(
        request,
        refreshed_session,
        was_completed=was_completed,
        actor=actor,
        close_method="CMS force-close",
    )
    return {
        "ok": True,
        "session_id": session_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "completed_count": len(completed),
        "batch_completed": True,
    }


@router.delete("/cms/sessions/{session_id}")
async def cms_delete_session(
    session_id: int,
    request: Request,
    actor: CmsPrincipal = Depends(require_permission(PERM_APP_SESSIONS_MANAGE)),
) -> dict:
    """Soft-delete a session.

    The CMS read-model row is flagged ``deleted_at`` so it disappears from all
    listings together with its tasks/votes/participants/tokens. The live
    Redis state and any pending invite tokens are also dropped so the deleted
    session cannot be reopened by a stale browser tab.
    """
    chat_id, topic_id = await _session_ref(request, session_id, actor)
    store = _get_cms_store(request)
    deleted_ref = await store.soft_delete_session(session_id)
    if deleted_ref is None:
        raise HTTPException(status_code=404, detail="Session not found or already deleted")

    repo = request.app.state.repository
    try:
        if hasattr(repo, "delete_session_async"):
            await repo.delete_session_async(chat_id, topic_id)
        elif hasattr(repo, "delete_session"):
            await repo.delete_session(chat_id, topic_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live session delete failed (id=%s): %s", session_id, exc)

    await _purge_redis_tokens_for_session(request, chat_id, topic_id)

    await _audit(
        request,
        "cms.session.delete",
        actor.username,
        "ok",
        {"session_id": session_id, "chat_id": chat_id, "topic_id": topic_id},
    )
    return {
        "ok": True,
        "session_id": session_id,
        "deleted": True,
    }

