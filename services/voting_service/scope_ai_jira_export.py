"""Build ADF comments and export scope AI summaries to the configured Plan epic."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Optional

from services.voting_service.ai_summary_jira_export import (
    ai_summary_jira_export_enabled,
    export_adf_comment_to_jira,
)

_SCOPE_HASH_FIELDS = (
    "health",
    "summary",
    "buffer_status",
    "whats_critical",
    "whats_bad",
    "recommendations",
    "focus_now",
    "blockers",
    "capacity_assessment",
    "report_assessment",
)

_HEALTH_RU = {
    "green": "Под контролем",
    "yellow": "Есть риски",
    "red": "Критично",
}

_SEVERITY_RU = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
}

_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def normalize_plan_epic_key(value: Optional[str]) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw if _JIRA_KEY_RE.match(raw) else ""


def compute_scope_summary_hash(summary: Mapping[str, Any]) -> str:
    payload = {key: summary.get(key) for key in _SCOPE_HASH_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _text(text: str, *, strong: bool = False) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if strong:
        node["marks"] = [{"type": "strong"}]
    return node


def _paragraph(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(nodes)}


def _heading(text: str, level: int = 3) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [_text(text)],
    }


def _bullet_list(items: list[str]) -> dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [_paragraph(_text(item))]}
            for item in items
            if item
        ],
    }


def _ordered_list(items: list[str]) -> dict[str, Any]:
    return {
        "type": "orderedList",
        "content": [
            {"type": "listItem", "content": [_paragraph(_text(item))]}
            for item in items
            if item
        ],
    }


def build_scope_ai_summary_comment_adf(
    summary: Mapping[str, Any],
    *,
    issue_key: str,
    board_name: str,
    board_month: str,
) -> dict[str, Any]:
    """Render a structured Jira ADF comment for a scope board AI summary."""
    health = str(summary.get("health") or "").strip().lower()
    health_ru = _HEALTH_RU.get(health, health or "—")
    content: list[dict[str, Any]] = [
        _heading("AI-сводка · Отчёт месяца"),
        _paragraph(
            _text(board_name, strong=True),
            _text(f" · {board_month} · {issue_key}"),
        ),
        _paragraph(_text("Статус: ", strong=True), _text(health_ru, strong=True)),
    ]

    main_summary = str(summary.get("summary") or "").strip()
    if main_summary:
        content.append(_heading("Главный вывод", 4))
        content.append(_paragraph(_text(main_summary)))

    recommendations = [
        str(item.get("text") or "").strip()
        for item in (summary.get("recommendations") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if recommendations:
        content.append(_heading("Что сделать на этой неделе", 4))
        content.append(_ordered_list(recommendations))

    focus_now = [str(item).strip() for item in (summary.get("focus_now") or []) if str(item).strip()]
    if focus_now:
        content.append(_heading("Обсудить на ближайшей встрече", 4))
        content.append(_bullet_list(focus_now))

    critical = [str(item).strip() for item in (summary.get("whats_critical") or []) if str(item).strip()]
    if critical:
        content.append(_heading("Требует внимания", 4))
        content.append(_bullet_list(critical))

    blockers = summary.get("blockers") or []
    blocker_lines: list[str] = []
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        title = str(blocker.get("title") or "").strip()
        detail = str(blocker.get("detail") or "").strip()
        severity = _SEVERITY_RU.get(str(blocker.get("severity") or "").strip().lower(), "")
        keys = [str(key).strip() for key in (blocker.get("issue_keys") or []) if str(key).strip()]
        line = title
        if severity:
            line = f"{line} ({severity})" if line else severity
        if detail:
            line = f"{line}: {detail}" if line else detail
        if keys:
            line = f"{line} [{', '.join(keys)}]" if line else ", ".join(keys)
        if line:
            blocker_lines.append(line)
    if blocker_lines:
        content.append(_heading("Блокеры", 4))
        content.append(_bullet_list(blocker_lines))

    capacity = str(summary.get("capacity_assessment") or "").strip()
    if capacity:
        content.append(_heading("Capacity", 4))
        content.append(_paragraph(_text(capacity)))

    generated_at = str(summary.get("generated_at") or "").strip()
    footer_parts = [_text("Сгенерировано Planning Poker")]
    if generated_at:
        footer_parts.extend([_text(" · "), _text(generated_at)])
    content.append(_paragraph(*footer_parts))

    return {"type": "doc", "version": 1, "content": content}


def should_skip_scope_jira_export(
    summary: Mapping[str, Any],
    *,
    previous_export: Optional[Mapping[str, Any]] = None,
) -> bool:
    previous = previous_export or summary.get("jira_export")
    if not isinstance(previous, dict):
        return False
    if previous.get("status") != "ok":
        return False
    return previous.get("hash") == compute_scope_summary_hash(summary)


async def export_scope_ai_summary_to_jira(
    client: Any,
    *,
    issue_key: str,
    summary: Mapping[str, Any],
    board_name: str,
    board_month: str,
) -> dict[str, Any]:
    """Post or update a Jira comment with scope AI summary ADF."""
    summary_hash = compute_scope_summary_hash(summary)
    previous = summary.get("jira_export") if isinstance(summary.get("jira_export"), dict) else None

    if should_skip_scope_jira_export(summary, previous_export=previous):
        return dict(previous or {})

    adf = build_scope_ai_summary_comment_adf(
        summary,
        issue_key=issue_key,
        board_name=board_name,
        board_month=board_month,
    )
    return await export_adf_comment_to_jira(
        client,
        issue_key=issue_key,
        adf=adf,
        content_hash=summary_hash,
        previous_export=previous,
    )
