"""Background runners for async AI jobs."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from app.domain.session import Session
from app.domain.task import Task
from services.voting_service.ai_jobs import find_cached_scope_summary, run_phased_job
from services.voting_service.ai_summary_llm import LlmSummaryError, fetch_jira_issue_context, generate_ai_summary_llm
from services.voting_service.retro_ai_llm import LlmRetroError, generate_retro_analysis
from services.voting_service.scope_ai_llm import LlmScopeError, generate_scope_analysis

logger = logging.getLogger(__name__)

PhaseSetter = Callable[[str], Awaitable[None]]


def _slim_scope_board_for_job(board: dict[str, Any]) -> dict[str, Any]:
    """Keep async job payloads small — the full Jira snapshot is not needed for polling."""
    snapshot = board.get("snapshot") if isinstance(board.get("snapshot"), dict) else {}
    return {
        "id": board.get("id"),
        "ai_summary": board.get("ai_summary"),
        "ai_summary_history": board.get("ai_summary_history") or [],
        "snapshot": {"refreshed_at": snapshot.get("refreshed_at")},
    }


def _background_request(app) -> SimpleNamespace:
    """Minimal request-like object for shared helpers used from background jobs."""
    return SimpleNamespace(app=app, headers={}, client=None)


async def run_scope_ai_job(
    app,
    *,
    job_id: str,
    board_id: int,
    actor_username: str,
) -> None:
    redis = app.state.web_redis
    store = app.state.cms_store
    http_session = app.state.http_session
    kind = "scope"
    resource_key = f"board:{board_id}"

    async def runner(set_phase: PhaseSetter) -> dict[str, Any]:
        await set_phase("building_context")
        board = await store.get_scope_board(board_id)
        if not board:
            raise LlmScopeError("Scope board not found", status_code=404)

        snapshot = board.get("snapshot") or {}
        snapshot_refreshed_at = snapshot.get("refreshed_at") if isinstance(snapshot, dict) else None
        cached = find_cached_scope_summary(board, snapshot_refreshed_at)
        if cached:
            return {"ai_summary": cached, "board": _slim_scope_board_for_job(board), "cached": True}

        if http_session is None:
            raise LlmScopeError("AI is not configured", status_code=503)

        await set_phase("calling_llm")

        async def notify_repair(message: str) -> None:
            from services.voting_service.ai_jobs import update_job

            await update_job(
                redis,
                job_id,
                message=f"Повторная генерация: {message[:160]}",
            )

        try:
            summary = await generate_scope_analysis(http_session, board, on_repair=notify_repair)
        except LlmScopeError:
            raise

        await set_phase("validating")
        await set_phase("saving")
        updated = await store.save_scope_board_ai_summary(
            board_id,
            summary,
            snapshot_refreshed_at=snapshot_refreshed_at,
        )
        if not updated:
            raise LlmScopeError("Scope board not found", status_code=404)

        logger.info(
            "scope AI job ok board_id=%s actor=%s health=%s",
            board_id,
            actor_username,
            summary.get("health"),
        )
        return {"ai_summary": summary, "board": _slim_scope_board_for_job(updated), "cached": False}

    await run_phased_job(
        redis,
        job_id,
        kind=kind,
        resource_key=resource_key,
        label=f"scope:{board_id}",
        runner=runner,
    )


async def run_retro_ai_job(
    app,
    *,
    job_id: str,
    retro_id: int,
    actor_username: str,
) -> None:
    from app.domain.retro import PHASE_DONE
    from services.voting_service.retro_api import (
        _publish_retro,
        _retro_from_anonymized_snapshot,
        _set_ai,
    )

    redis = app.state.web_redis
    store = app.state.cms_store
    http_session = app.state.http_session
    repo = app.state.retro_repository
    kind = "retro"
    resource_key = f"retro:{retro_id}"

    async def runner(set_phase: PhaseSetter) -> dict[str, Any]:
        await set_phase("building_context")
        retro = await repo.get_retro(retro_id)
        if retro is None:
            row = await store.get_retro(retro_id)
            if row and row.get("snapshot"):
                retro = _retro_from_anonymized_snapshot(row["snapshot"], retro_id)
        if retro is None:
            raise LlmRetroError("Retro not found", status_code=404)
        if retro.phase != PHASE_DONE:
            raise LlmRetroError("Сначала завершите ретро", status_code=409)
        if not retro.cards:
            raise LlmRetroError("Нет карточек для анализа", status_code=400)

        if retro.ai_summary:
            return {"ai_summary": retro.ai_summary, "cached": True}

        if http_session is None:
            raise LlmRetroError("AI is not configured", status_code=503)

        await set_phase("calling_llm")
        summary = await generate_retro_analysis(http_session, retro)

        await set_phase("validating")
        await set_phase("saving")
        await store.save_retro_ai_summary(retro_id, summary)
        try:
            retro, _ = await repo.mutate_retro(retro_id, lambda r: _set_ai(r, summary))
            await _publish_retro(redis, retro)
        except KeyError:
            pass

        logger.info("retro AI job ok retro_id=%s actor=%s", retro_id, actor_username)
        return {"ai_summary": summary, "cached": False}

    await run_phased_job(
        redis,
        job_id,
        kind=kind,
        resource_key=resource_key,
        label=f"retro:{retro_id}",
        runner=runner,
    )


async def run_session_ai_summary_job(
    app,
    *,
    job_id: str,
    chat_id: int,
    topic_id: Optional[int],
    task_id: str,
    actor_username: str,
    force_refresh: bool = False,
) -> None:
    from services.voting_service.app_api import (
        _audit as app_audit,
        _get_repo_session,
        _manager_session_payload,
        _mutate_repo_session,
        _publish_state,
    )

    redis = app.state.web_redis
    repo = app.state.repository
    http_session = app.state.http_session
    kind = "session_ai_summary"
    resource_key = f"session:{chat_id}:{task_id}:refresh" if force_refresh else f"session:{chat_id}:{task_id}"

    async def runner(set_phase: PhaseSetter) -> dict[str, Any]:
        await set_phase("building_context")
        session = await _get_repo_session(repo, chat_id, topic_id)
        if not session.current_task or session.current_task.task_id != task_id:
            raise LlmSummaryError(
                "Task changed before AI summary could be saved. Refresh and try again.",
                status_code=400,
            )
        if not session.current_batch_started_at:
            raise LlmSummaryError("Start voting before generating an AI summary.", status_code=400)

        task = session.current_task
        if task.ai_summary and not force_refresh:
            if task.jira_key:
                from services.voting_service.ai_summary_jira_export import should_skip_jira_export

                if not should_skip_jira_export(task.ai_summary):
                    spawn_session_ai_jira_export(
                        app,
                        chat_id=chat_id,
                        topic_id=topic_id,
                        task_id=task_id,
                        issue_key=task.jira_key,
                        summary=dict(task.ai_summary),
                        actor_username=actor_username,
                    )
            return {"session": _manager_session_payload(session), "cached": True}

        jira_context = None
        if task.jira_key:
            jira_context = await fetch_jira_issue_context(http_session, task.jira_key)

        await set_phase("calling_llm")
        summary = await generate_ai_summary_llm(http_session, task, jira_context)

        await set_phase("validating")

        def mutate(active: Session) -> Optional[str]:
            if not active.current_task or active.current_task.task_id != task_id:
                return "Task changed before AI summary could be saved. Refresh and try again."
            active.current_task.ai_summary = summary
            active.current_task.touch()
            active.bump_tasks_version()
            return None

        await set_phase("saving")
        session, error = await _mutate_repo_session(repo, chat_id, topic_id, mutate)
        if error:
            raise LlmSummaryError(error, status_code=400)

        fake_request = _background_request(app)
        await _publish_state(fake_request, session)
        await app_audit(
            fake_request,
            "app.task.ai_summary.generate",
            actor_username,
            "ok",
            {"chat_id": chat_id, "task_id": session.current_task_id, "source": summary.get("source")},
        )
        logger.info(
            "session AI job ok chat_id=%s task_id=%s actor=%s",
            chat_id,
            task_id,
            actor_username,
        )
        if task.jira_key:
            spawn_session_ai_jira_export(
                app,
                chat_id=chat_id,
                topic_id=topic_id,
                task_id=task_id,
                issue_key=task.jira_key,
                summary=dict(summary),
                actor_username=actor_username,
            )
        return {"session": _manager_session_payload(session), "cached": False}

    await run_phased_job(
        redis,
        job_id,
        kind=kind,
        resource_key=resource_key,
        label=f"session:{chat_id}:{task_id}",
        runner=runner,
    )


async def run_session_ai_jira_export(
    app,
    *,
    chat_id: int,
    topic_id: Optional[int],
    task_id: str,
    issue_key: str,
    summary: dict[str, Any],
    actor_username: str,
) -> None:
    """Fire-and-forget export of AI summary to Jira as an ADF comment."""
    from app.adapters.jira_service_client import JiraServiceHttpClient
    from services.voting_service.ai_summary_jira_export import (
        ai_summary_jira_export_enabled,
        export_ai_summary_to_jira,
        should_skip_jira_export,
    )
    from services.voting_service.app_api import (
        _get_repo_session,
        _mutate_repo_session,
        _publish_state,
    )

    if not ai_summary_jira_export_enabled() or not issue_key:
        return
    if should_skip_jira_export(summary):
        return

    client = JiraServiceHttpClient()
    try:
        session = await _get_repo_session(app.state.repository, chat_id, topic_id)
        task_summary = None
        if session.current_task and session.current_task.task_id == task_id:
            task_summary = session.current_task.summary

        jira_export = await export_ai_summary_to_jira(
            client,
            issue_key=issue_key,
            summary=summary,
            task_summary=task_summary,
        )

        def mutate(active: Session) -> Optional[str]:
            if not active.current_task or active.current_task.task_id != task_id:
                return None
            current_summary = active.current_task.ai_summary
            if not isinstance(current_summary, dict):
                return None
            merged = dict(current_summary)
            merged["jira_export"] = jira_export
            active.current_task.ai_summary = merged
            active.current_task.touch()
            active.bump_tasks_version()
            return None

        session, _ = await _mutate_repo_session(app.state.repository, chat_id, topic_id, mutate)
        if session:
            await _publish_state(_background_request(app), session)
        logger.info(
            "session AI Jira export chat_id=%s task_id=%s key=%s status=%s actor=%s",
            chat_id,
            task_id,
            issue_key,
            jira_export.get("status"),
            actor_username,
        )
    finally:
        await client.close()


def spawn_session_ai_jira_export(
    app,
    *,
    chat_id: int,
    topic_id: Optional[int],
    task_id: str,
    issue_key: str,
    summary: dict[str, Any],
    actor_username: str,
) -> None:
    from services.voting_service.ai_jobs import spawn_ai_job

    spawn_ai_job(
        run_session_ai_jira_export(
            app,
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
            issue_key=issue_key,
            summary=summary,
            actor_username=actor_username,
        )
    )
