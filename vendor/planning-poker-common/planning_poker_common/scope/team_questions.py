"""Team-level open questions for mobile release scope boards."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def empty_team_scope_questions() -> dict[str, Any]:
    return {
        "manual_questions": [],
        "resolved_questions": [],
        "tracked_jira": {},
    }


def normalize_team_scope_questions(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return empty_team_scope_questions()
    manual = raw.get("manual_questions") or []
    resolved = raw.get("resolved_questions") or []
    tracked = raw.get("tracked_jira") or {}
    return {
        "manual_questions": [item for item in manual if isinstance(item, dict) and str(item.get("id") or "").strip()],
        "resolved_questions": [item for item in resolved if isinstance(item, dict) and str(item.get("id") or "").strip()],
        "tracked_jira": {
            str(key).strip(): value
            for key, value in tracked.items()
            if str(key).strip() and isinstance(value, dict)
        },
    }


def team_scope_questions_empty(store: dict[str, Any]) -> bool:
    normalized = normalize_team_scope_questions(store)
    return not normalized["manual_questions"] and not normalized["resolved_questions"] and not normalized["tracked_jira"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_ids(store: dict[str, Any]) -> set[str]:
    normalized = normalize_team_scope_questions(store)
    return {str(item.get("id") or "").strip() for item in normalized["resolved_questions"] if str(item.get("id") or "").strip()}


def merge_team_scope_questions_into_snapshot(
    snapshot: dict[str, Any],
    team_questions: dict[str, Any],
    *,
    open_jira_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Overlay team-level question registry onto a board snapshot."""
    store = normalize_team_scope_questions(team_questions)
    updated = dict(snapshot or {})
    updated["manual_questions"] = [dict(item) for item in store["manual_questions"]]
    updated["resolved_questions"] = sorted(
        [dict(item) for item in store["resolved_questions"]],
        key=lambda item: str(item.get("resolved_at") or ""),
        reverse=True,
    )
    if open_jira_ids:
        tracked = dict(store["tracked_jira"])
        for question_id in open_jira_ids:
            key = str(question_id or "").strip()
            if not key or key in _resolved_ids(store):
                continue
            if key not in tracked:
                continue
            meta = tracked[key]
            updated.setdefault("question_meta", {})[key] = dict(meta)
    elif store["tracked_jira"]:
        updated["question_meta"] = {key: dict(value) for key, value in store["tracked_jira"].items()}
    return updated


def extract_team_scope_questions_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    store = normalize_team_scope_questions(snapshot)
    question_meta = snapshot.get("question_meta") or {}
    if isinstance(question_meta, dict):
        for key, value in question_meta.items():
            if str(key).strip() and isinstance(value, dict):
                store["tracked_jira"][str(key).strip()] = dict(value)
    return store


def snapshot_open_jira_question_ids(snapshot: dict[str, Any]) -> set[str]:
    from planning_poker_common.scope.domain import classify_scope_report_bucket

    ids: set[str] = set()
    resolved = _resolved_ids(extract_team_scope_questions_from_snapshot(snapshot))
    for section in snapshot.get("sections") or []:
        for issue in section.get("issues") or []:
            if classify_scope_report_bucket(issue) != "open_questions":
                continue
            key = str(issue.get("key") or "").strip()
            if key and key not in resolved:
                ids.add(key)
    for section in ("plan_issues", "unplan_issues"):
        for issue in snapshot.get(section) or []:
            if classify_scope_report_bucket(issue) != "open_questions":
                continue
            key = str(issue.get("key") or "").strip()
            if key and key not in resolved:
                ids.add(key)
    return ids


def register_open_jira_questions(
    store: dict[str, Any],
    *,
    question_ids: set[str],
    release_name: str,
    registered_at: Optional[str] = None,
) -> dict[str, Any]:
    updated = normalize_team_scope_questions(store)
    stamp = registered_at or _utc_now_iso()
    release = (release_name or "").strip() or "Релиз"
    resolved = _resolved_ids(updated)
    tracked = dict(updated["tracked_jira"])
    for question_id in question_ids:
        key = str(question_id or "").strip()
        if not key or key in resolved or key in tracked:
            continue
        tracked[key] = {
            "created_at": stamp,
            "created_release_name": release,
        }
    updated["tracked_jira"] = tracked
    return updated


def manual_question_with_release_meta(
    *,
    text: str,
    actor_name: str,
    question_id: str,
    release_name: str,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    stamp = created_at or _utc_now_iso()
    release = (release_name or "").strip() or "Релиз"
    return {
        "id": question_id,
        "summary": text,
        "created_by": actor_name,
        "created_at": stamp,
        "created_release_name": release,
        "kind": "manual",
    }


def resolved_question_with_release_meta(
    source: dict[str, Any],
    *,
    question_id: str,
    comment: str,
    actor_name: str,
    release_name: str,
    resolved_at: Optional[str] = None,
) -> dict[str, Any]:
    stamp = resolved_at or _utc_now_iso()
    release = (release_name or "").strip() or "Релиз"
    tracked_meta = source.get("_tracked_meta") if isinstance(source.get("_tracked_meta"), dict) else {}
    created_at = source.get("created_at") or tracked_meta.get("created_at")
    created_release_name = source.get("created_release_name") or tracked_meta.get("created_release_name")
    payload = {
        **{key: value for key, value in source.items() if key != "_tracked_meta"},
        "id": question_id,
        "comment": comment,
        "resolved_by": actor_name,
        "resolved_at": stamp,
        "resolved_release_name": release,
    }
    if created_at and not payload.get("created_at"):
        payload["created_at"] = created_at
    if created_release_name and not payload.get("created_release_name"):
        payload["created_release_name"] = created_release_name
    return payload


def days_open_between(created_at: Optional[str], resolved_at: Optional[str] = None) -> Optional[int]:
    if not created_at:
        return None
    try:
        opened = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if resolved_at:
        try:
            closed = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
        except ValueError:
            closed = datetime.now(timezone.utc)
    else:
        closed = datetime.now(timezone.utc)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    delta = closed.date() - opened.date()
    return max(0, delta.days)
