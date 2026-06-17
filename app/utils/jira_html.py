"""Sanitize Jira-rendered HTML for safe display in the voter UI."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

# Tags Jira's renderedFields.description typically emits. We drop
# everything else (including script/style/svg) so the voter UI can use
# innerHTML without pulling in bleach.
_ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "code",
        "pre",
        "blockquote",
        "a",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "colgroup",
        "col",
        "span",
        "div",
    }
)

# Strip obvious active content before parsing.
_STRIP_BLOCKS_RE = re.compile(
    r"<(script|style|iframe|object|embed|form)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_EVENT_ATTR_RE = re.compile(r"\s+on\w+\s*=\s*(['\"]).*?\1", re.IGNORECASE | re.DOTALL)
_JAVASCRIPT_HREF_RE = re.compile(r"\s+href\s*=\s*['\"]?\s*javascript:", re.IGNORECASE)


class _SanitizingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered not in _ALLOWED_TAGS:
            return
        safe_attrs: list[tuple[str, str]] = []
        for key, value in attrs:
            lk = key.lower()
            if lk.startswith("on") or lk in {"style", "src", "srcset", "formaction"}:
                continue
            if lk == "href" and value:
                href = value.strip()
                if href.lower().startswith(("javascript:", "data:")):
                    continue
                safe_attrs.append((lk, href))
            elif lk in {"colspan", "rowspan", "class", "id", "title", "target", "rel"}:
                if value is not None:
                    safe_attrs.append((lk, value))
        attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in safe_attrs)
        if lowered in {"br", "hr", "col"}:
            self._parts.append(f"<{lowered}{attr_str} />")
        else:
            self._parts.append(f"<{lowered}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _ALLOWED_TAGS and lowered not in {"br", "hr", "col"}:
            self._parts.append(f"</{lowered}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(_escape_text(data))

    def get_html(self) -> str:
        return "".join(self._parts)


class _PlainTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "blockquote",
        "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"br", "hr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self._parts.append(data)

    def get_text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _escape_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(text: str) -> str:
    return _escape_text(text).replace('"', "&quot;")


def sanitize_jira_html(html: str, max_chars: int = 80_000) -> str:
    """Return allowlisted HTML safe enough to store and render in the UI."""
    if not html or not str(html).strip():
        return ""
    cleaned = _STRIP_BLOCKS_RE.sub("", str(html))
    cleaned = _EVENT_ATTR_RE.sub("", cleaned)
    cleaned = _JAVASCRIPT_HREF_RE.sub("", cleaned)
    parser = _SanitizingParser()
    parser.feed(cleaned)
    parser.close()
    result = parser.get_html().strip()
    if max_chars > 0 and len(result) > max_chars:
        result = result[: max_chars - 1].rsplit("<", 1)[0] + "…"
    return result


def html_to_plain_text(html: str, max_chars: int = 20_000) -> str:
    """Project sanitized HTML into readable plain text for AI/fallbacks."""
    if not html or not str(html).strip():
        return ""
    parser = _PlainTextParser()
    parser.feed(str(html))
    parser.close()
    result = parser.get_text()
    if max_chars > 0 and len(result) > max_chars:
        return result[: max_chars - 1].rstrip() + "…"
    return result
