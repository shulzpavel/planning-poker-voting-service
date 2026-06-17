"""Build role attribution evidence from GitLab API search results."""

from __future__ import annotations

from typing import Any, Optional

from app.utils.jira_role_contributors import role_from_repo_path

_GITLAB_API_MR = "gitlab_api_mr"
_GITLAB_API_COMMIT = "gitlab_api_commit"
_SUBTASK_GITLAB_API_MR = "subtask_gitlab_api_mr"
_SUBTASK_GITLAB_API_COMMIT = "subtask_gitlab_api_commit"

GITLAB_API_SOURCES = {_GITLAB_API_MR, _GITLAB_API_COMMIT, _SUBTASK_GITLAB_API_MR, _SUBTASK_GITLAB_API_COMMIT}


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _mr_author(row: dict[str, Any]) -> str:
    author = row.get("author")
    if isinstance(author, dict):
        return _norm(author.get("name") or author.get("username"))
    return ""


def _commit_author(row: dict[str, Any]) -> str:
    return _norm(row.get("author_name") or row.get("author_email"))


def _project_path(row: dict[str, Any]) -> str:
    return _norm(row.get("project_path") or row.get("path_with_namespace"))


def _evidence_row(
    *,
    role: str,
    name: str,
    source: str,
    jira_key: str,
    project_path: str,
    source_url: str,
    kind: str,
    subtask_key: str = "",
    subtask_summary: str = "",
) -> dict[str, str]:
    row: dict[str, str] = {
        "role": role,
        "name": name,
        "source": source,
        "jira_key": jira_key,
        "project_path": project_path,
        "source_url": source_url,
        "confidence": "confirmed",
        "kind": kind,
        "gitlab_user": name,
    }
    if subtask_key:
        row["subtask_key"] = subtask_key
    if subtask_summary:
        row["subtask_summary"] = subtask_summary
    return row


def build_gitlab_api_workload_items(
    raw: dict[str, Any],
    *,
    jira_key: str,
    subtask_key: str = "",
    subtask_summary: str = "",
) -> list[dict[str, str]]:
    if not jira_key:
        return []

    mr_source = _SUBTASK_GITLAB_API_MR if subtask_key else _GITLAB_API_MR
    commit_source = _SUBTASK_GITLAB_API_COMMIT if subtask_key else _GITLAB_API_COMMIT
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for row in raw.get("merge_requests") or []:
        if not isinstance(row, dict):
            continue
        project_path = _project_path(row)
        role = role_from_repo_path(project_path)
        name = _mr_author(row)
        if not role or not name or role not in {"front", "back"}:
            continue
        dedupe_key = (role, name, project_path)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(
            _evidence_row(
                role=role,
                name=name,
                source=mr_source,
                jira_key=jira_key,
                project_path=project_path,
                source_url=_norm(row.get("web_url")),
                kind="merge_request",
                subtask_key=subtask_key,
                subtask_summary=subtask_summary,
            )
        )

    for row in raw.get("commits") or []:
        if not isinstance(row, dict):
            continue
        project_path = _project_path(row)
        role = role_from_repo_path(project_path)
        name = _commit_author(row)
        if not role or not name or role not in {"front", "back"}:
            continue
        dedupe_key = (role, name, project_path)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(
            _evidence_row(
                role=role,
                name=name,
                source=commit_source,
                jira_key=jira_key,
                project_path=project_path,
                source_url=_norm(row.get("web_url")),
                kind="commit",
                subtask_key=subtask_key,
                subtask_summary=subtask_summary,
            )
        )

    return items


def build_gitlab_api_contributors(items: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    best: dict[str, tuple[str, int, str]] = {}
    score_by_source = {
        _GITLAB_API_MR: 4,
        _SUBTASK_GITLAB_API_MR: 4,
        _GITLAB_API_COMMIT: 2,
        _SUBTASK_GITLAB_API_COMMIT: 2,
    }
    for item in items:
        role = _norm(item.get("role"))
        name = _norm(item.get("name"))
        source = _norm(item.get("source"))
        if role not in {"front", "back"} or not name or source not in score_by_source:
            continue
        score = score_by_source[source]
        current = best.get(role)
        if current is None or score > current[1]:
            best[role] = (name, score, source)
    return {role: {"name": entry[0], "source": entry[2]} for role, entry in best.items()}


def unresolved_reason_for_role(
    *,
    role: str,
    labels: list[str] | None,
    gitlab_items: list[dict[str, str]],
    comment_gitlab_roles: set[str],
    has_trusted_name: bool,
) -> str:
    if has_trusted_name:
        return ""
    if role not in {"front", "back"}:
        return "unresolved_no_qa_transition" if role == "qa" else ""

    label_set = {str(label).strip().lower() for label in (labels or []) if label}
    in_scope = ("frontend" in label_set and role == "front") or ("backend" in label_set and role == "back")
    gitlab_roles = {item.get("role") for item in gitlab_items}
    has_same_role_evidence = role in gitlab_roles or role in comment_gitlab_roles

    if in_scope and gitlab_items and not has_same_role_evidence:
        opposite_role = "back" if role == "front" else "front"
        if opposite_role in gitlab_roles or opposite_role in comment_gitlab_roles:
            return ""
    if in_scope and comment_gitlab_roles and role not in comment_gitlab_roles and len(comment_gitlab_roles) > 1:
        return "unresolved_ambiguous_role"

    if in_scope or has_same_role_evidence:
        return "unresolved_no_gitlab_link"
    return ""
