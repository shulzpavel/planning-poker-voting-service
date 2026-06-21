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

@app_router.post("/app/sessions")
async def create_app_session(
    body: AppSessionCreateRequest,
    request: Request,
    actor: CmsPrincipal = Depends(_manager_dep),
) -> dict:
    resolved_team_id = resolve_create_team_id(actor, body.team_id)
    repo = request.app.state.repository
    chat_id = _new_app_chat_id()
    topic_id = None
    session = await _get_repo_session(repo, chat_id, topic_id)
    if body.estimation_mode:
        mode = normalise_estimation_mode(body.estimation_mode)
        if mode not in VALID_ESTIMATION_MODES:
            raise HTTPException(status_code=400, detail="Invalid estimation mode")

        def set_mode(session: Session) -> None:
            session.estimation_mode = mode

        session, _ = await _mutate_repo_session(repo, chat_id, topic_id, set_mode)
    cms_store = getattr(request.app.state, "cms_store", None)
    if cms_store is not None:
        await cms_store.set_session_team_by_chat(chat_id, topic_id, resolved_team_id)
    token, invite_url = await _create_invite_token(
        request,
        chat_id,
        topic_id,
        body.title,
        team_id=resolved_team_id,
    )
    await _audit(
        request,
        "app.session.create",
        actor.username,
        "ok",
        {"chat_id": chat_id, "title": body.title, "team_id": resolved_team_id},
    )
    return _manager_session_payload(
        session,
        title=body.title,
        invite_url=invite_url,
        token=token,
        team_id=resolved_team_id,
    )


@app_router.post("/app/demo-session")
async def create_demo_session(request: Request, reset: bool = Query(default=False)) -> dict:
    """Create or reuse a real demo session for local/product testing."""
    if not _demo_enabled():
        raise HTTPException(status_code=404, detail="Demo session is disabled")

    from app.adapters.jira_service_client import JiraServiceHttpClient

    jira_client = JiraServiceHttpClient()
    try:
        demo_rows = await jira_client.parse_jira_request(DEMO_JQL, max_results=20)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Demo tasks unavailable: {exc}") from exc
    finally:
        await jira_client.close()

    if not demo_rows:
        raise HTTPException(status_code=503, detail="No demo tasks returned from jira-service")

    chat_id = DEMO_CHAT_ID
    topic_id = None
    repo = request.app.state.repository

    def mutate(session: Session) -> None:
        if reset:
            session.participants.clear()
            session.tasks_queue.clear()
            session.history.clear()
            session.last_batch.clear()

        # Demo tasks may already live in ``last_batch`` after a previous run.
        # Do not treat that as "already imported" — only skip keys already queued.
        if not session.tasks_queue:
            queued_keys = {task.jira_key for task in session.tasks_queue if task.jira_key}
            for item in demo_rows:
                key = item["key"]
                if key in queued_keys:
                    continue
                session.tasks_queue.append(
                    Task(
                        jira_key=key,
                        summary=item["summary"],
                        story_points=item.get("story_points") or None,
                        source="jira",
                        jql=DEMO_JQL,
                    )
                )
                queued_keys.add(key)

        if session.tasks_queue and (reset or session.batch_completed or not session.current_batch_started_at or not session.current_task):
            session.current_task_index = 0
            session.batch_completed = False
            session.current_batch_started_at = datetime.utcnow().isoformat()
            session.revealed_task_id = None
            if session.current_task:
                clear_task_votes(session.current_task, session.estimation_mode)

        session.bump_tasks_version()

    session, _ = await _mutate_repo_session(repo, chat_id, topic_id, mutate)
    token, invite_url = await _create_invite_token(request, chat_id, topic_id, DEMO_TITLE)
    await _publish_state(request, session)
    await _audit(request, "app.demo_session", None, "ok", {"chat_id": chat_id, "reset": reset})
    return _manager_session_payload(session, title=DEMO_TITLE, invite_url=invite_url, token=token)


@app_router.get("/app/sessions/{chat_id}/state")
async def app_session_state(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = None,
    completed_limit: Optional[int] = Query(default=None, ge=1, le=COMPLETED_MAX_LIMIT),
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    # Backfill description for the current Jira task when it wasn't
    # captured at import time (sessions imported before the field
    # landed). No-op on the warm path — once the field is filled in,
    # the helper short-circuits without doing any I/O.
    await _ensure_current_task_description(request, chat_id, topic_id)
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_row = await _stored_session_row(request, chat_id, topic_id)
    stored_title = (stored_row.get("title") or "").strip() if stored_row else None
    resolved_title = _resolve_session_title(title, stored_title or None)
    return _manager_session_payload(
        session,
        title=resolved_title,
        completed_limit=completed_limit,
        team_id=stored_row.get("team_id") if stored_row else None,
        team=stored_row.get("team") if stored_row else None,
    )


@app_router.get("/app/sessions/{chat_id}/completed")
async def app_session_completed(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    limit: int = Query(default=COMPLETED_DEFAULT_LIMIT, ge=1, le=COMPLETED_MAX_LIMIT),
    cursor: Optional[str] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Paginated, oldest-first slice of tasks already played in the active
    batch. Use it to lazy-load Manager's HistoryStrip and the Finished-session
    report without pulling the entire batch in one payload."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    return _paginate_completed_in_batch(session, limit=limit, cursor=cursor)


@app_router.patch("/app/sessions/{chat_id}/title")
async def app_rename_session(
    chat_id: int,
    body: AppSessionRenameRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Rename an active manager session.

    The friendly title is stored on ``cms_sessions.title`` so the CMS surfaces
    the same name. Unlike create/invite-regenerate this endpoint *always*
    overwrites the stored title — the manager is the source of truth here.
    """
    await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    cms_store = getattr(request.app.state, "cms_store", None)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title must not be empty")
    if cms_store is not None:
        try:
            await cms_store.set_session_title_by_chat(
                chat_id, topic_id, new_title, only_if_empty=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "rename_session failed chat_id=%s topic_id=%s err=%r",
                chat_id,
                topic_id,
                exc,
            )
            raise HTTPException(status_code=503, detail="Title store unavailable") from exc
    await _audit(
        request,
        "app.session.rename",
        actor.username,
        "ok",
        {"chat_id": chat_id, "title": new_title},
    )
    return {"chat_id": chat_id, "topic_id": topic_id, "title": new_title}

@app_router.post("/app/sessions/{chat_id}/start")
async def app_start_session(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    body: Optional[AppSessionStartRequest] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    start_body = body or AppSessionStartRequest()

    def mutate(session: Session) -> Optional[str]:
        if not session.tasks_queue:
            return "Add at least one task before starting."
        if start_body.estimation_mode:
            mode = normalise_estimation_mode(start_body.estimation_mode)
            if mode not in VALID_ESTIMATION_MODES:
                return "Invalid estimation mode"
            session.estimation_mode = mode
        session.normalize_current_task_index()
        session.batch_completed = False
        started = datetime.utcnow().isoformat()
        session.current_batch_started_at = started
        session.last_batch_started_at = started  # preserved through next/finish for summary
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
        session.bump_tasks_version()
        return None

    session, error = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    if error:
        raise HTTPException(status_code=400, detail=error)
    # First task of the batch just became active — make sure its Jira
    # description is loaded before we publish/return, otherwise the
    # voter UI would briefly miss the spec block until the next WS
    # push. Helper mutates ``session`` in place.
    await _ensure_current_task_description(request, chat_id, topic_id, session=session)
    await _publish_state(request, session)
    await _audit(request, "app.session.start", actor.username, "ok", {"chat_id": chat_id})
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/ai-summary")
async def app_generate_ai_summary(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    async_mode: bool = Query(False, alias="async"),
    refresh: bool = Query(False),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Generate a facilitator-facing AI hint for the current voting task via Anthropic.

    The summary is stored on the active ``Task`` and appears in manager state and
    participant WebSocket payloads. Strict mode: no heuristic fallback when LLM fails.

    Each call costs an Anthropic completion, so we cap them per CMS actor — a
    stuck UI loop must never burn the LLM budget unbounded.
    """
    await enforce_rate_limit(
        request.app.state.web_redis,
        key=f"rl:ai_summary:actor:{actor.username}",
        limit=AI_SUMMARY_RATE_LIMIT_MAX,
        window_seconds=AI_SUMMARY_RATE_LIMIT_WINDOW_SECONDS,
        error_detail="Too many AI summary requests",
    )
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    if not session.current_task or not session.current_batch_started_at:
        raise HTTPException(status_code=400, detail="Start voting before generating an AI summary.")

    task = session.current_task
    if task.ai_summary and not refresh:
        if task.jira_key and isinstance(task.ai_summary, dict):
            from services.voting_service.ai_job_runners import spawn_session_ai_jira_export
            from services.voting_service.ai_summary_jira_export import should_skip_jira_export

            if not should_skip_jira_export(task.ai_summary):
                spawn_session_ai_jira_export(
                    request.app,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    task_id=task.task_id,
                    issue_key=task.jira_key,
                    summary=dict(task.ai_summary),
                    actor_username=actor.username,
                )
        return _manager_session_payload(session)

    from services.voting_service.ai_job_runners import run_session_ai_summary_job
    from services.voting_service.ai_jobs import get_job, get_or_create_job, job_public_view, spawn_ai_job

    if async_mode:
        redis = request.app.state.web_redis
        resource_key = f"session:{chat_id}:{task.task_id}:refresh" if refresh else f"session:{chat_id}:{task.task_id}"
        job_id, is_new = await get_or_create_job(
            redis,
            kind="session_ai_summary",
            resource_key=resource_key,
            actor=actor.username,
        )
        if is_new:
            spawn_ai_job(
                run_session_ai_summary_job(
                    request.app,
                    job_id=job_id,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    task_id=task.task_id,
                    actor_username=actor.username,
                    force_refresh=refresh,
                )
            )
        job_record = await get_job(redis, job_id)
        return job_public_view(job_record or {"job_id": job_id, "status": "queued", "phase": "queued", "message": "В очереди"})

    http_session = request.app.state.http_session
    jira_context = None
    if task.jira_key:
        try:
            jira_context = await fetch_jira_issue_context(http_session, task.jira_key)
        except LlmSummaryError as exc:
            await _audit(
                request,
                "app.task.ai_summary.generate",
                actor.username,
                "failed",
                {"chat_id": chat_id, "task_id": task.task_id, "error": exc.message},
            )
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        summary = await generate_ai_summary_llm(http_session, task, jira_context)
    except LlmSummaryError as exc:
        await _audit(
            request,
            "app.task.ai_summary.generate",
            actor.username,
            "failed",
            {"chat_id": chat_id, "task_id": task.task_id, "error": exc.message},
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    def mutate(active: Session) -> Optional[str]:
        if not active.current_task or active.current_task.task_id != task.task_id:
            return "Task changed before AI summary could be saved. Refresh and try again."
        active.current_task.ai_summary = summary
        active.current_task.touch()
        active.bump_tasks_version()
        return None

    session, error = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    if error:
        raise HTTPException(status_code=400, detail=error)
    await _publish_state(request, session)
    await _audit(
        request,
        "app.task.ai_summary.generate",
        actor.username,
        "ok",
        {"chat_id": chat_id, "task_id": session.current_task_id, "source": summary.get("source")},
    )
    if task.jira_key:
        from services.voting_service.ai_job_runners import spawn_session_ai_jira_export

        spawn_session_ai_jira_export(
            request.app,
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task.task_id,
            issue_key=task.jira_key,
            summary=dict(summary),
            actor_username=actor.username,
        )
    return _manager_session_payload(session)


@app_router.get("/app/sessions/{chat_id}/ai-summary/jobs/{job_id}")
async def app_ai_summary_job_status(
    chat_id: int,
    job_id: str,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    if not session.current_task:
        raise HTTPException(status_code=400, detail="No active task")

    from services.voting_service.ai_jobs import get_job, job_public_view

    redis = request.app.state.web_redis
    job = await get_job(redis, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AI job not found")
    if job.get("kind") != "session_ai_summary" or not str(job.get("resource_key", "")).startswith(f"session:{chat_id}:"):
        raise HTTPException(status_code=404, detail="AI job not found")
    return job_public_view(job)


@app_router.post("/app/sessions/{chat_id}/next")
async def app_next_task(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    before = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    was_completed = before.batch_completed

    def mutate(session: Session) -> None:
        if session.current_task:
            session.current_task_index += 1
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
            session.current_batch_started_at = datetime.utcnow().isoformat()
            session.batch_completed = False
        else:
            session.current_batch_started_at = None
            session.batch_completed = True
        session.bump_tasks_version()

    session, _ = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    # The active task just rolled over — backfill its description before
    # we broadcast so voters see the right spec block on the very first
    # post-advance render. In-place mutation; no second repo read.
    await _ensure_current_task_description(request, chat_id, topic_id, session=session)
    await _publish_state(request, session)
    await _audit(request, "app.session.next", actor.username, "ok", {"chat_id": chat_id})
    await maybe_notify_session_finished(
        request,
        session,
        was_completed=was_completed,
        actor=actor,
        close_method="Last task completed",
    )
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/skip")
async def app_skip_task(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    before = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    was_completed = before.batch_completed

    # Skip == advance to next task. Run the same mutation as `next` but only
    # record a single `skip` audit event so the audit log isn't polluted with
    # a paired `skip` + `next` for every skip click.
    def mutate(session: Session) -> None:
        if session.current_task:
            session.current_task_index += 1
        session.revealed_task_id = None
        if session.current_task:
            clear_task_votes(session.current_task, session.estimation_mode)
            session.current_batch_started_at = datetime.utcnow().isoformat()
            session.batch_completed = False
        else:
            session.current_batch_started_at = None
            session.batch_completed = True
        session.bump_tasks_version()

    session, _ = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    # Same rationale as ``app_next_task`` — backfill before broadcasting
    # the new active task's state. In-place mutation; no second repo read.
    await _ensure_current_task_description(request, chat_id, topic_id, session=session)
    await _publish_state(request, session)
    await _audit(request, "app.session.skip", actor.username, "ok", {"chat_id": chat_id})
    await maybe_notify_session_finished(
        request,
        session,
        was_completed=was_completed,
        actor=actor,
        close_method="Last task skipped",
    )
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/completed/{task_id}/reopen")
async def app_reopen_completed_task(
    chat_id: int,
    task_id: str,
    body: ReopenCompletedRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    use_case = ReopenCompletedTaskUseCase(request.app.state.repository)
    try:
        result = await use_case.execute(
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            expected_version=body.expected_version,
        )
    except TaskQueueError as exc:
        await _audit(
            request,
            "app.session.completed_reopen",
            actor.username,
            "failed",
            {"error": str(exc), "chat_id": chat_id, "task_id": task_id},
        )
        _raise_task_error(exc)
    await _ensure_current_task_description(request, chat_id, topic_id, session=result.session)
    await _publish_state(request, result.session)
    await _audit(
        request,
        "app.session.completed_reopen",
        actor.username,
        "ok",
        {"chat_id": chat_id, "task_id": task_id},
    )
    return _manager_session_payload(result.session)


@app_router.post("/app/sessions/{chat_id}/final-estimate")
async def app_set_final_estimate(
    chat_id: int,
    body: FinalEstimateRequest,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    def mutate(session: Session) -> Optional[str]:
        if not session.current_task:
            return "No active task."
        task = session.current_task
        if is_split_mode(session.estimation_mode):
            if not body.tracks:
                return "Final estimate requires per-track values for this estimation mode."
            config = get_mode_config(session.estimation_mode)
            allowed = {track.key for track in config.tracks}
            for track_key, value in body.tracks.items():
                if track_key not in allowed:
                    return f"Unknown track: {track_key}"
                if value < 0 or value > MAX_STORY_POINTS:
                    return f"Track value must be between 0 and {MAX_STORY_POINTS} SP"
            task.story_points_by_track = {key: int(value) for key, value in body.tracks.items()}
            task.story_points = None
        else:
            if body.value is None:
                return "Final estimate value is required."
            task.story_points = body.value
        task.touch()
        session.bump_tasks_version()
        return None

    session, error = await _mutate_repo_session(request.app.state.repository, chat_id, topic_id, mutate)
    if error:
        raise HTTPException(status_code=400, detail=error)
    await _publish_state(request, session)
    await _audit(
        request,
        "app.session.final_estimate",
        actor.username,
        "ok",
        {
            "chat_id": chat_id,
            "value": body.value,
            "tracks": body.tracks,
        },
    )
    return _manager_session_payload(session)


@app_router.post("/app/sessions/{chat_id}/finish")
async def app_finish_session(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Finalize the session. Idempotent: safe to call after auto-next-on-last.

    Delegates to ``CloseSessionUseCase`` so the manager-driven finish and the
    CMS-driven force-close stay strictly in sync (they used to be two copies
    of the same mutator).
    """
    repo = request.app.state.repository
    before = await _get_repo_session(repo, chat_id, topic_id)
    was_completed = before.batch_completed

    use_case = CloseSessionUseCase(repo)
    session, completed = await use_case.execute(chat_id, topic_id)
    await _publish_state(request, session)
    await _audit(request, "app.session.finish", actor.username, "ok", {"chat_id": chat_id, "count": len(completed)})
    await maybe_notify_session_finished(
        request,
        session,
        was_completed=was_completed,
        actor=actor,
        close_method="Finish",
    )
    return _manager_session_payload(session)

