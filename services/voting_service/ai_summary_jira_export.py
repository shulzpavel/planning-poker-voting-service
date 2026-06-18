"""Build ADF comments and export session AI summaries to Jira."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_EXPORT_ENV = "AI_SUMMARY_JIRA_EXPORT"
_HASH_FIELDS = (
    "description",
    "methods",
    "complexity",
    "sp_dev",
    "sp_test",
    "sp_final",
    "scale_label",
    "confidence",
    "assumptions",
    "estimation_model",
)

_CONFIDENCE_RU = {"low": "низкая", "medium": "средняя", "high": "высокая"}


def ai_summary_jira_export_enabled() -> bool:
    raw = os.getenv(_EXPORT_ENV, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def compute_summary_hash(summary: Mapping[str, Any]) -> str:
    payload = {key: summary.get(key) for key in _HASH_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def should_skip_jira_export(
    summary: Mapping[str, Any],
    *,
    previous_export: Optional[Mapping[str, Any]] = None,
) -> bool:
    previous = previous_export or summary.get("jira_export")
    if not isinstance(previous, dict):
        return False
    if previous.get("status") != "ok":
        return False
    return previous.get("hash") == compute_summary_hash(summary)


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


def _panel(panel_type: str, *blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "panel", "attrs": {"panelType": panel_type}, "content": list(blocks)}


def build_ai_summary_comment_adf(
    summary: Mapping[str, Any],
    *,
    issue_key: str,
    task_summary: Optional[str] = None,
) -> dict[str, Any]:
    """Render a structured Jira ADF comment for an AI task summary."""
    sp_dev = summary.get("sp_dev")
    sp_test = summary.get("sp_test")
    sp_final = summary.get("sp_final")
    scale_label = str(summary.get("scale_label") or "").strip()
    confidence = str(summary.get("confidence") or "").strip().lower()
    confidence_ru = _CONFIDENCE_RU.get(confidence, confidence or "—")

    content: list[dict[str, Any]] = [
        _heading("AI-оценка · Planning Poker"),
    ]

    if task_summary:
        content.append(_paragraph(_text(task_summary, strong=True), _text(f" · {issue_key}")))
    else:
        content.append(_paragraph(_text(issue_key, strong=True)))

    description = str(summary.get("description") or "").strip()
    if description:
        content.append(_heading("Описание", 4))
        content.append(_paragraph(_text(description)))

    panel_blocks: list[dict[str, Any]] = []
    if all(isinstance(value, (int, float)) for value in (sp_dev, sp_test, sp_final)):
        panel_blocks.append(
            _paragraph(
                _text("SP final: ", strong=True),
                _text(str(int(sp_final)), strong=True),
                _text(f" · dev {int(sp_dev)} · test {int(sp_test)}"),
            )
        )
    if scale_label or confidence:
        parts: list[dict[str, Any]] = []
        if scale_label:
            parts.append(_text(scale_label))
        if confidence:
            if parts:
                parts.append(_text(" · "))
            parts.append(_text(f"Уверенность: {confidence_ru}"))
        panel_blocks.append(_paragraph(*parts))
    if panel_blocks:
        content.append(_panel("info", *panel_blocks))

    complexity = str(summary.get("complexity") or "").strip()
    if complexity:
        content.append(_heading("Сложность", 4))
        content.append(_paragraph(_text(complexity)))

    methods = [str(item).strip() for item in (summary.get("methods") or []) if str(item).strip()]
    if methods:
        content.append(_heading("Методы / зоны внимания", 4))
        content.append(_bullet_list(methods))

    assumptions = [str(item).strip() for item in (summary.get("assumptions") or []) if str(item).strip()]
    if assumptions:
        content.append(_heading("Допущения", 4))
        content.append(_bullet_list(assumptions))

    generated_at = str(summary.get("generated_at") or "").strip()
    footer_parts = [_text("Сгенерировано Planning Poker")]
    if generated_at:
        footer_parts.extend([_text(" · "), _text(generated_at)])
    content.append(_paragraph(*footer_parts))

    return {"type": "doc", "version": 1, "content": content}


async def export_adf_comment_to_jira(
    client: Any,
    *,
    issue_key: str,
    adf: Mapping[str, Any],
    content_hash: str,
    previous_export: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Post or update a Jira comment with ADF. Returns jira_export metadata."""
    previous = previous_export if isinstance(previous_export, dict) else None
    if previous and previous.get("status") == "ok" and previous.get("hash") == content_hash:
        return dict(previous)

    comment_id = str((previous or {}).get("comment_id") or "").strip() or None

    try:
        if comment_id:
            result = await client.update_issue_comment_adf(issue_key, comment_id, adf)
            cid = str(result.get("comment_id") or comment_id)
        else:
            result = await client.add_issue_comment_adf(issue_key, adf)
            cid = str(result.get("comment_id") or "")
        return {
            "status": "ok",
            "hash": content_hash,
            "comment_id": cid or None,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.warning("Jira ADF comment export failed key=%s err=%r", issue_key, exc)
        return {
            "status": "error",
            "hash": content_hash,
            "comment_id": comment_id,
            "error": str(exc)[:500],
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }


async def export_ai_summary_to_jira(
    client: Any,
    *,
    issue_key: str,
    summary: Mapping[str, Any],
    task_summary: Optional[str] = None,
) -> dict[str, Any]:
    """Post or update a Jira comment with AI summary ADF. Returns jira_export metadata."""
    summary_hash = compute_summary_hash(summary)
    previous = summary.get("jira_export") if isinstance(summary.get("jira_export"), dict) else None

    if should_skip_jira_export(summary, previous_export=previous):
        return dict(previous or {})

    adf = build_ai_summary_comment_adf(summary, issue_key=issue_key, task_summary=task_summary)
    return await export_adf_comment_to_jira(
        client,
        issue_key=issue_key,
        adf=adf,
        content_hash=summary_hash,
        previous_export=previous,
    )
