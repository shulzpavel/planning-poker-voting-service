"""Plain-text helpers for Jira issue fields (including ADF descriptions)."""

from __future__ import annotations

from typing import Any


def adf_to_plain_text(node: Any) -> str:
    """Convert Atlassian Document Format (or plain string) to readable text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        return "\n".join(part for part in (adf_to_plain_text(item) for item in node) if part).strip()
    if not isinstance(node, dict):
        return str(node).strip()

    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "inlineCard":
        url = str((node.get("attrs") or {}).get("url") or "").strip()
        if url:
            return url.rstrip("/").split("/")[-1]
        return ""
    if node_type == "mention":
        return str((node.get("attrs") or {}).get("text") or "")

    children = node.get("content") or []
    inner = "".join(adf_to_plain_text(child) for child in children)
    if node_type in {"paragraph", "heading", "listItem", "tableRow", "tableCell"}:
        return f"{inner}\n" if inner else ""
    if node_type in {"bulletList", "orderedList", "blockquote", "codeBlock"}:
        return f"{inner}\n" if inner else ""
    if node_type == "hardBreak":
        return "\n"
    return inner


def truncate_text(text: str, max_chars: int) -> str:
    """Trim text for LLM/UI context without breaking mid-word when possible.

    Newlines are preserved on purpose: this same projection is what the
    voter UI shows as a plain-text fallback when the source field is not
    ADF (e.g. Jira Server returns descriptions as wiki/plain strings).
    Collapsing on every whitespace character — like an earlier version
    did with ``" ".join(text.split())`` — flattened paragraphs into a
    single run of text and made the description look like one big wall
    on the voter screen. We still normalise runs of spaces/tabs inside a
    line so wiki indentation doesn't blow up the budget.
    """
    if not text:
        return ""
    normalised_lines = ["\u00a0".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(normalised_lines).replace("\u00a0", " ").strip()
    # Collapse 3+ consecutive blank lines down to 2 — large gaps from
    # wiki markup are visual noise but a single blank line still
    # separates paragraphs.
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    # Try to clip on a whitespace boundary (newline or space) so we
    # don't cut a word in half. Prefer a newline boundary when it lands
    # at least 50% into the budget, otherwise fall back to the last
    # space — keeps the truncation visually clean for both rich-text
    # and one-paragraph descriptions.
    window = cleaned[: max_chars - 1]
    nl = window.rfind("\n")
    sp = window.rfind(" ")
    cut = nl if nl >= max_chars // 2 else max(sp, nl)
    if cut <= 0:
        cut = max_chars - 1
    return f"{window[:cut].rstrip()}…"
