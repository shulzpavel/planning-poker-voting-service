"""Pure helpers for monthly scope / buffer dashboards."""

from __future__ import annotations

import copy
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from app.utils.jira_role_contributors import person_bucket_key

IntakeStatus = Literal["ok", "warning", "stop"]
ScopeSectionKind = Literal["planned", "unplanned"]
WorkloadMode = Literal["sp", "sp_dev_test"]
RoleWorkloadHorizon = Literal["active", "sprint"]

DEFAULT_WORKLOAD_MODE: WorkloadMode = "sp"
WORKLOAD_MODES = frozenset({"sp", "sp_dev_test"})

ACTIVE_STATUS_CATEGORIES = frozenset({"new", "indeterminate"})


def normalise_workload_mode(mode: Optional[str]) -> WorkloadMode:
    normalized = str(mode or DEFAULT_WORKLOAD_MODE).strip().lower()
    if normalized in WORKLOAD_MODES:
        return normalized  # type: ignore[return-value]
    return DEFAULT_WORKLOAD_MODE


def is_split_workload_mode(mode: Optional[str]) -> bool:
    return normalise_workload_mode(mode) == "sp_dev_test"


def month_start_iso(month: str) -> str:
    """Return ISO timestamp for the first instant of ``YYYY-MM`` (UTC)."""
    year, month_num = month.split("-", 1)
    start = datetime(int(year), int(month_num), 1, tzinfo=timezone.utc)
    return start.isoformat()


def _parse_created(created: Optional[str]) -> Optional[datetime]:
    if not created:
        return None
    normalized = created.replace("Z", "+00:00")
    if len(normalized) >= 5 and normalized[-5] in "+-" and normalized[-3] != ":":
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def is_scope_creep(created: Optional[str], month: str) -> bool:
    created_at = _parse_created(created)
    if created_at is None:
        return False
    start = _parse_created(month_start_iso(month))
    if start is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= start


def _nullable_story_points_value(raw: Any) -> Optional[float]:
    """Return non-negative SP including explicit zero; None means unset."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw >= 0 else None
    return None


def _story_points_value(raw: Any) -> Optional[float]:
    value = _nullable_story_points_value(raw)
    if value is not None and value > 0:
        return value
    return None


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item]


def _jira_role_assignees(raw: dict[str, Any]) -> dict[str, str]:
    assignees = raw.get("jira_role_assignees")
    if not isinstance(assignees, dict):
        return {"front": "", "back": "", "qa": ""}
    return {
        role: str(assignees.get(role) or "").strip()
        for role in ("front", "back", "qa")
    }


def jira_role_fields_configured() -> dict[str, bool]:
    from config import JIRA_BACK_ASSIGNEE_FIELD, JIRA_FRONT_ASSIGNEE_FIELD, JIRA_QA_ASSIGNEE_FIELD

    return {
        "front": bool(JIRA_FRONT_ASSIGNEE_FIELD),
        "back": bool(JIRA_BACK_ASSIGNEE_FIELD),
        "qa": bool(JIRA_QA_ASSIGNEE_FIELD),
    }


def merge_jira_role_fields_configured(*sources: dict[str, Any] | None) -> dict[str, bool]:
    merged = {"front": False, "back": False, "qa": False}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for role in ("front", "back", "qa"):
            if bool(source.get(role)):
                merged[role] = True
    return merged


def normalize_scope_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Jira issue dict into the scope-board snapshot shape."""
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    issue_type = raw.get("issue_type") if isinstance(raw.get("issue_type"), dict) else {}
    sp = _story_points_value(raw.get("story_points"))
    return {
        "key": str(raw.get("key") or ""),
        "summary": str(raw.get("summary") or ""),
        "url": str(raw.get("url") or ""),
        "story_points": sp,
        "story_points_source": raw.get("story_points_source"),
        "story_points_plan": _story_points_value(raw.get("story_points_plan")),
        "story_points_fact": _story_points_value(raw.get("story_points_fact")),
        "story_points_dev": _nullable_story_points_value(raw.get("story_points_dev")),
        "story_points_test": _nullable_story_points_value(raw.get("story_points_test")),
        "story_points_front": _story_points_value(raw.get("story_points_front")),
        "story_points_back": _story_points_value(raw.get("story_points_back")),
        "story_points_qa": _story_points_value(raw.get("story_points_qa")),
        "story_point_estimate": _story_points_value(raw.get("story_point_estimate")),
        "estimated": sp is not None,
        "status": str(status.get("name") or raw.get("status_name") or ""),
        "status_category": str(status.get("category") or raw.get("status_category") or ""),
        "issue_type": str(issue_type.get("name") or raw.get("issue_type") or ""),
        "labels": [str(label) for label in (raw.get("labels") or []) if label],
        "epic_labels": [str(label) for label in (raw.get("epic_labels") or []) if label],
        "created": raw.get("created"),
        "updated": raw.get("updated"),
        "status_changed_at": raw.get("status_changed_at"),
        "status_entered_at": raw.get("status_entered_at"),
        "epic_linked_at": raw.get("epic_linked_at"),
        "due_date": raw.get("due_date"),
        "resolution": str(raw.get("resolution") or ""),
        "resolution_date": raw.get("resolution_date"),
        "parent_key": raw.get("parent_key"),
        "epic_key": raw.get("epic_key") or raw.get("parent_key"),
        "linked_epic_key": raw.get("linked_epic_key"),
        "priority": str(raw.get("priority") or ""),
        "assignee": str(raw.get("assignee") or ""),
        "developer": str(raw.get("developer") or raw.get("assignee") or ""),
        "developer_source": str(raw.get("developer_source") or "fallback"),
        "role_contributors": raw.get("role_contributors") if isinstance(raw.get("role_contributors"), dict) else {},
        "jira_role_assignees": _jira_role_assignees(raw),
        "role_contributors_list": raw.get("role_contributors_list") if isinstance(raw.get("role_contributors_list"), list) else [],
        "role_workload_items": raw.get("role_workload_items") if isinstance(raw.get("role_workload_items"), list) else [],
        "role_evidence": raw.get("role_evidence") if isinstance(raw.get("role_evidence"), list) else [],
        "reporter": str(raw.get("reporter") or ""),
        "components": _string_list(raw.get("components")),
        "fix_versions": _string_list(raw.get("fix_versions")),
        "versions": _string_list(raw.get("versions")),
        "sprints": _string_list(raw.get("sprints")),
        "sprint": str(raw.get("sprint") or ""),
        "team": str(raw.get("team") or ""),
        "team_labels": _string_list(raw.get("team_labels")),
        "plan_status": str(raw.get("plan_status") or ""),
        "plan_change_reason": str(raw.get("plan_change_reason") or ""),
        "plan_change_reasons": _string_list(raw.get("plan_change_reasons") or raw.get("plan_change_reason")),
        "final_priority": str(raw.get("final_priority") or ""),
        "severity": str(raw.get("severity") or ""),
        "domain": str(raw.get("domain") or ""),
        "request_type": str(raw.get("request_type") or ""),
        "checklist_progress": raw.get("checklist_progress") if isinstance(raw.get("checklist_progress"), (int, float)) else None,
        "last_comment": str(raw.get("last_comment") or ""),
        "last_comment_author": str(raw.get("last_comment_author") or ""),
        "last_comment_at": raw.get("last_comment_at"),
    }


def _is_active_issue(issue: dict[str, Any]) -> bool:
    category = str(issue.get("status_category") or "").lower()
    if category:
        return category in ACTIVE_STATUS_CATEGORIES
    status = str(issue.get("status") or "").lower()
    return status not in {"done", "closed", "resolved", "cancelled", "canceled"}


def _sum_sp(issues: list[dict[str, Any]]) -> float:
    total = 0.0
    for issue in issues:
        sp = issue.get("story_points")
        if isinstance(sp, (int, float)) and sp > 0:
            total += float(sp)
    return total


def _track_sp_value(issue: dict[str, Any], track: Literal["dev", "test"]) -> Optional[float]:
    field = "story_points_dev" if track == "dev" else "story_points_test"
    return _nullable_story_points_value(issue.get(field))


def _sum_track_sp(issues: list[dict[str, Any]], track: Literal["dev", "test"]) -> float:
    total = 0.0
    for issue in issues:
        sp = _track_sp_value(issue, track)
        if sp is not None:
            total += sp
    return total


def _missing_workload_tracks(issue: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if _track_sp_value(issue, "dev") is None:
        missing.append("dev")
    if _track_sp_value(issue, "test") is None:
        missing.append("test")
    return missing


def _workload_attention_reasons(issue: dict[str, Any]) -> list[str]:
    missing = _missing_workload_tracks(issue)
    if not missing:
        return []
    reasons: list[str] = []
    for track in missing:
        reasons.append(f"нет SP {'Dev' if track == 'dev' else 'Test'}")
    if _story_points_value(issue.get("story_points")) is not None:
        reasons.append("указан только общий SP")
    return reasons


def _is_done_issue(issue: dict[str, Any]) -> bool:
    category = str(issue.get("status_category") or "").lower()
    if category == "done":
        return True
    status = str(issue.get("status") or "").lower()
    return status in {"done", "closed", "resolved", "cancelled", "canceled", "готово"}


def _needs_workload_track_attention(issue: dict[str, Any]) -> bool:
    if _is_done_issue(issue):
        return False
    return bool(_missing_workload_tracks(issue))


def _is_track_estimated(issue: dict[str, Any], track: Literal["dev", "test"]) -> bool:
    return _track_sp_value(issue, track) is not None


def _status_breakdown(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        label = str(issue.get("status") or "Unknown")
        counts[label] = counts.get(label, 0) + 1
    return counts


_UNASSIGNED_ASSIGNEE = "Не назначен"
_JIRA_FIELD_SOURCES = {"jira_field"}
_UNATTRIBUTED_ROLE = "Не атрибутировано"
_PAUSE_STATUS_KEYWORDS = ("пауз", "pause", "on hold", "blocked")
_QA_TEST_STATUS_NAMES = frozenset({"тестирование", "к релизу"})
_QA_WORKLOAD_STATUS_NAMES = _QA_TEST_STATUS_NAMES
_REPORT_IN_TEST_STATUS_NAMES = frozenset({"тестирование", "к тестированию", "к релизу"})
_DONE_STATUS_NAMES = frozenset({"готово", "done", "closed", "resolved", "cancelled", "canceled", "won't do", "wont do"})
_NOT_STARTED_STATUS_NAMES = frozenset({"backlog", "бэклог", "к выполнению", "to do", "todo", "open"})


def _status_tokens(issue: dict[str, Any]) -> tuple[str, str]:
    status = str(issue.get("status") or "").lower().strip()
    category = str(issue.get("status_category") or "").lower()
    return status, category


def _issue_in_test_phase(issue: dict[str, Any]) -> bool:
    """QA attention is required only when the task is already in testing."""
    status, category = _status_tokens(issue)
    if category == "done" or status in _DONE_STATUS_NAMES:
        return False
    return status in _QA_TEST_STATUS_NAMES


def _issue_in_qa_workload_scope(issue: dict[str, Any]) -> bool:
    """QA sprint workload: tasks that reached testing or were completed."""
    status, category = _status_tokens(issue)
    if status in _QA_WORKLOAD_STATUS_NAMES:
        return True
    return category == "done" or status in _DONE_STATUS_NAMES


def _issue_in_qa_active_workload_scope(issue: dict[str, Any]) -> bool:
    """QA active workload: tasks currently in testing or release-ready."""
    status, category = _status_tokens(issue)
    if category == "done" or status in _DONE_STATUS_NAMES:
        return False
    return status in _QA_WORKLOAD_STATUS_NAMES


def _developer_task_summary(issue: dict[str, Any]) -> dict[str, Any]:
    sp = issue.get("story_points")
    return {
        "key": str(issue.get("key") or ""),
        "summary": str(issue.get("summary") or ""),
        "url": str(issue.get("url") or ""),
        "story_points": float(sp) if isinstance(sp, (int, float)) else None,
        "status": str(issue.get("status") or ""),
        "assignee": str(issue.get("assignee") or ""),
        "developer_source": str(issue.get("developer_source") or ""),
    }


def _norm_role_name(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("name") or "").strip()
    return str(payload or "").strip()


def _role_source(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("source") or "").strip()
    return ""


def _jira_role_assignee_name(issue: dict[str, Any], role: str) -> str:
    assignees = issue.get("jira_role_assignees")
    if isinstance(assignees, dict) and role in assignees:
        return str(assignees.get(role) or "").strip()
    contributors = issue.get("role_contributors") if isinstance(issue.get("role_contributors"), dict) else {}
    payload = contributors.get(role)
    if _role_source(payload) in _JIRA_FIELD_SOURCES:
        return _norm_role_name(payload)
    return ""


def _issue_has_jira_role_assignee(issue: dict[str, Any], role: str) -> bool:
    return bool(_jira_role_assignee_name(issue, role))


def _positive_story_points_test(issue: dict[str, Any]) -> float:
    test_sp = _track_sp_value(issue, "test")
    if test_sp is not None and test_sp > 0:
        return test_sp
    return 0.0


def _qa_workload_assignee_name(issue: dict[str, Any], *, horizon: RoleWorkloadHorizon = "active") -> str:
    """QA workload: SP Test must be filled; person from Тестировщик or assignee."""
    if not _issue_in_role_workload_scope(issue, "qa", horizon=horizon):
        return ""
    if _positive_story_points_test(issue) <= 0:
        return ""
    return _jira_role_assignee_name(issue, "qa") or str(issue.get("assignee") or "").strip()


def _role_workload_assignee_name(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> str:
    if role == "qa":
        return _qa_workload_assignee_name(issue, horizon=horizon)
    return _jira_role_assignee_name(issue, role)


def _issue_has_role_workload_attribution(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> bool:
    if role == "qa":
        if not _issue_in_role_workload_scope(issue, "qa", horizon=horizon):
            return False
        test_sp = _track_sp_value(issue, "test")
        if test_sp is None:
            return False
        if test_sp == 0:
            return True
        return bool(_qa_workload_assignee_name(issue, horizon=horizon))
    return bool(_jira_role_assignee_name(issue, role))


def _issue_in_role_workload_scope(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> bool:
    bucket = classify_scope_report_bucket(issue)
    if role in {"front", "back"}:
        if horizon == "active":
            return bucket in {"in_work", "in_test"}
        return bucket != "not_started"
    if role == "qa":
        if horizon == "active":
            return _issue_in_qa_active_workload_scope(issue)
        return _issue_in_qa_workload_scope(issue)
    return False


def _role_bucket_key(name: str) -> str:
    if name == _UNATTRIBUTED_ROLE:
        return name
    return person_bucket_key(name)


def _prefer_display_name(current: str, candidate: str) -> str:
    if not current or current == _UNATTRIBUTED_ROLE:
        return candidate
    if not candidate or candidate == _UNATTRIBUTED_ROLE:
        return current
    return candidate if len(candidate) > len(current) else current


def _issue_requires_jira_role(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> bool:
    if not _issue_in_role_workload_scope(issue, role, horizon=horizon):
        return False
    if role == "qa":
        return True
    if role not in {"front", "back"}:
        return False
    labels = issue.get("labels") if isinstance(issue.get("labels"), list) else []
    from app.utils.jira_role_contributors import required_engineering_roles

    return role in required_engineering_roles(labels)


def _issue_unresolved_reason(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> str:
    if not _issue_requires_jira_role(issue, role, horizon=horizon):
        return ""
    if role == "qa":
        if _issue_has_role_workload_attribution(issue, role, horizon=horizon):
            return ""
        test_sp = _track_sp_value(issue, "test")
        if test_sp is None:
            return "jira_sp_test_empty"
        if test_sp == 0:
            return ""
        return "qa_user_missing"
    if _issue_has_jira_role_assignee(issue, role):
        return ""
    return "jira_field_empty"


def _role_attribution_tier(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> str:
    if not _issue_requires_jira_role(issue, role, horizon=horizon):
        return "none"
    if _issue_has_role_workload_attribution(issue, role, horizon=horizon):
        return "confirmed"
    return "unattributed"


def _parent_role_sp(issue: dict[str, Any], role: str) -> float:
    field_by_role = {
        "front": "story_points_front",
        "back": "story_points_back",
        "qa": "story_points_qa",
    }
    specific = issue.get(field_by_role.get(role, ""))
    if isinstance(specific, (int, float)) and specific > 0:
        return float(specific)
    if role == "qa":
        test_sp = issue.get("story_points_test")
        if isinstance(test_sp, (int, float)) and test_sp > 0:
            return float(test_sp)
    sp = issue.get("story_points")
    if isinstance(sp, (int, float)) and sp > 0:
        return float(sp)
    return 0.0


def _role_workload_slices(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> list[tuple[str, dict[str, Any]]]:
    if not _issue_requires_jira_role(issue, role, horizon=horizon):
        return []
    name = _role_workload_assignee_name(issue, role, horizon=horizon)
    payload = {"count": 1, "story_points": _role_sp(issue, role, horizon=horizon)}
    if name:
        return [(name, payload)]
    return [(_UNATTRIBUTED_ROLE, {**payload, "story_points": _parent_role_sp(issue, role)})]


def _role_sp(
    issue: dict[str, Any],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> float:
    if not _issue_in_role_workload_scope(issue, role, horizon=horizon):
        return 0.0

    field_by_role = {
        "front": "story_points_front",
        "back": "story_points_back",
        "qa": "story_points_qa",
    }
    specific = issue.get(field_by_role.get(role, ""))
    if isinstance(specific, (int, float)) and specific > 0:
        return float(specific)

    if role == "qa":
        test_sp = issue.get("story_points_test")
        if isinstance(test_sp, (int, float)) and test_sp > 0:
            return float(test_sp)

    sp = issue.get("story_points")
    if isinstance(sp, (int, float)) and sp > 0:
        return float(sp)
    return 0.0


def _role_task_summary(
    issue: dict[str, Any],
    *,
    role: str = "",
    horizon: RoleWorkloadHorizon = "active",
) -> dict[str, Any]:
    summary = _developer_task_summary(issue)
    summary["status_entered_at"] = issue.get("status_entered_at")
    summary["status_changed_at"] = issue.get("status_changed_at")
    summary["updated"] = issue.get("updated")
    summary["role_contributors_list"] = issue.get("role_contributors_list") or []
    summary["front"] = _jira_role_assignee_name(issue, "front")
    summary["back"] = _jira_role_assignee_name(issue, "back")
    summary["qa"] = _qa_workload_assignee_name(issue, horizon=horizon)
    unresolved_roles = (role,) if role else ("front", "back", "qa")
    unresolved = {
        role_key: _issue_unresolved_reason(issue, role_key, horizon=horizon)
        for role_key in unresolved_roles
        if _issue_unresolved_reason(issue, role_key, horizon=horizon)
    }
    if unresolved:
        summary["role_unresolved"] = unresolved
    if role:
        role_sp = _role_sp(issue, role, horizon=horizon)
        summary["story_points"] = role_sp if role_sp > 0 else summary.get("story_points")
    return summary


def _sort_role_workload_tasks(tasks: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    if role == "qa":
        return sorted(
            tasks,
            key=lambda task: (
                -_status_entered_timestamp(task),
                str(task.get("key") or ""),
            ),
        )
    return sorted(
        tasks,
        key=lambda task: (-(task.get("story_points") or 0), str(task.get("key") or "")),
    )


def _role_breakdown(
    issues: list[dict[str, Any]],
    role: str,
    *,
    horizon: RoleWorkloadHorizon = "active",
    max_items: int = 10,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for issue in issues:
        slices = _role_workload_slices(issue, role, horizon=horizon)
        for name, payload in slices:
            bucket_key = _role_bucket_key(name)
            entry = buckets.setdefault(
                bucket_key,
                {"developer": name, "story_points": 0.0, "count": 0, "issues": []},
            )
            entry["developer"] = _prefer_display_name(str(entry.get("developer") or ""), name)
            entry["count"] += int(payload.get("count") or 0)
            entry["story_points"] += float(payload.get("story_points") or 0)
            task_summary = _role_task_summary(
                issue,
                role=role,
                horizon=horizon,
            )
            slice_sp = float(payload.get("story_points") or 0)
            if slice_sp > 0:
                task_summary["story_points"] = slice_sp
            entry["issues"].append(task_summary)

    rows = sorted(
        buckets.values(),
        key=lambda item: (-float(item["story_points"]), -int(item["count"]), str(item["developer"])),
    )
    for row in rows:
        row["issues"] = _sort_role_workload_tasks(row["issues"], role)

    if len(rows) <= max_items:
        return rows

    top = rows[: max_items - 1]
    rest = rows[max_items - 1 :]
    others = {
        "developer": "Прочие",
        "story_points": sum(float(row["story_points"]) for row in rest),
        "count": sum(int(row["count"]) for row in rest),
        "issues": [task for row in rest for task in row["issues"]],
    }
    return top + [others]


def _role_metrics(
    issues: list[dict[str, Any]],
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> dict[str, list[dict[str, Any]]]:
    return {
        "front": _role_breakdown(issues, "front", horizon=horizon),
        "back": _role_breakdown(issues, "back", horizon=horizon),
        "qa": _role_breakdown(issues, "qa", horizon=horizon),
    }


def _role_coverage(
    issues: list[dict[str, Any]],
    *,
    horizon: RoleWorkloadHorizon = "active",
) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    for role in ("front", "back", "qa"):
        total = sum(1 for issue in issues if _issue_requires_jira_role(issue, role, horizon=horizon))
        confirmed = unattributed = 0
        for issue in issues:
            if not _issue_requires_jira_role(issue, role, horizon=horizon):
                continue
            if _issue_has_role_workload_attribution(issue, role, horizon=horizon):
                confirmed += 1
            else:
                unattributed += 1
        role_coverage = {
            "attributed": confirmed,
            "total": total,
            "confirmed": confirmed,
            "estimated": 0,
            "unattributed": unattributed,
            "confirmed_jira": confirmed,
        }
        if role in {"front", "back"}:
            role_coverage["confirmed_gitlab"] = 0
            role_coverage["unresolved_no_gitlab_link"] = 0
            role_coverage["unresolved_ambiguous_role"] = 0
        if role == "qa":
            role_coverage["confirmed_jira_qa"] = confirmed
            role_coverage["unresolved_no_qa_transition"] = unattributed
        coverage[role] = role_coverage
    return coverage


def _developer_breakdown(issues: list[dict[str, Any]], *, max_items: int = 10) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for issue in issues:
        developer = str(issue.get("developer") or "").strip() or _UNASSIGNED_ASSIGNEE
        entry = buckets.setdefault(
            developer,
            {"developer": developer, "story_points": 0.0, "count": 0, "issues": []},
        )
        entry["count"] += 1
        sp = issue.get("story_points")
        if isinstance(sp, (int, float)) and sp > 0:
            entry["story_points"] += float(sp)
        entry["issues"].append(_role_task_summary(issue))

    rows = sorted(
        buckets.values(),
        key=lambda item: (-float(item["story_points"]), -int(item["count"]), str(item["developer"])),
    )
    for row in rows:
        row["issues"] = sorted(
            row["issues"],
            key=lambda task: (-(task.get("story_points") or 0), str(task.get("key") or "")),
        )

    if len(rows) <= max_items:
        return rows

    top = rows[: max_items - 1]
    rest = rows[max_items - 1 :]
    others = {
        "developer": "Прочие",
        "story_points": sum(float(row["story_points"]) for row in rest),
        "count": sum(int(row["count"]) for row in rest),
        "issues": [task for row in rest for task in row["issues"]],
    }
    return top + [others]


def _assignee_breakdown(issues: list[dict[str, Any]], *, max_items: int = 10) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for issue in issues:
        assignee = str(issue.get("assignee") or "").strip() or _UNASSIGNED_ASSIGNEE
        entry = buckets.setdefault(
            assignee,
            {"assignee": assignee, "story_points": 0.0, "count": 0},
        )
        entry["count"] += 1
        sp = issue.get("story_points")
        if isinstance(sp, (int, float)) and sp > 0:
            entry["story_points"] += float(sp)

    rows = sorted(
        buckets.values(),
        key=lambda item: (-float(item["story_points"]), -int(item["count"]), str(item["assignee"])),
    )
    if len(rows) <= max_items:
        return rows

    top = rows[: max_items - 1]
    rest = rows[max_items - 1 :]
    others = {
        "assignee": "Прочие",
        "story_points": sum(float(row["story_points"]) for row in rest),
        "count": sum(int(row["count"]) for row in rest),
    }
    return top + [others]


def _plan_status_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        label = str(issue.get("plan_status") or "").strip() or "Не указан"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _plan_change_reason_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        reasons = issue.get("plan_change_reasons")
        if not isinstance(reasons, list) or not reasons:
            single = str(issue.get("plan_change_reason") or "").strip()
            reasons = [single] if single else []
        for reason in reasons:
            label = str(reason or "").strip()
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def classify_scope_report_bucket(issue: dict[str, Any]) -> Literal["in_work", "in_test", "done", "open_questions", "not_started"]:
    """Bucket an issue for the monthly scope status report."""
    status, category = _status_tokens(issue)
    if category == "done" or status in _DONE_STATUS_NAMES:
        return "done"
    if status == "пауза" or any(token in status for token in _PAUSE_STATUS_KEYWORDS):
        return "open_questions"
    if status in _REPORT_IN_TEST_STATUS_NAMES:
        return "in_test"
    if category == "new" or status in _NOT_STARTED_STATUS_NAMES:
        return "not_started"
    return "in_work"


_JIRA_PRIORITY_RANK = {
    "blocker": 0,
    "highest": 0,
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
    "lowest": 5,
    "minor": 5,
    "trivial": 6,
}


def jira_priority_rank(priority: Any) -> int:
    label = str(priority or "").strip().lower()
    if not label:
        return 99
    if label in _JIRA_PRIORITY_RANK:
        return _JIRA_PRIORITY_RANK[label]
    for token, rank in _JIRA_PRIORITY_RANK.items():
        if token in label:
            return rank
    return 50


def sort_issues_by_jira_priority(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        issues,
        key=lambda issue: (
            jira_priority_rank(issue.get("priority")),
            str(issue.get("key") or ""),
        ),
    )


def _queue_issue_type_rank(issue: dict[str, Any]) -> int:
    issue_type = str(issue.get("issue_type") or "").strip().lower()
    if issue_type in {"story", "user story", "история"}:
        return 0
    return 1


def _status_entered_timestamp(issue: dict[str, Any]) -> float:
    for field in ("status_entered_at", "status_changed_at", "resolution_date", "updated"):
        value = issue.get(field)
        if not value:
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            continue
    return 0.0


def sort_done_issues_by_recent_status(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        issues,
        key=lambda issue: (
            -_status_entered_timestamp(issue),
            jira_priority_rank(issue.get("priority")),
            str(issue.get("key") or ""),
        ),
    )


def sort_report_column_issues(column: str, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if column == "done":
        return sort_done_issues_by_recent_status(issues)
    return sort_issues_by_jira_priority(issues)


def _build_epic_report_section(issues: list[dict[str, Any]], bucket_name: str) -> dict[str, Any]:
    section: dict[str, list[dict[str, Any]]] = {
        "in_work": [],
        "in_test": [],
        "done": [],
    }
    for issue in issues:
        bucket = classify_scope_report_bucket(issue)
        if bucket in {"open_questions", "not_started"}:
            continue
        section[bucket].append({**issue, "bucket": bucket_name})

    for key in section:
        section[key] = sort_report_column_issues(key, section[key])

    counts = {name: len(items) for name, items in section.items()}
    counts["total"] = counts["in_work"] + counts["in_test"] + counts["done"]
    return {**section, "counts": counts}


def merge_scope_issues(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge issue lists by key; later groups win (fresher supplemental fetch)."""
    by_key: dict[str, dict[str, Any]] = {}
    for group in groups:
        for issue in group:
            key = str(issue.get("key") or "")
            if key:
                by_key[key] = issue
    return list(by_key.values())


def pause_supplement_jql(base_jql: str) -> str:
    """JQL to fetch paused tasks for a Plan/Unplan query even if base JQL filters status."""
    base = base_jql.strip()
    if not base:
        return ""
    return (
        f'({base}) AND (status = "Пауза" OR status = "On Hold" OR status = "Pause" OR status = "Blocked")'
    )


def default_scope_sections() -> list[dict[str, Any]]:
    return [
        {"id": "plan", "name": "Plan", "jql": "", "kind": "planned", "order": 0},
        {"id": "unplan", "name": "Unplan", "jql": "", "kind": "unplanned", "order": 1},
    ]


def _normalize_scope_section_kind(raw: Any) -> ScopeSectionKind:
    value = str(raw or "").strip().lower()
    if value in {"planned", "plan"}:
        return "planned"
    return "unplanned"


def normalize_scope_section_config(raw: dict[str, Any], *, fallback_order: int = 0) -> dict[str, Any]:
    section_id = str(raw.get("id") or secrets.token_hex(4)).strip()
    name = str(raw.get("name") or "Секция").strip() or "Секция"
    return {
        "id": section_id,
        "name": name,
        "jql": str(raw.get("jql") or "").strip(),
        "kind": _normalize_scope_section_kind(raw.get("kind")),
        "order": int(raw.get("order") if raw.get("order") is not None else fallback_order),
    }


def normalize_scope_sections(
    raw_sections: Optional[list[dict[str, Any]]],
    *,
    plan_jql: str = "",
    unplan_jql: str = "",
) -> list[dict[str, Any]]:
    if raw_sections:
        normalized = [
            normalize_scope_section_config(section, fallback_order=index)
            for index, section in enumerate(raw_sections)
            if isinstance(section, dict)
        ]
        normalized.sort(key=lambda section: (section["order"], section["name"].lower(), section["id"]))
        for index, section in enumerate(normalized):
            section["order"] = index
        if normalized:
            return normalized

    plan = plan_jql.strip()
    unplan = unplan_jql.strip()
    if plan or unplan:
        sections = []
        if plan:
            sections.append({"id": "plan", "name": "Plan", "jql": plan, "kind": "planned", "order": 0})
        if unplan:
            sections.append({"id": "unplan", "name": "Unplan", "jql": unplan, "kind": "unplanned", "order": len(sections)})
        return sections
    return default_scope_sections()


def sync_legacy_jql_from_sections(sections: list[dict[str, Any]]) -> tuple[str, str]:
    planned = [section for section in sections if section.get("kind") == "planned"]
    unplanned = [section for section in sections if section.get("kind") == "unplanned"]
    plan_jql = str(planned[0].get("jql") or "") if planned else ""
    unplan_jql = str(unplanned[0].get("jql") or "") if unplanned else ""
    return plan_jql, unplan_jql


def derive_legacy_issue_lists(sections: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plan_issues: list[dict[str, Any]] = []
    unplan_issues: list[dict[str, Any]] = []
    for section in sections:
        section_id = str(section.get("id") or "")
        section_name = str(section.get("name") or section_id)
        section_kind = _normalize_scope_section_kind(section.get("kind"))
        for issue in section.get("issues") or []:
            tagged = {
                **issue,
                "bucket": section_id,
                "section_id": section_id,
                "section_name": section_name,
                "section_kind": section_kind,
            }
            if section_kind == "planned":
                plan_issues.append(tagged)
            else:
                unplan_issues.append(tagged)
    return plan_issues, unplan_issues


def _empty_epic_report_section() -> dict[str, Any]:
    return {
        "in_work": [],
        "in_test": [],
        "done": [],
        "counts": {"in_work": 0, "in_test": 0, "done": 0, "total": 0},
    }


def _aggregate_report_sections(sections: list[dict[str, Any]], kind: ScopeSectionKind) -> dict[str, Any]:
    merged = _empty_epic_report_section()
    for section in sections:
        if section.get("kind") != kind:
            continue
        for key in ("in_work", "in_test", "done"):
            merged[key].extend(section.get(key) or [])
    for key in ("in_work", "in_test", "done"):
        merged[key] = sort_report_column_issues(key, merged[key])
    merged["counts"] = {
        "in_work": len(merged["in_work"]),
        "in_test": len(merged["in_test"]),
        "done": len(merged["done"]),
        "total": len(merged["in_work"]) + len(merged["in_test"]) + len(merged["done"]),
    }
    return merged


def compute_scope_report_from_sections(section_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build status report buckets for arbitrary configured sections."""
    sections_out: list[dict[str, Any]] = []
    open_questions: list[dict[str, Any]] = []

    for section in sorted(section_inputs, key=lambda item: (item.get("order", 0), str(item.get("name") or ""))):
        section_id = str(section.get("id") or "")
        section_name = str(section.get("name") or section_id)
        section_kind = _normalize_scope_section_kind(section.get("kind"))
        issues = section.get("issues") or []
        report = _build_epic_report_section(issues, section_id)
        sections_out.append(
            {
                "id": section_id,
                "name": section_name,
                "kind": section_kind,
                "order": int(section.get("order") or 0),
                **report,
            }
        )
        for issue in issues:
            if classify_scope_report_bucket(issue) != "open_questions":
                continue
            entry = {
                **issue,
                "bucket": section_id,
                "section_id": section_id,
                "section_name": section_name,
                "section_kind": section_kind,
            }
            entry["comment"] = str(issue.get("last_comment") or "")
            entry["comment_author"] = str(issue.get("last_comment_author") or "")
            entry["comment_at"] = issue.get("last_comment_at")
            open_questions.append(entry)

    open_questions.sort(
        key=lambda issue: (
            0 if issue.get("section_kind") == "planned" else 1,
            int(next((section.get("order", 99) for section in section_inputs if section.get("id") == issue.get("section_id")), 99)),
            jira_priority_rank(issue.get("priority")),
            str(issue.get("key") or ""),
        )
    )

    plan = _aggregate_report_sections(sections_out, "planned")
    unplan = _aggregate_report_sections(sections_out, "unplanned")
    aggregate_counts = {
        "in_work": plan["counts"]["in_work"] + unplan["counts"]["in_work"],
        "in_test": plan["counts"]["in_test"] + unplan["counts"]["in_test"],
        "done": plan["counts"]["done"] + unplan["counts"]["done"],
        "open_questions": len(open_questions),
    }

    return {
        "sections": sections_out,
        "plan": plan,
        "unplan": unplan,
        "open_questions": open_questions,
        "counts": aggregate_counts,
    }


def compute_scope_report(
    plan_issues: list[dict[str, Any]],
    unplan_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Legacy wrapper for Plan/Unplan-only boards."""
    return compute_scope_report_from_sections(
        [
            {"id": "plan", "name": "Plan", "kind": "planned", "order": 0, "issues": plan_issues},
            {"id": "unplan", "name": "Unplan", "kind": "unplanned", "order": 1, "issues": unplan_issues},
        ]
    )


PriorityQueueKind = Literal["todo", "test"]

_PRIORITY_QUEUE_LABELS = {
    "todo": "Задачи к выполнению",
    "test": "Задачи к тестированию",
}

_QUEUE_MILESTONE_STATUS_TARGETS: dict[PriorityQueueKind, tuple[str, ...]] = {
    "todo": (
        "К выполнению",
        "Ready for Dev",
        "Ready for Development",
        "In Progress",
        "В работе",
        "To Do",
    ),
    "test": (
        "К тестированию",
        "Testing",
        "Тестирование",
        "In Test",
        "To Test",
        "QA",
    ),
}


def priority_queue_milestone_targets(kind: PriorityQueueKind) -> list[str]:
    return list(_QUEUE_MILESTONE_STATUS_TARGETS.get(kind, ()))


def priority_queue_label(kind: PriorityQueueKind) -> str:
    return _PRIORITY_QUEUE_LABELS[kind]


def _queue_issues_by_key(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for issue in issues:
        key = str(issue.get("key") or "")
        if key:
            index[key] = issue
    return index


def _append_queue_history(history: list[dict[str, Any]], entry: dict[str, Any], *, limit: int = 100) -> list[dict[str, Any]]:
    next_history = [entry, *history]
    return next_history[:limit]


def _queue_issue_milestone_at(issue: dict[str, Any]) -> Optional[str]:
    value = issue.get("status_entered_at")
    return str(value) if value else None


def _scope_binding_milestone_at(issue: dict[str, Any]) -> Optional[str]:
    value = issue.get("epic_linked_at")
    return str(value) if value else None


def _rebuild_queue_appeared_history(
    issues: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    queue_label: str,
) -> list[dict[str, Any]]:
    """Rebuild appeared entries from Jira milestone dates on current queue issues."""
    preserved = [
        entry
        for entry in history
        if entry.get("type") not in {"appeared", "refresh"}
    ]
    appeared: list[dict[str, Any]] = []
    for issue in issues:
        key = str(issue.get("key") or "")
        milestone_at = _queue_issue_milestone_at(issue)
        if not key or not milestone_at:
            continue
        status_name = str(issue.get("status") or "")
        appeared.append(
            {
                "type": "appeared",
                "at": milestone_at,
                "by": "Jira",
                "issue_key": key,
                "status_name": status_name,
                "message": (
                    f"{queue_label}: {key} перешла в «{status_name}»"
                    if status_name
                    else f"{queue_label}: {key} изменила статус"
                ),
            }
        )
    appeared.sort(key=lambda entry: str(entry.get("at") or ""), reverse=True)
    return appeared + preserved


def merge_priority_queue(
    fetched_issues: list[dict[str, Any]],
    previous_queue: Optional[dict[str, Any]],
    *,
    queue_label: str,
    refreshed_at: str,
) -> dict[str, Any]:
    """Merge Jira fetch into a grooming queue while preserving manual order."""
    fresh_by_key = _queue_issues_by_key(fetched_issues)
    previous = previous_queue or {}
    previous_by_key = _queue_issues_by_key(previous.get("issues") or [])

    merged_by_key: dict[str, dict[str, Any]] = {}
    for key, issue in fresh_by_key.items():
        merged = {**issue}
        prev_issue = previous_by_key.get(key)
        if prev_issue:
            for field in ("grooming_comment", "grooming_comment_at", "grooming_comment_by"):
                if prev_issue.get(field):
                    merged[field] = prev_issue[field]
        merged_by_key[key] = merged

    previous_order = [str(key) for key in previous.get("order") or [] if str(key) in merged_by_key]
    new_keys = sorted(
        [key for key in merged_by_key if key not in previous_order],
        key=lambda key: (
            _queue_issue_type_rank(merged_by_key[key]),
            jira_priority_rank(merged_by_key[key].get("priority")),
            key,
        ),
    )
    raw_order = new_keys + previous_order
    order = [key for key in raw_order if _queue_issue_type_rank(merged_by_key[key]) == 0] + [
        key for key in raw_order if _queue_issue_type_rank(merged_by_key[key]) != 0
    ]
    issues = [merged_by_key[key] for key in order]
    history = list(previous.get("history") or [])
    filter_seen_at = dict(previous.get("filter_seen_at") or {})

    for issue in issues:
        key = str(issue.get("key") or "")
        milestone_at = _queue_issue_milestone_at(issue)
        if key and milestone_at:
            filter_seen_at[key] = filter_seen_at.get(key) or milestone_at

    history = _rebuild_queue_appeared_history(issues, history, queue_label=queue_label)

    return {
        "order": order,
        "issues": issues,
        "history": history,
        "filter_seen_at": filter_seen_at,
    }


def _queue_order_keys(queue: dict[str, Any]) -> list[str]:
    order = queue.get("order")
    if isinstance(order, list) and order:
        return [str(key) for key in order if key]
    return [str(issue.get("key") or "") for issue in queue.get("issues") or [] if issue.get("key")]


def _detect_moved_key(previous_order: list[str], next_order: list[str]) -> tuple[Optional[str], Optional[int], Optional[int]]:
    if previous_order == next_order:
        return None, None, None
    prev_index = {key: index for index, key in enumerate(previous_order)}
    next_index = {key: index for index, key in enumerate(next_order)}
    moved_candidates: list[tuple[int, str, int, int]] = []
    for key in next_order:
        if key not in prev_index:
            continue
        from_index = prev_index[key]
        to_index = next_index[key]
        if from_index != to_index:
            moved_candidates.append((abs(from_index - to_index), key, from_index, to_index))
    if not moved_candidates:
        return None, None, None
    moved_candidates.sort(key=lambda item: (-item[0], item[1]))
    _, key, from_index, to_index = moved_candidates[0]
    return key, from_index, to_index


def apply_priority_queue_reorder(
    queue: dict[str, Any],
    *,
    order: list[str],
    comment: str,
    actor_name: str,
    changed_at: str,
    queue_label: str,
    moved_key: Optional[str] = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(queue or {})
    current_order = _queue_order_keys(updated)
    normalized_order = [str(key).upper() for key in order if str(key).strip()]
    current_set = {key.upper() for key in current_order}
    next_set = set(normalized_order)
    if current_set != next_set or len(normalized_order) != len(current_order):
        raise ValueError("Order must include exactly the same issue keys as the current queue")

    by_key = _queue_issues_by_key(updated.get("issues") or [])
    by_key_upper = {str(key).upper(): issue for key, issue in by_key.items()}
    canonical_order: list[str] = []
    for key in normalized_order:
        issue = by_key_upper.get(key)
        if issue:
            canonical_order.append(str(issue.get("key") or key))
    moved_key_resolved, from_index, to_index = _detect_moved_key(current_order, canonical_order)
    if moved_key:
        explicit = moved_key.strip().upper()
        if explicit in {str(key).upper() for key in canonical_order}:
            moved_key_resolved = explicit
            from_index = current_order.index(next(key for key in current_order if str(key).upper() == explicit))
            to_index = canonical_order.index(next(key for key in canonical_order if str(key).upper() == explicit))
    if moved_key_resolved:
        lookup = by_key_upper.get(moved_key_resolved.upper()) or by_key.get(moved_key_resolved)
        if lookup:
            lookup["grooming_comment"] = comment
            lookup["grooming_comment_by"] = actor_name
            lookup["grooming_comment_at"] = changed_at

    message = f"{queue_label}: изменён порядок"
    if moved_key_resolved and from_index is not None and to_index is not None:
        message = f"{queue_label}: {moved_key_resolved} {from_index + 1} → {to_index + 1}"

    history = _append_queue_history(
        list(updated.get("history") or []),
        {
            "type": "reorder",
            "at": changed_at,
            "by": actor_name,
            "comment": comment,
            "issue_key": moved_key_resolved,
            "from_index": from_index,
            "to_index": to_index,
            "order": canonical_order,
            "message": message,
        },
    )
    updated["order"] = canonical_order
    updated["issues"] = [by_key_upper[str(key).upper()] for key in canonical_order if str(key).upper() in by_key_upper]
    updated["history"] = history
    return updated


def apply_priority_queue_comment(
    queue: dict[str, Any],
    *,
    issue_key: str,
    comment: str,
    actor_name: str,
    changed_at: str,
    queue_label: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(queue or {})
    target = issue_key.upper()
    found = False
    for issue in updated.get("issues") or []:
        if str(issue.get("key") or "").upper() != target:
            continue
        issue["grooming_comment"] = comment
        issue["grooming_comment_by"] = actor_name
        issue["grooming_comment_at"] = changed_at
        found = True
        break
    if not found:
        raise ValueError("Issue not found in queue")

    history = _append_queue_history(
        list(updated.get("history") or []),
        {
            "type": "comment",
            "at": changed_at,
            "by": actor_name,
            "comment": comment,
            "issue_key": issue_key.upper(),
            "message": f"{queue_label}: комментарий к {issue_key.upper()}",
        },
    )
    updated["history"] = history
    return updated


def compute_scope_metrics_from_sections(
    capacity_sp: float,
    sections: list[dict[str, Any]],
    month: str,
    *,
    workload_mode: Optional[str] = None,
    capacity_sp_dev: Optional[float] = None,
    capacity_sp_test: Optional[float] = None,
) -> dict[str, Any]:
    """Compute buffer / intake metrics for configured scope sections."""
    mode = normalise_workload_mode(workload_mode)
    split_mode = mode == "sp_dev_test"
    capacity = max(0.0, float(capacity_sp))
    capacity_dev = max(0.0, float(capacity_sp_dev if capacity_sp_dev is not None else capacity_sp))
    capacity_test = max(0.0, float(capacity_sp_test if capacity_sp_test is not None else capacity_sp))
    planned_issues: list[dict[str, Any]] = []
    unplanned_issues: list[dict[str, Any]] = []
    section_metrics: list[dict[str, Any]] = []

    for section in sorted(sections, key=lambda item: (item.get("order", 0), str(item.get("name") or ""))):
        section_id = str(section.get("id") or "")
        section_name = str(section.get("name") or section_id)
        section_kind = _normalize_scope_section_kind(section.get("kind"))
        issues = section.get("issues") or []
        tagged = [
            {
                **issue,
                "bucket": section_id,
                "section_id": section_id,
                "section_name": section_name,
                "section_kind": section_kind,
            }
            for issue in issues
        ]
        if section_kind == "planned":
            planned_issues.extend(tagged)
        else:
            unplanned_issues.extend(tagged)
        section_metrics.append(
            {
                "id": section_id,
                "name": section_name,
                "kind": section_kind,
                "order": int(section.get("order") or 0),
                "story_points": _sum_sp(issues),
                "count": len(issues),
                "by_status": _status_breakdown(issues),
            }
        )

    plan_sp = _sum_sp(planned_issues)
    unplan_sp = _sum_sp(unplanned_issues)
    plan_dev_sp = _sum_track_sp(planned_issues, "dev")
    unplan_dev_sp = _sum_track_sp(unplanned_issues, "dev")
    plan_test_sp = _sum_track_sp(planned_issues, "test")
    unplan_test_sp = _sum_track_sp(unplanned_issues, "test")

    if split_mode:
        plan_sp = plan_dev_sp + plan_test_sp
        unplan_sp = unplan_dev_sp + unplan_test_sp

    buffer_sp = capacity - plan_sp - unplan_sp
    buffer_dev_sp = capacity_dev - plan_dev_sp - unplan_dev_sp
    buffer_test_sp = capacity_test - plan_test_sp - unplan_test_sp
    all_issues = planned_issues + unplanned_issues

    unestimated_tasks: list[dict[str, Any]] = []
    scope_creep_count = 0
    for issue in all_issues:
        bucket = issue.get("bucket")
        normalized = {k: v for k, v in issue.items() if k not in {"bucket", "section_id", "section_name", "section_kind"}}
        if _needs_workload_track_attention(normalized) if split_mode else _is_active_issue(normalized):
            if split_mode:
                missing_tracks = _missing_workload_tracks(normalized)
                if missing_tracks:
                    unestimated_tasks.append(
                        {
                            **normalized,
                            "bucket": bucket,
                            "section_id": issue.get("section_id"),
                            "section_name": issue.get("section_name"),
                            "section_kind": issue.get("section_kind"),
                            "missing_tracks": missing_tracks,
                            "workload_attention_reasons": _workload_attention_reasons(normalized),
                            "estimated": False,
                        }
                    )
            elif not normalized.get("estimated"):
                unestimated_tasks.append(
                    {
                        **normalized,
                        "bucket": bucket,
                        "section_id": issue.get("section_id"),
                        "section_name": issue.get("section_name"),
                        "section_kind": issue.get("section_kind"),
                    }
                )
        created = normalized.get("created")
        if is_scope_creep(str(created) if created else None, month):
            scope_creep_count += 1

    track_buffers = [buffer_dev_sp, buffer_test_sp] if split_mode else [buffer_sp]
    worst_buffer = min(track_buffers) if track_buffers else buffer_sp

    def _track_intake_risk(buffer: float, track_capacity: float) -> IntakeStatus:
        if buffer <= 0:
            return "stop"
        if track_capacity > 0 and buffer <= track_capacity * 0.2:
            return "warning"
        return "ok"

    if split_mode:
        dev_risk = _track_intake_risk(buffer_dev_sp, capacity_dev)
        test_risk = _track_intake_risk(buffer_test_sp, capacity_test)
        if dev_risk == "stop" or test_risk == "stop":
            buffer_risk: IntakeStatus = "stop"
        elif dev_risk == "warning" or test_risk == "warning":
            buffer_risk = "warning"
        else:
            buffer_risk = "ok"
    else:
        buffer_risk = _track_intake_risk(buffer_sp, capacity)

    if buffer_risk == "stop":
        intake_status: IntakeStatus = "stop"
    elif unestimated_tasks or buffer_risk == "warning":
        intake_status = "warning"
    else:
        intake_status = "ok"

    overfill_sp = max(0.0, plan_sp + unplan_sp - capacity)
    overfill_dev_sp = max(0.0, plan_dev_sp + unplan_dev_sp - capacity_dev)
    overfill_test_sp = max(0.0, plan_test_sp + unplan_test_sp - capacity_test)

    return {
        "workload_mode": mode,
        "capacity_sp": capacity,
        "capacity_sp_dev": capacity_dev if split_mode else None,
        "capacity_sp_test": capacity_test if split_mode else None,
        "plan_sp": plan_sp,
        "unplan_sp": unplan_sp,
        "buffer_sp": buffer_sp if not split_mode else min(buffer_dev_sp, buffer_test_sp),
        "overfill_sp": overfill_sp if not split_mode else max(overfill_dev_sp, overfill_test_sp),
        "plan_dev_sp": plan_dev_sp,
        "unplan_dev_sp": unplan_dev_sp,
        "buffer_dev_sp": buffer_dev_sp,
        "overfill_dev_sp": overfill_dev_sp,
        "plan_test_sp": plan_test_sp,
        "unplan_test_sp": unplan_test_sp,
        "buffer_test_sp": buffer_test_sp,
        "overfill_test_sp": overfill_test_sp,
        "intake_status": intake_status,
        "plan_count": len(planned_issues),
        "unplan_count": len(unplanned_issues),
        "unestimated_count": len(unestimated_tasks),
        "unestimated_tasks": unestimated_tasks,
        "scope_creep_count": scope_creep_count,
        "plan_by_status": _status_breakdown(planned_issues),
        "unplan_by_status": _status_breakdown(unplanned_issues),
        "plan_by_assignee": _assignee_breakdown(planned_issues),
        "unplan_by_assignee": _assignee_breakdown(unplanned_issues),
        "plan_by_developer": _developer_breakdown(planned_issues),
        "unplan_by_developer": _developer_breakdown(unplanned_issues),
        "plan_by_role": _role_metrics(planned_issues, horizon="active"),
        "unplan_by_role": _role_metrics(unplanned_issues, horizon="active"),
        "plan_by_role_sprint": _role_metrics(planned_issues, horizon="sprint"),
        "unplan_by_role_sprint": _role_metrics(unplanned_issues, horizon="sprint"),
        "plan_role_coverage": _role_coverage(planned_issues, horizon="active"),
        "unplan_role_coverage": _role_coverage(unplanned_issues, horizon="active"),
        "plan_role_coverage_sprint": _role_coverage(planned_issues, horizon="sprint"),
        "unplan_role_coverage_sprint": _role_coverage(unplanned_issues, horizon="sprint"),
        "plan_status_counts": _plan_status_counts(all_issues),
        "plan_change_reason_counts": _plan_change_reason_counts(all_issues),
        "sections": section_metrics,
        "section_count": len(section_metrics),
        "month": month,
        "month_start": month_start_iso(month),
    }


def refresh_scope_snapshot_metrics(
    snapshot: dict[str, Any],
    *,
    capacity_sp: float,
    month: str,
    workload_mode: Optional[str] = None,
    capacity_sp_dev: Optional[float] = None,
    capacity_sp_test: Optional[float] = None,
) -> dict[str, Any]:
    """Recompute metrics block from an existing snapshot without refetching Jira."""
    sections = snapshot.get("sections")
    if sections:
        metrics = compute_scope_metrics_from_sections(
            capacity_sp,
            sections,
            month,
            workload_mode=workload_mode,
            capacity_sp_dev=capacity_sp_dev,
            capacity_sp_test=capacity_sp_test,
        )
    else:
        metrics = compute_scope_metrics(
            capacity_sp,
            snapshot.get("plan_issues") or [],
            snapshot.get("unplan_issues") or [],
            month,
            workload_mode=workload_mode,
            capacity_sp_dev=capacity_sp_dev,
            capacity_sp_test=capacity_sp_test,
        )
    return {**snapshot, "metrics": metrics}


def compute_scope_metrics(
    capacity_sp: float,
    plan_issues: list[dict[str, Any]],
    unplan_issues: list[dict[str, Any]],
    month: str,
    *,
    workload_mode: Optional[str] = None,
    capacity_sp_dev: Optional[float] = None,
    capacity_sp_test: Optional[float] = None,
) -> dict[str, Any]:
    """Legacy wrapper for Plan/Unplan-only boards."""
    return compute_scope_metrics_from_sections(
        capacity_sp,
        [
            {"id": "plan", "name": "Plan", "kind": "planned", "order": 0, "issues": plan_issues},
            {"id": "unplan", "name": "Unplan", "kind": "unplanned", "order": 1, "issues": unplan_issues},
        ],
        month,
        workload_mode=workload_mode,
        capacity_sp_dev=capacity_sp_dev,
        capacity_sp_test=capacity_sp_test,
    )


def build_scope_snapshot(
    *,
    metrics: dict[str, Any],
    refreshed_at: str,
    previous_snapshot: Optional[dict[str, Any]] = None,
    sections: Optional[list[dict[str, Any]]] = None,
    plan_issues: Optional[list[dict[str, Any]]] = None,
    unplan_issues: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    resolved_sections = sections
    if resolved_sections is None:
        resolved_sections = [
            {"id": "plan", "name": "Plan", "kind": "planned", "order": 0, "issues": plan_issues or []},
            {"id": "unplan", "name": "Unplan", "kind": "unplanned", "order": 1, "issues": unplan_issues or []},
        ]
    legacy_plan, legacy_unplan = derive_legacy_issue_lists(resolved_sections)
    resolved_plan = plan_issues if plan_issues is not None else legacy_plan
    resolved_unplan = unplan_issues if unplan_issues is not None else legacy_unplan
    delta_pack = compute_scope_refresh_delta(
        previous_snapshot,
        resolved_plan,
        resolved_unplan,
        metrics,
        sections=resolved_sections,
    )
    snapshot_sections = [
        {
            "id": section.get("id"),
            "name": section.get("name"),
            "kind": section.get("kind"),
            "order": section.get("order"),
            "issues": section.get("issues") or [],
        }
        for section in resolved_sections
    ]
    entry = {
        "at": refreshed_at,
        "delta": delta_pack["delta"],
        "events": delta_pack["events"][:30],
        "metrics_summary": {
            "plan_sp": metrics["plan_sp"],
            "unplan_sp": metrics["unplan_sp"],
            "buffer_sp": metrics["buffer_sp"],
            "plan_count": metrics["plan_count"],
            "unplan_count": metrics["unplan_count"],
            "intake_status": metrics["intake_status"],
        },
    }
    refresh_log = [entry]
    if previous_snapshot:
        refresh_log.extend((previous_snapshot.get("refresh_log") or [])[:14])

    return {
        "sections": snapshot_sections,
        "plan_issues": resolved_plan,
        "unplan_issues": resolved_unplan,
        "metrics": metrics,
        "report": compute_scope_report_from_sections(resolved_sections),
        "jira_role_fields_configured": jira_role_fields_configured(),
        "refreshed_at": refreshed_at,
        "delta": delta_pack["delta"],
        "events": delta_pack["events"],
        "refresh_log": refresh_log,
    }


def _issues_by_key_from_sections(sections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for section in sections:
        section_id = str(section.get("id") or "")
        for issue in section.get("issues") or []:
            key = str(issue.get("key") or "")
            if key:
                index[key] = {
                    **issue,
                    "bucket": section_id,
                    "section_id": section_id,
                    "section_name": section.get("name"),
                    "section_kind": section.get("kind"),
                }
    return index


def _issues_by_key(
    plan_issues: list[dict[str, Any]],
    unplan_issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for issue in plan_issues:
        key = str(issue.get("key") or "")
        if key:
            index[key] = {**issue, "bucket": "plan"}
    for issue in unplan_issues:
        key = str(issue.get("key") or "")
        if key:
            index[key] = {**issue, "bucket": "unplan"}
    return index


def _sp_label(sp: Any) -> str:
    if isinstance(sp, (int, float)) and sp > 0:
        rounded = round(float(sp) * 10) / 10
        text = str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
        return f"{text} SP"
    return "без SP"


def _format_scope_event_date(iso: str) -> str:
    value = str(iso or "").strip()
    if not value:
        return ""
    try:
        from datetime import datetime

        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).strftime("%d.%m.%y")
    except ValueError:
        return value[:10]


def compute_scope_refresh_delta(
    previous_snapshot: Optional[dict[str, Any]],
    plan_issues: list[dict[str, Any]],
    unplan_issues: list[dict[str, Any]],
    metrics: dict[str, Any],
    *,
    sections: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Compare against the previous snapshot and build human-readable change events."""
    if not previous_snapshot or not previous_snapshot.get("metrics"):
        return {
            "delta": None,
            "events": [
                {
                    "type": "baseline",
                    "message": (
                        f"Первый снимок: плановый {_sp_label(metrics['plan_sp'])}, "
                        f"внеплановый {_sp_label(metrics['unplan_sp'])}, "
                        f"буфер {_sp_label(metrics['buffer_sp'])}"
                    ),
                }
            ],
        }

    prev_metrics = previous_snapshot["metrics"]
    prev_sections = previous_snapshot.get("sections") or []
    if prev_sections:
        prev_keys = _issues_by_key_from_sections(prev_sections)
    else:
        prev_keys = _issues_by_key(
            previous_snapshot.get("plan_issues") or [],
            previous_snapshot.get("unplan_issues") or [],
        )
    if sections:
        curr_keys = _issues_by_key_from_sections(sections)
    else:
        curr_keys = _issues_by_key(plan_issues, unplan_issues)

    delta = {
        "plan_sp": round(float(metrics["plan_sp"]) - float(prev_metrics.get("plan_sp") or 0), 2),
        "unplan_sp": round(float(metrics["unplan_sp"]) - float(prev_metrics.get("unplan_sp") or 0), 2),
        "buffer_sp": round(float(metrics["buffer_sp"]) - float(prev_metrics.get("buffer_sp") or 0), 2),
        "plan_count": int(metrics["plan_count"]) - int(prev_metrics.get("plan_count") or 0),
        "unplan_count": int(metrics["unplan_count"]) - int(prev_metrics.get("unplan_count") or 0),
        "from": {
            "plan_sp": float(prev_metrics.get("plan_sp") or 0),
            "unplan_sp": float(prev_metrics.get("unplan_sp") or 0),
            "buffer_sp": float(prev_metrics.get("buffer_sp") or 0),
            "plan_count": int(prev_metrics.get("plan_count") or 0),
            "unplan_count": int(prev_metrics.get("unplan_count") or 0),
        },
        "to": {
            "plan_sp": float(metrics["plan_sp"]),
            "unplan_sp": float(metrics["unplan_sp"]),
            "buffer_sp": float(metrics["buffer_sp"]),
            "plan_count": int(metrics["plan_count"]),
            "unplan_count": int(metrics["unplan_count"]),
        },
    }

    events: list[dict[str, Any]] = []
    buf_from = float(prev_metrics.get("buffer_sp") or 0)
    buf_to = float(metrics["buffer_sp"])
    parts: list[str] = []
    if delta["plan_sp"]:
        parts.append(f"Плановый {delta['plan_sp']:+.0f} SP")
    if delta["unplan_sp"]:
        parts.append(f"Внеплановый {delta['unplan_sp']:+.0f} SP")
    if delta["plan_count"] or delta["unplan_count"]:
        parts.append(
            f"задач {prev_metrics.get('plan_count', 0)}+{prev_metrics.get('unplan_count', 0)}"
            f" → {metrics['plan_count']}+{metrics['unplan_count']}"
        )
    if parts or buf_from != buf_to:
        events.append(
            {
                "type": "summary",
                "message": f"Буфер {buf_from:.0f} → {buf_to:.0f} SP"
                + (f" ({', '.join(parts)})" if parts else ""),
                "buffer_from": buf_from,
                "buffer_to": buf_to,
            }
        )

    for key, issue in curr_keys.items():
        if key not in prev_keys:
            bound_at = _scope_binding_milestone_at(issue)
            events.append(
                {
                    "type": "added",
                    "key": key,
                    "bucket": issue.get("bucket"),
                    "story_points": issue.get("story_points"),
                    "summary": issue.get("summary", key),
                    "at": bound_at,
                    "message": (
                        f"+ {key} привязана {_format_scope_event_date(bound_at)} "
                        f"({issue.get('section_name') or issue.get('bucket')}): {_sp_label(issue.get('story_points'))}"
                        if bound_at
                        else f"+ {key} ({issue.get('section_name') or issue.get('bucket')}): {_sp_label(issue.get('story_points'))}"
                    ),
                }
            )

    for key, issue in prev_keys.items():
        if key not in curr_keys:
            events.append(
                {
                    "type": "removed",
                    "key": key,
                    "bucket": issue.get("bucket"),
                    "message": f"− {key} убрана из {issue.get('section_name') or issue.get('bucket')}",
                }
            )

    for key, issue in curr_keys.items():
        prev_issue = prev_keys.get(key)
        if not prev_issue:
            continue
        prev_sp = prev_issue.get("story_points")
        curr_sp = issue.get("story_points")
        if prev_sp != curr_sp:
            events.append(
                {
                    "type": "sp_changed",
                    "key": key,
                    "bucket": issue.get("bucket"),
                    "from_sp": prev_sp,
                    "to_sp": curr_sp,
                    "message": f"↔ {key}: {_sp_label(prev_sp)} → {_sp_label(curr_sp)}",
                }
            )

    if not events:
        events.append({"type": "unchanged", "message": "Без изменений с прошлого обновления"})

    return {"delta": delta, "events": events}


ScopeReportType = Literal["monthly", "release"]
_RELEASE_TEAM_MARKERS = ("ios", "android", "igaming")


def infer_scope_report_type(team_slug: Optional[str] = None, team_name: Optional[str] = None) -> ScopeReportType:
    """Mobile release teams use the release report template."""
    haystack = f"{team_slug or ''} {team_name or ''}".casefold()
    if any(marker in haystack for marker in _RELEASE_TEAM_MARKERS):
        return "release"
    return "monthly"


def parse_release_jql(jql: str) -> dict[str, str]:
    """Best-effort parse of project/fixVersion from a release JQL."""
    cleaned = (jql or "").strip()
    parsed: dict[str, str] = {"jql": cleaned}
    if not cleaned:
        return parsed

    project_match = re.search(r"\bproject\s*=\s*([A-Za-z][A-Za-z0-9_-]*)", cleaned, flags=re.IGNORECASE)
    if project_match:
        parsed["project_key"] = project_match.group(1).upper()

    version_name_match = re.search(r'\bfixVersion\s*=\s*"([^"]+)"', cleaned, flags=re.IGNORECASE)
    if version_name_match:
        parsed["version_name"] = version_name_match.group(1).strip()

    version_name_match_single = re.search(r"\bfixVersion\s*=\s*'([^']+)'", cleaned, flags=re.IGNORECASE)
    if version_name_match_single and "version_name" not in parsed:
        parsed["version_name"] = version_name_match_single.group(1).strip()

    if "version_name" not in parsed and "version_id" not in parsed:
        version_unquoted_match = re.search(r"\bfixVersion\s*=\s*([^\"\s]+)", cleaned, flags=re.IGNORECASE)
        if version_unquoted_match:
            value = version_unquoted_match.group(1).strip()
            if value.isdigit():
                parsed["version_id"] = value
            else:
                parsed["version_name"] = value

    return parsed


def infer_release_version_lookup(
    jql: str,
    issues: Optional[list[dict[str, Any]]] = None,
) -> dict[str, str]:
    """Resolve project/version identifiers for Jira version metadata lookup."""
    parsed = parse_release_jql(jql)
    version_name = (parsed.get("version_name") or "").strip()
    version_id = (parsed.get("version_id") or "").strip()
    if not version_name and not version_id and issues:
        version_name = _dominant_fix_version(issues)
    lookup: dict[str, str] = {}
    project_key = (parsed.get("project_key") or "").strip()
    if project_key:
        lookup["project_key"] = project_key
    if version_id:
        lookup["version_id"] = version_id
    if version_name:
        lookup["version_name"] = version_name
    return lookup


def release_scope_sections(release_jql: str, *, label: Optional[str] = None) -> list[dict[str, Any]]:
    """Single planned section for a mobile release report."""
    cleaned = (release_jql or "").strip()
    parsed = parse_release_jql(cleaned)
    section_name = (label or "").strip() or parsed.get("version_name") or parsed.get("version_id") or "Текущий релиз"
    return [
        {
            "id": "release",
            "name": section_name,
            "jql": cleaned,
            "kind": "planned",
            "order": 0,
        }
    ]


def _dominant_fix_version(issues: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for issue in issues:
        for version in issue.get("fix_versions") or []:
            label = str(version or "").strip()
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _normalize_jira_calendar_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
        return cleaned
    return cleaned


def normalize_version_meta(raw: dict[str, Any], *, project_key: str = "") -> dict[str, Any]:
    """Normalize Jira version API payload for release report UI."""
    version_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    start_date = _normalize_jira_calendar_date(
        raw.get("startDate") or raw.get("userStartDate") or raw.get("start_date")
    )
    release_date = _normalize_jira_calendar_date(
        raw.get("releaseDate") or raw.get("userReleaseDate") or raw.get("release_date")
    )
    resolved_project_key = (project_key or raw.get("project_key") or "").strip().upper()
    return {
        "id": version_id,
        "name": name,
        "released": bool(raw.get("released")),
        "archived": bool(raw.get("archived")),
        "overdue": bool(raw.get("overdue")),
        "start_date": start_date,
        "release_date": release_date,
        "description": str(raw.get("description") or "").strip(),
        "project_key": resolved_project_key,
        "project_id": str(raw.get("projectId") or raw.get("project_id") or "").strip() or None,
    }


def _apply_version_meta(bucket: dict[str, Any], version_meta: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not version_meta:
        return bucket
    bucket["version_meta"] = version_meta
    if version_meta.get("name"):
        bucket["version_name"] = version_meta["name"]
        if bucket.get("slot") == "current":
            bucket["label"] = version_meta["name"]
    if version_meta.get("id") and not bucket.get("version_id"):
        bucket["version_id"] = version_meta["id"]
    if version_meta.get("project_key") and not bucket.get("project_key"):
        bucket["project_key"] = version_meta["project_key"]
    return bucket


def _issue_type_breakdown(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        label = str(issue.get("issue_type") or "Unknown")
        counts[label] = counts.get(label, 0) + 1
    return counts


def summarize_release_bucket(
    *,
    slot: str,
    label: str,
    jql: str,
    issues: list[dict[str, Any]],
    version_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Summarize one release JQL bucket for the release report UI."""
    parsed = parse_release_jql(jql)
    dominant_version = _dominant_fix_version(issues)
    release_label = (
        (label or "").strip()
        or dominant_version
        or parsed.get("version_name")
        or parsed.get("version_id")
        or "Релиз"
    )
    in_work: list[dict[str, Any]] = []
    in_test: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    open_questions: list[dict[str, Any]] = []
    for issue in issues:
        bucket = classify_scope_report_bucket(issue)
        if bucket == "done":
            done.append(issue)
        elif bucket == "in_test":
            in_test.append(issue)
        elif bucket == "open_questions":
            open_questions.append(issue)
        else:
            in_work.append(issue)

    return _apply_version_meta(
        {
        "slot": slot,
        "label": release_label,
        "jql": (jql or "").strip(),
        "project_key": parsed.get("project_key", ""),
        "version_id": parsed.get("version_id", ""),
        "version_name": dominant_version or parsed.get("version_name", ""),
        "issues": issues,
        "story_points": _sum_sp(issues),
        "counts": {
            "total": len(issues),
            "in_work": len(in_work),
            "in_test": len(in_test),
            "done": len(done),
            "open_questions": len(open_questions),
        },
        "by_status": _status_breakdown(issues),
        "by_issue_type": _issue_type_breakdown(issues),
        "in_work": sort_issues_by_jira_priority(in_work),
        "in_test": sort_issues_by_jira_priority(in_test),
        "done": sort_done_issues_by_recent_status(done),
        "open_questions": sort_issues_by_jira_priority(open_questions),
    },
        version_meta,
    )


def build_release_context(
    *,
    current_jql: str,
    current_issues: list[dict[str, Any]],
    previous_jql: str = "",
    previous_issues: Optional[list[dict[str, Any]]] = None,
    next_jql: str = "",
    next_issues: Optional[list[dict[str, Any]]] = None,
    custom_name: str = "",
    custom_jql: str = "",
    custom_issues: Optional[list[dict[str, Any]]] = None,
    release_queries: Optional[list[dict[str, Any]]] = None,
    release_issues_by_slot: Optional[dict[str, list[dict[str, Any]]]] = None,
    version_meta_by_slot: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build release comparison payload stored on the scope snapshot."""
    meta_map = version_meta_by_slot or {}
    current = summarize_release_bucket(
        slot="current",
        label="",
        jql=current_jql,
        issues=current_issues,
        version_meta=meta_map.get("current"),
    )
    context: dict[str, Any] = {"current": current}
    if (previous_jql or "").strip():
        context["previous"] = summarize_release_bucket(
            slot="previous",
            label="Предыдущий релиз",
            jql=previous_jql,
            issues=previous_issues or [],
            version_meta=meta_map.get("previous"),
        )
    if (next_jql or "").strip():
        context["next"] = summarize_release_bucket(
            slot="next",
            label="Следующий релиз",
            jql=next_jql,
            issues=next_issues or [],
            version_meta=meta_map.get("next"),
        )
    if (custom_jql or "").strip():
        context["custom"] = summarize_release_bucket(
            slot="custom",
            label=(custom_name or "").strip() or "Следующий релиз+",
            jql=custom_jql,
            issues=custom_issues or [],
            version_meta=meta_map.get("custom"),
        )
    releases: list[dict[str, Any]] = []
    issues_by_slot = release_issues_by_slot or {}
    for index, query in enumerate(release_queries or []):
        if not isinstance(query, dict):
            continue
        jql = str(query.get("jql") or "").strip()
        if not jql:
            continue
        slot = str(query.get("id") or "").strip() or f"release-{index + 1}"
        relation = str(query.get("type") or query.get("relation") or "future").strip().lower()
        if relation not in {"past", "future"}:
            relation = "future"
        label = str(query.get("label") or "").strip() or ("Прошедший релиз" if relation == "past" else "Будущий релиз")
        bucket = summarize_release_bucket(
            slot=slot,
            label=label,
            jql=jql,
            issues=issues_by_slot.get(slot) or [],
            version_meta=meta_map.get(slot),
        )
        bucket["relation"] = relation
        bucket["order"] = index
        releases.append(bucket)
    if releases:
        context["releases"] = releases
    return context
