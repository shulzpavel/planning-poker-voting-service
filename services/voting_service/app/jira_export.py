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

class JiraStoryPointsSyncBody(BaseModel):
    skip_errors: bool = True


@app_router.post("/app/sessions/{chat_id}/jira-story-points/sync")
async def app_sync_jira_story_points(
    chat_id: int,
    body: JiraStoryPointsSyncBody,
    request: Request,
    topic_id: Optional[int] = Query(None),
    actor: CmsPrincipal = Depends(_require_manager_session),
):
    """Write final SP from the last finished batch into Jira (manager-initiated)."""
    session = await request.app.state.repository.get_session(chat_id, topic_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.last_batch:
        raise HTTPException(status_code=400, detail="Нет завершённого батча для синхронизации")

    from app.adapters.jira_service_client import JiraServiceHttpClient
    from app.usecases.update_jira_sp import UpdateJiraStoryPointsUseCase

    jira_client = JiraServiceHttpClient()
    try:
        use_case = UpdateJiraStoryPointsUseCase(
            jira_client,
            request.app.state.repository,
        )
        updated, failed, skipped = await use_case.execute(
            chat_id,
            topic_id,
            skip_errors=body.skip_errors,
        )
    finally:
        await jira_client.close()
    await _audit(
        request,
        "app.session.jira_sp_sync",
        actor.username,
        "ok" if not failed else "partial",
        {
            "chat_id": chat_id,
            "updated": updated,
            "failed": failed,
            "skipped_count": len(skipped),
        },
    )
    return {
        "updated": updated,
        "failed": failed,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Session summary (used by the post-finish "results" page and CSV export)
# ---------------------------------------------------------------------------


def _summary_payload(
    session: Session,
    *,
    title: str,
    tasks_limit: Optional[int] = None,
) -> dict:
    """Build a detailed summary of the (current or just-finished) session.

    Works in both states:
    - phase == "complete": tasks read from ``session.last_batch``.
    - in-progress: completed slice = tasks_queue[:current_task_index].

    Aggregates (stats / participants) are ALWAYS computed across the full
    batch. ``tasks_limit`` lets callers (e.g. the Finished-page UI) inline
    only the first slice — they then page through the rest via
    ``/sessions/{chat_id}/summary/tasks``. CSV export does not pass the limit.
    """
    full_completed = _completed_in_batch(session)

    if tasks_limit is None:
        completed = full_completed
        completed_next_cursor: Optional[str] = None
    else:
        limit = max(1, min(tasks_limit, COMPLETED_MAX_LIMIT))
        completed = full_completed[:limit]
        completed_next_cursor = str(len(completed)) if len(completed) < len(full_completed) else None

    with_estimate = sum(
        1
        for entry in full_completed
        if entry["story_points"] is not None or entry.get("story_points_by_track")
    )
    consensus_count = sum(1 for entry in full_completed if entry["consensus"])
    total_voters = sum(entry["voter_count"] for entry in full_completed)
    total_story_points = sum(
        entry["story_points"]
        for entry in full_completed
        if entry["story_points"] is not None
    )
    total_story_points_by_track: dict[str, int] = {}
    for entry in full_completed:
        by_track = entry.get("story_points_by_track")
        if not isinstance(by_track, dict):
            continue
        for track_key, value in by_track.items():
            try:
                total_story_points_by_track[track_key] = total_story_points_by_track.get(track_key, 0) + int(value)
            except (TypeError, ValueError):
                continue

    # We persist a snapshot of the batch start time so finish/auto-next-on-last
    # don't erase it. Fall back to the current live timestamp for in-flight
    # sessions; final fallback is the first task's created_at for very old
    # sessions imported without timing data.
    started_at = (
        session.last_batch_started_at
        or session.current_batch_started_at
        or (session.last_batch[0].created_at if session.last_batch else None)
        or (session.tasks_queue[0].created_at if session.tasks_queue else None)
    )

    finished_at: Optional[str] = None
    if session.batch_completed and session.last_batch:
        finished_at = session.last_batch[0].completed_at

    # Stable participant roster across the session (manager + voters).
    participant_names = sorted(
        {participant.name for participant in session.participants.values() if participant.name},
        key=str.casefold,
    )
    participants_detailed = _participant_report_rows(session)
    estimation_payload = estimation_mode_payload(session.estimation_mode)

    return {
        "chat_id": session.chat_id,
        "topic_id": session.topic_id,
        "title": title,
        "phase": "complete" if session.batch_completed else ("in_progress" if full_completed else "fresh"),
        "started_at": started_at,
        "finished_at": finished_at,
        "tasks_queue_count": len(session.tasks_queue),
        **estimation_payload,
        "completed_tasks": completed,
        "completed_next_cursor": completed_next_cursor,
        "participants": participant_names,
        "participants_detailed": participants_detailed,
        "stats": {
            # Aggregates are always computed across the full batch so the UI
            # can show truthful totals before pulling every task into memory.
            "total_completed": len(full_completed),
            "with_estimate": with_estimate,
            "consensus_count": consensus_count,
            "votes_cast": total_voters,
            "total_story_points": total_story_points,
            "total_story_points_by_track": total_story_points_by_track,
        },
    }


@app_router.get("/app/sessions/{chat_id}/summary")
async def app_session_summary(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    tasks_limit: Optional[int] = Query(default=None, ge=1, le=COMPLETED_MAX_LIMIT),
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """JSON-summary for the Finished-session page. Pass ``tasks_limit`` to
    inline only the first slice of completed tasks; remaining pages are
    served by ``/summary/tasks``. Aggregate stats are always exact."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    return _summary_payload(session, title=resolved_title, tasks_limit=tasks_limit)


@app_router.get("/app/sessions/{chat_id}/summary/tasks")
async def app_session_summary_tasks(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    limit: int = Query(default=COMPLETED_DEFAULT_LIMIT, ge=1, le=COMPLETED_MAX_LIMIT),
    cursor: Optional[str] = None,
    _: CmsPrincipal = Depends(_require_manager_session),
) -> dict:
    """Page through the completed-tasks list for the Finished-session report.

    Shape matches the existing CMS list contract (``items``,
    ``next_cursor``, ``limit``, ``total``) so the frontend can drop it into
    the shared ``useCmsList``-style hook."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    return _paginate_completed_in_batch(session, limit=limit, cursor=cursor)


def _format_distribution(distribution: dict[str, int]) -> str:
    """Render `{5: 3, 8: 1}` as `5×3, 8×1` (sorted by descending count)."""
    if not distribution:
        return ""
    pairs = sorted(distribution.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{value}×{count}" for value, count in pairs)


def _format_track_totals(totals: dict[str, int]) -> str:
    if not totals:
        return "—"
    return ", ".join(f"{track}: {value}" for track, value in totals.items())


def _format_task_track_results(entry: dict) -> str:
    votes = entry.get("votes") or []
    if not votes:
        return "—"

    groups: dict[str, list[dict]] = {}
    for vote in votes:
        label = vote.get("track_label") or "Общая оценка"
        groups.setdefault(label, []).append(vote)

    parts: list[str] = []
    for track_label, track_votes in groups.items():
        track_key = next((vote.get("track") for vote in track_votes if vote.get("track")), None)
        final_value = None
        by_track = entry.get("story_points_by_track")
        if track_key and isinstance(by_track, dict):
            final_value = by_track.get(track_key)
        elif entry.get("story_points") is not None:
            final_value = entry.get("story_points")

        distribution: dict[str, int] = {}
        participant_bits: list[str] = []
        for vote in track_votes:
            raw_value = vote.get("value")
            # Explicit None/empty check: a vote of 0 is a legitimate value
            # and must not collapse into the "—" placeholder.
            value = "—" if raw_value is None or raw_value == "" else str(raw_value)
            distribution[value] = distribution.get(value, 0) + 1
            role = f" [{vote.get('role')}]" if vote.get("role") else ""
            participant_bits.append(f"{vote.get('name') or '—'}{role}: {value}")

        final_label = f"final {final_value} SP" if final_value is not None else "final —"
        parts.append(
            f"{track_label} ({final_label}; {_format_distribution(distribution) or '—'}; "
            + "; ".join(participant_bits)
            + ")"
        )

    return " | ".join(parts)


def _normalise_cell_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _join_summary_list(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "; ".join(_normalise_cell_text(item) for item in value if _normalise_cell_text(item))


def _csv_ai_summary_fields(ai_summary: Optional[dict]) -> tuple[str, str, str, str, str, str, str, str, str]:
    """Flatten the persisted AI result for CSV cells.

    Keep this tied to the stored ``Task.ai_summary`` schema, not to the prompt
    text, so downloaded reports contain the actual model estimate.
    """
    if not ai_summary or not isinstance(ai_summary, dict):
        return "", "", "", "", "", "", "", "", ""

    description = _normalise_cell_text(ai_summary.get("description"))
    complexity = _normalise_cell_text(ai_summary.get("complexity"))
    methods = _normalise_cell_text(_join_summary_list(ai_summary.get("methods")))
    sp_dev = _normalise_cell_text(ai_summary.get("sp_dev"))
    sp_test = _normalise_cell_text(ai_summary.get("sp_test"))
    sp_final = _normalise_cell_text(ai_summary.get("sp_final"))
    confidence = _normalise_cell_text(ai_summary.get("confidence"))
    assumptions = _normalise_cell_text(_join_summary_list(ai_summary.get("assumptions")))
    estimation_model = _normalise_cell_text(ai_summary.get("estimation_model"))
    return description, complexity, methods, sp_dev, sp_test, sp_final, confidence, assumptions, estimation_model


def _download_filename(title: str, chat_id: int, extension: str) -> str:
    """ASCII fallback filename for Content-Disposition headers."""
    safe_title = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in (title or "session"))
    safe_title = "_".join(part for part in safe_title.split("_") if part) or "session"
    return f"REPORT_{safe_title}.{extension}"


def _content_disposition(title: str, chat_id: int, extension: str) -> str:
    filename = _download_filename(title, chat_id, extension)
    utf8_filename = f"REPORT_{title or 'session'}.{extension}"
    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(utf8_filename)}"


def _md_escape(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _md_link(label: object, url: object) -> str:
    clean_url = str(url or "").strip()
    clean_label = _md_escape(label)
    if not clean_url:
        return clean_label
    return f"[{clean_label or _md_escape(clean_url)}]({clean_url})"


def _markdown_report(summary: dict) -> str:
    stats = summary["stats"]
    lines = [
        f"# Planning Poker: {_md_escape(summary['title'])}",
        "",
        "## Summary",
        "",
        f"- **Estimation method:** {_md_escape(summary.get('estimation_mode_label') or 'SP')}",
        f"- **TOTAL SP:** {stats['total_story_points']}",
        f"- **Split SP totals:** {_md_escape(_format_track_totals(stats.get('total_story_points_by_track') or {}))}",
        f"- **Completed tasks:** {stats['total_completed']}",
        f"- **With final estimate:** {stats['with_estimate']} / {stats['total_completed']}",
        f"- **Consensus:** {stats['consensus_count']} / {stats['total_completed']}",
        f"- **Votes cast:** {stats['votes_cast']}",
        f"- **Started:** {_md_escape(summary['started_at'] or '—')}",
        f"- **Finished:** {_md_escape(summary['finished_at'] or '—')}",
        "",
        "## Participants",
        "",
        "| Participant | Role | Track |",
        "|---|---|---|",
    ]
    participants_detailed = summary.get("participants_detailed") or []
    if participants_detailed:
        for participant in participants_detailed:
            lines.append(
                f"| {_md_escape(participant.get('name'))} | "
                f"{_md_escape(participant.get('role') or '—')} | "
                f"{_md_escape(participant.get('track_label') or '—')} |"
            )
    else:
        lines.append("| — | — | — |")
    lines.extend([
        "",
        "## Results By Task",
        "",
    ])

    if not summary["completed_tasks"]:
        lines.extend(["No completed tasks.", ""])
        return "\n".join(lines).strip() + "\n"

    lines.extend([
        "| # | Task | Final SP | Results | Consensus | AI Description |",
        "|---:|---|---:|---|---|---|",
    ])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        task_label = entry["jira_key"] or entry["summary"]
        task = _md_link(task_label, entry.get("url"))
        if entry["jira_key"]:
            task = f"{task}<br />{_md_escape(entry['summary'])}"
        ai_description, _, _, _, _, ai_sp_final, _, _, _ = _csv_ai_summary_fields(entry.get("ai_summary"))
        ai_table_value = " — ".join(part for part in [ai_sp_final and f"{ai_sp_final} SP", ai_description] if part)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    task,
                    _md_escape(_final_estimate_label(entry)),
                    _md_escape(_format_task_track_results(entry)),
                    "yes" if entry["consensus"] else "no",
                    _md_escape(ai_table_value or "—"),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Vote Details", ""])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        title = entry["jira_key"] or entry["summary"]
        lines.extend([
            "---",
            "",
            f"### {idx}. {_md_escape(title)}",
            "",
            f"- **Final SP:** {_md_escape(_final_estimate_label(entry))}",
            f"- **Distribution:** {_md_escape(_format_distribution(entry['distribution']) or '—')}",
        ])
        if entry.get("url"):
            lines.append(f"- **Link:** {entry['url']}")
        if entry.get("ai_summary"):
            (
                ai_description,
                ai_complexity,
                ai_methods,
                ai_sp_dev,
                ai_sp_test,
                ai_sp_final,
                ai_confidence,
                ai_assumptions,
                ai_estimation_model,
            ) = _csv_ai_summary_fields(entry.get("ai_summary"))
            if ai_description:
                lines.append(f"- **AI description:** {_md_escape(ai_description)}")
            if ai_complexity:
                lines.append(f"- **AI complexity:** {_md_escape(ai_complexity)}")
            if ai_methods:
                lines.append(f"- **AI methods:** {_md_escape(ai_methods)}")
            if ai_sp_dev or ai_sp_test or ai_sp_final:
                lines.append(
                    "- **AI estimate:** "
                    + _md_escape(
                        ", ".join(
                            part
                            for part in [
                                ai_sp_dev and f"dev {ai_sp_dev} SP",
                                ai_sp_test and f"test {ai_sp_test} SP",
                                ai_sp_final and f"final {ai_sp_final} SP",
                            ]
                            if part
                        )
                    )
                )
            if ai_confidence:
                lines.append(f"- **AI confidence:** {_md_escape(ai_confidence)}")
            if ai_assumptions:
                lines.append(f"- **AI assumptions:** {_md_escape(ai_assumptions)}")
            if ai_estimation_model:
                lines.append(f"- **AI estimation model:** {_md_escape(ai_estimation_model)}")
        lines.extend(["", "| Track | Role | Participant | Vote |", "|---|---|---|---|"])
        if entry["votes"]:
            for vote in entry["votes"]:
                lines.append(
                    f"| {_md_escape(vote.get('track_label') or '—')} | "
                    f"{_md_escape(vote.get('role') or '—')} | "
                    f"{_md_escape(vote['name'])} | "
                    f"{_md_escape(vote['value'])} |"
                )
        else:
            lines.append("| — | — | — | — |")
        lines.extend(["", "---", ""])

    return "\n".join(lines).strip() + "\n"


def _csv_report(summary: dict) -> str:
    """Build an Excel/Sheets-friendly report with readable sections."""
    participant_names: list[str] = summary["participants"]
    stats = summary["stats"]
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["Planning Poker Report"])
    writer.writerow(["Title", summary["title"]])
    writer.writerow(["Chat ID", summary["chat_id"]])
    writer.writerow(["Topic ID", summary["topic_id"] if summary["topic_id"] is not None else "—"])
    writer.writerow(["Started", summary["started_at"] or "—"])
    writer.writerow(["Finished", summary["finished_at"] or "—"])
    writer.writerow(["Phase", summary["phase"]])
    writer.writerow(["Estimation Method", summary.get("estimation_mode_label") or "SP"])
    writer.writerow([])

    writer.writerow(["Summary"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["TOTAL SP", stats["total_story_points"]])
    writer.writerow(["Split SP totals", _format_track_totals(stats.get("total_story_points_by_track") or {})])
    writer.writerow(["Completed tasks", stats["total_completed"]])
    writer.writerow(["With final estimate", f"{stats['with_estimate']} / {stats['total_completed']}"])
    writer.writerow(["Consensus", f"{stats['consensus_count']} / {stats['total_completed']}"])
    writer.writerow(["Votes cast", stats["votes_cast"]])
    writer.writerow([])

    writer.writerow(["Participants"])
    participants_detailed = summary.get("participants_detailed") or []
    if participants_detailed:
        writer.writerow(["Name", "Role", "Track"])
        for participant in participants_detailed:
            writer.writerow([
                participant.get("name") or "—",
                participant.get("role") or "—",
                participant.get("track_label") or "—",
            ])
    elif participant_names:
        writer.writerow(["Name", "Role", "Track"])
        for name in participant_names:
            writer.writerow([name, "—", "—"])
    else:
        writer.writerow(["—"])
    writer.writerow([])

    writer.writerow(["Results By Task"])
    writer.writerow([
        "#",
        "Jira Key",
        "Task",
        "Final SP",
        "Estimation Method",
        "Results",
        "Consensus",
        "AI Description",
        "AI Complexity",
        "AI Methods",
        "AI SP Dev",
        "AI SP Test",
        "AI SP Final",
        "AI Confidence",
        "AI Assumptions",
        "AI Estimation Model",
        "URL",
        "Completed At",
    ])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        (
            ai_description,
            ai_complexity,
            ai_methods,
            ai_sp_dev,
            ai_sp_test,
            ai_sp_final,
            ai_confidence,
            ai_assumptions,
            ai_estimation_model,
        ) = _csv_ai_summary_fields(entry.get("ai_summary"))
        writer.writerow([
            idx,
            entry["jira_key"] or "",
            entry["summary"],
            _final_estimate_label(entry),
            summary.get("estimation_mode_label") or "SP",
            _format_task_track_results(entry),
            "yes" if entry["consensus"] else "no",
            ai_description or "—",
            ai_complexity or "—",
            ai_methods or "—",
            ai_sp_dev or "—",
            ai_sp_test or "—",
            ai_sp_final or "—",
            ai_confidence or "—",
            ai_assumptions or "—",
            ai_estimation_model or "—",
            entry["url"] or "",
            entry["completed_at"] or "",
        ])
    writer.writerow([])

    writer.writerow(["Vote Details"])
    writer.writerow(["Task #", "Jira Key", "Task", "Method", "Track", "Role", "Participant", "Vote"])
    for idx, entry in enumerate(summary["completed_tasks"], start=1):
        if entry["votes"]:
            for vote in entry["votes"]:
                writer.writerow([
                    idx,
                    entry["jira_key"] or "",
                    entry["summary"],
                    summary.get("estimation_mode_label") or "SP",
                    vote.get("track_label") or "—",
                    vote.get("role") or "—",
                    vote["name"],
                    vote["value"],
                ])
        else:
            writer.writerow([idx, entry["jira_key"] or "", entry["summary"], summary.get("estimation_mode_label") or "SP", "—", "—", "—", "—"])

    return buf.getvalue()


@app_router.get("/app/sessions/{chat_id}/summary.csv")
async def app_session_summary_csv(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> StreamingResponse:
    """Export the session summary as a structured, human-readable CSV."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    summary = _summary_payload(session, title=resolved_title)
    csv_bytes = _csv_report(summary).encode("utf-8-sig")  # BOM so Excel detects UTF-8

    content_disposition = _content_disposition(summary["title"], chat_id, "csv")

    await _audit(
        request,
        "app.session.summary_export",
        actor.username,
        "ok",
        {"chat_id": chat_id, "format": "csv", "rows": len(summary["completed_tasks"])},
    )

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition},
    )


@app_router.get("/app/sessions/{chat_id}/summary.md")
async def app_session_summary_markdown(
    chat_id: int,
    request: Request,
    topic_id: Optional[int] = None,
    title: Optional[str] = Query(default=None),
    actor: CmsPrincipal = Depends(_require_manager_session),
) -> StreamingResponse:
    """Export a Confluence-friendly Markdown report for a planning session."""
    session = await _get_repo_session(request.app.state.repository, chat_id, topic_id)
    stored_title = await _stored_session_title(request, chat_id, topic_id)
    resolved_title = _resolve_session_title(title, stored_title)
    summary = _summary_payload(session, title=resolved_title)
    markdown = _markdown_report(summary).encode("utf-8")

    await _audit(
        request,
        "app.session.summary_export",
        actor.username,
        "ok",
        {"chat_id": chat_id, "format": "md", "rows": len(summary["completed_tasks"])},
    )

    return StreamingResponse(
        iter([markdown]),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(summary["title"], chat_id, "md")},
    )
