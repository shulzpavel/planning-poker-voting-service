"""Infer front/back/qa contributors for scope workload reporting."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

from planning_poker_common.jira.text import adf_to_plain_text

_GITLAB_MENTION_RE = re.compile(
    r"^(.+?)\s+mentioned this issue in a\s+(commit|merge request)\s+of\s+(.+?)(?:\s+on branch\s+(.+))?$",
    re.IGNORECASE,
)

_ROLE_ORDER = ("front", "back", "qa")
_PARENT_GITLAB_SOURCES = {"gitlab_mr", "gitlab_commit", "gitlab_api_mr", "gitlab_api_commit"}
_SUBTASK_GITLAB_SOURCES = {
    "subtask_gitlab_mr",
    "subtask_gitlab_commit",
    "subtask_gitlab_api_mr",
    "subtask_gitlab_api_commit",
}
_CONFIRMED_SOURCES = _PARENT_GITLAB_SOURCES | _SUBTASK_GITLAB_SOURCES
_JIRA_FIELD_SOURCES = {"jira_field"}
_ESTIMATED_SOURCES = {"changelog_dev", "testing_comment"}
_ENGINEERING_SOURCES = _CONFIRMED_SOURCES | _ESTIMATED_SOURCES
_QA_SOURCES = {"changelog", "current", "testing_comment"} | _JIRA_FIELD_SOURCES
_TRUSTED_ROLE_SOURCES = {
    "front": _ENGINEERING_SOURCES | _JIRA_FIELD_SOURCES,
    "back": _ENGINEERING_SOURCES | _JIRA_FIELD_SOURCES,
    "qa": _QA_SOURCES,
}
_ENGINEERING_LABELS = {"frontend", "backend"}
_QA_COMMENT_MARKERS = (
    "в рамках тестирования",
    "протестир",
    "задача реализована",
    "проверено:",
)


def _norm(value: Any) -> str:
    return str(value or "").strip()


_PATRONYMIC_SUFFIXES = ("ович", "евич", "овна", "евна", "ична")


def _name_tokens(name: str) -> list[str]:
    return [token.lower() for token in _norm(name).replace(".", " ").split() if token]


def _drop_patronymic(tokens: list[str]) -> list[str]:
    if len(tokens) < 3:
        return tokens
    patronymic = tokens[-1]
    if any(patronymic.endswith(suffix) for suffix in _PATRONYMIC_SUFFIXES):
        return tokens[:-1]
    return tokens


def person_bucket_key(name: str) -> str:
    """Normalize display names so 'Илья Пыхтин' and 'Пыхтин Илья Александрович' share one bucket."""
    tokens = _drop_patronymic(_name_tokens(name))
    if len(tokens) >= 2:
        return " ".join(sorted(tokens[:2]))
    return " ".join(sorted(tokens))


def attribution_tier(source: str) -> str:
    normalized = _norm(source)
    if normalized in _CONFIRMED_SOURCES or normalized in _JIRA_FIELD_SOURCES:
        return "confirmed"
    if normalized in _ESTIMATED_SOURCES:
        return "estimated"
    if normalized in {"changelog", "current"}:
        return "confirmed"
    return "unattributed"


def _comment_text(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    if body is None:
        return ""
    if isinstance(body, str):
        return _norm(body)
    return _norm(adf_to_plain_text(body))


def _comment_author(comment: dict[str, Any]) -> str:
    author = comment.get("author")
    if isinstance(author, dict):
        return _norm(author.get("displayName"))
    return ""


def _gitlab_mention_line(text: str) -> str:
    line = text.split("\n", 1)[0].strip()
    if line.endswith(":"):
        line = line[:-1].strip()
    return line


def role_from_repo_path(path: str) -> Optional[str]:
    lowered = _norm(path).lower()
    if not lowered:
        return None
    if "frontend" in lowered:
        return "front"
    if "backend" in lowered:
        return "back"
    if any(token in lowered for token in ("qa", "quality", "autotest", "e2e", "testing")):
        return "qa"
    return None


def unambiguous_engineering_label(labels: list[str] | None) -> Optional[str]:
    label_set = {str(label).strip().lower() for label in (labels or []) if label}
    front = "frontend" in label_set
    back = "backend" in label_set
    if front and not back:
        return "front"
    if back and not front:
        return "back"
    return None


def required_engineering_roles(labels: list[str] | None) -> set[str]:
    """Roles whose Jira assignee fields must be filled for this issue."""
    label_set = {str(label).strip().lower() for label in (labels or []) if label}
    has_front = "frontend" in label_set
    has_back = "backend" in label_set
    if has_front and not has_back:
        return {"front"}
    if has_back and not has_front:
        return {"back"}
    return {"front", "back"}


def _mention_score(kind: str) -> int:
    return 3 if _norm(kind).lower() == "merge request" else 1


def infer_role_contributors_from_comments(
    comments: list[dict[str, Any]],
    *,
    integration_author: str = "igaming",
) -> dict[str, dict[str, str]]:
    best: dict[str, tuple[str, int, str, str]] = {}
    integration = _norm(integration_author).lower()

    for comment in comments:
        if _comment_author(comment).lower() != integration:
            continue
        text = _gitlab_mention_line(_comment_text(comment))
        match = _GITLAB_MENTION_RE.match(text)
        if not match:
            continue

        person = _norm(match.group(1))
        kind = _norm(match.group(2))
        repo_path = _norm(match.group(3))
        role = role_from_repo_path(repo_path)
        if not role or not person:
            continue

        score = _mention_score(kind)
        created = _norm(comment.get("created"))
        source = "gitlab_mr" if score >= 3 else "gitlab_commit"
        current = best.get(role)
        if current is None or score > current[1] or (score == current[1] and created >= current[3]):
            best[role] = (person, score, source, created)

    return {
        role: {"name": entry[0], "source": entry[2]}
        for role, entry in best.items()
    }


def build_subtask_workload_items(subtasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for subtask in subtasks:
        subtask_key = _norm(subtask.get("key"))
        if not subtask_key:
            continue
        roles = infer_role_contributors_from_comments(subtask.get("comments") if isinstance(subtask.get("comments"), list) else [])
        for role in ("front", "back"):
            payload = roles.get(role)
            if not payload:
                continue
            source = "subtask_gitlab_mr" if payload.get("source") == "gitlab_mr" else "subtask_gitlab_commit"
            items.append(
                {
                    "role": role,
                    "name": _norm(payload.get("name")),
                    "source": source,
                    "subtask_key": subtask_key,
                    "subtask_summary": _norm(subtask.get("summary")),
                }
            )
    return items


def _gitlab_source_score(source: str) -> int:
    scores = {
        "gitlab_api_mr": 5,
        "subtask_gitlab_api_mr": 5,
        "gitlab_mr": 4,
        "subtask_gitlab_mr": 4,
        "gitlab_api_commit": 3,
        "subtask_gitlab_api_commit": 3,
        "gitlab_commit": 2,
        "subtask_gitlab_commit": 2,
    }
    return scores.get(_norm(source), 0)


def _has_gitlab_role(
    *,
    role: str,
    from_comments: Optional[dict[str, dict[str, str]]] = None,
    workload_items: Optional[list[dict[str, str]]] = None,
) -> bool:
    payload = (from_comments or {}).get(role) or {}
    if _norm(payload.get("name")) and _norm(payload.get("source")) in _PARENT_GITLAB_SOURCES:
        return True
    return any(_norm(item.get("role")) == role for item in (workload_items or []))


def build_changelog_dev_fallback(
    *,
    labels: list[str] | None,
    developer: str,
    developer_source: str,
    from_comments: Optional[dict[str, dict[str, str]]] = None,
    workload_items: Optional[list[dict[str, str]]] = None,
) -> dict[str, dict[str, str]]:
    role = unambiguous_engineering_label(labels)
    if not role or developer_source not in {"changelog", "current"} or not _norm(developer):
        return {}
    if _has_gitlab_role(role=role, from_comments=from_comments, workload_items=workload_items):
        return {}

    other_role = "back" if role == "front" else "front"
    if _has_gitlab_role(role=other_role, from_comments=from_comments, workload_items=workload_items):
        if unambiguous_engineering_label(labels) == role:
            return {}

    return {role: {"name": _norm(developer), "source": "changelog_dev"}}


def infer_qa_from_testing_comments(
    comments: list[dict[str, Any]],
    *,
    developer: str = "",
) -> tuple[str, str]:
    developer_key = person_bucket_key(developer) if developer else ""
    ordered = sorted(
        [comment for comment in comments if isinstance(comment, dict)],
        key=lambda item: str(item.get("created") or ""),
        reverse=True,
    )
    for comment in ordered:
        author = _comment_author(comment)
        if not author or author.lower() == "igaming":
            continue
        text = _comment_text(comment).lower()
        if not any(marker in text for marker in _QA_COMMENT_MARKERS):
            continue
        if developer_key and person_bucket_key(author) == developer_key:
            continue
        return author, "testing_comment"
    return "", ""


def _summary_source_for_subtasks(items: list[dict[str, str]], role: str) -> str:
    role_items = [item for item in items if item.get("role") == role]
    for preferred in ("subtask_gitlab_api_mr", "subtask_gitlab_mr", "subtask_gitlab_api_commit", "subtask_gitlab_commit"):
        if any(item.get("source") == preferred for item in role_items):
            return preferred
    return "subtask_gitlab_commit"


def merge_role_contributors(
    *,
    from_comments: Optional[dict[str, dict[str, str]]] = None,
    from_gitlab_api: Optional[dict[str, dict[str, str]]] = None,
    from_jira_fields: Optional[dict[str, dict[str, str]]] = None,
    subtask_workload_items: Optional[list[dict[str, str]]] = None,
    labels: Optional[list[str]] = None,
    developer: str = "",
    developer_source: str = "",
    issue_comments: Optional[list[dict[str, Any]]] = None,
    qa_from_changelog: str = "",
    qa_source: str = "",
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    merged: dict[str, dict[str, str]] = {}
    workload_items = list(subtask_workload_items or [])

    for role, payload in (from_gitlab_api or {}).items():
        if role not in {"front", "back"}:
            continue
        name = _norm(payload.get("name"))
        source = _norm(payload.get("source"))
        if name and source in _PARENT_GITLAB_SOURCES:
            merged[role] = {"name": name, "source": source}

    for role, payload in (from_comments or {}).items():
        if role not in {"front", "back"} or role in merged:
            continue
        name = _norm(payload.get("name"))
        source = _norm(payload.get("source"))
        if name and source in {"gitlab_mr", "gitlab_commit"}:
            merged[role] = {"name": name, "source": source}

    subtask_counts: dict[str, Counter[str]] = {role: Counter() for role in ("front", "back")}
    subtask_best_source: dict[tuple[str, str], str] = {}
    for item in workload_items:
        role = _norm(item.get("role"))
        name = _norm(item.get("name"))
        source = _norm(item.get("source"))
        if role in subtask_counts and name:
            subtask_counts[role][name] += 1
            key = (role, name)
            current = subtask_best_source.get(key)
            if not current or _gitlab_source_score(source) > _gitlab_source_score(current):
                subtask_best_source[key] = source

    for role in ("front", "back"):
        if role in merged or not subtask_counts[role]:
            continue
        name, _count = subtask_counts[role].most_common(1)[0]
        source = subtask_best_source.get((role, name)) or _summary_source_for_subtasks(workload_items, role)
        merged[role] = {"name": name, "source": source}

    for role, payload in (from_jira_fields or {}).items():
        if role not in {"front", "back", "qa"} or role in merged:
            continue
        name = _norm(payload.get("name"))
        if name:
            merged[role] = {"name": name, "source": "jira_field"}

    for role, payload in build_changelog_dev_fallback(
        labels=labels,
        developer=developer,
        developer_source=developer_source,
        from_comments={**(from_comments or {}), **(from_gitlab_api or {})},
        workload_items=workload_items,
    ).items():
        merged.setdefault(role, payload)

    if qa_from_changelog and (qa_source or "changelog") in {"changelog", "current"} and "qa" not in merged:
        merged["qa"] = {"name": qa_from_changelog, "source": qa_source or "changelog"}
    elif "qa" not in merged:
        qa_name, qa_comment_source = infer_qa_from_testing_comments(issue_comments or [], developer=developer)
        if qa_name:
            merged["qa"] = {"name": qa_name, "source": qa_comment_source}

    return merged, workload_items


def trusted_role_name(role_contributors: dict[str, dict[str, str]] | None, role: str) -> str:
    payload = (role_contributors or {}).get(role)
    if not isinstance(payload, dict):
        return ""
    name = _norm(payload.get("name"))
    source = _norm(payload.get("source"))
    if not name or source not in _TRUSTED_ROLE_SOURCES.get(role, set()):
        return ""
    return name


def role_contributors_list(role_contributors: dict[str, dict[str, str]] | None) -> list[dict[str, str]]:
    if not role_contributors:
        return []
    rows: list[dict[str, str]] = []
    for role in _ROLE_ORDER:
        name = trusted_role_name(role_contributors, role)
        if not name:
            continue
        payload = role_contributors.get(role) or {}
        rows.append({"role": role, "name": name, "source": _norm(payload.get("source")) or "unknown"})
    return rows
