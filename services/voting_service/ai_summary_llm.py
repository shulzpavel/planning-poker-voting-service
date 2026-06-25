"""Anthropic Claude integration for voting task AI summaries."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from app.domain.task import Task
from app.utils.jira_text import truncate_text

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_CONTEXT_CHARS = 16000
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
STORY_POINT_SCALE = (1, 2, 3, 5, 8, 13, 18)


class LlmSummaryError(Exception):
    """Raised when LLM summary generation fails (strict mode, no fallback)."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _anthropic_api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def _anthropic_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL).strip() or DEFAULT_ANTHROPIC_MODEL


def _anthropic_timeout() -> aiohttp.ClientTimeout:
    seconds = max(5, int(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "20")))
    return aiohttp.ClientTimeout(total=seconds)


def _max_context_chars() -> int:
    return max(500, int(os.getenv("ANTHROPIC_MAX_CONTEXT_CHARS", str(DEFAULT_MAX_CONTEXT_CHARS))))


def _max_output_tokens() -> int:
    return max(512, int(os.getenv("ANTHROPIC_MAX_OUTPUT_TOKENS", "1200")))


def _task_context_chars() -> int:
    return max(1200, int(os.getenv("AI_SUMMARY_MAX_CONTEXT_CHARS", str(min(_max_context_chars(), 3500)))))


def llm_configured() -> bool:
    return bool(_anthropic_api_key())


def _compact_long_text(text: str, limit: int) -> str:
    """Keep the beginning and end of long specs; Confluence often hides key
    acceptance criteria near the bottom.
    """
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    head = max(400, int(limit * 0.62))
    tail = max(300, limit - head - 32)
    return f"{cleaned[:head]}\n…\n{cleaned[-tail:]}"


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = JSON_FENCE_RE.sub("", stripped).strip()
    return stripped


def _extract_json_object(text: str) -> str:
    """Return the first complete JSON object from a model response."""
    start = text.find("{")
    if start < 0:
        return text

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text


def _parse_llm_json_payload(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_json_fences(raw_text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        extracted = _extract_json_object(cleaned)
        if extracted == cleaned:
            raise LlmSummaryError("LLM returned invalid JSON", status_code=502) from exc
        try:
            payload = json.loads(extracted)
        except json.JSONDecodeError as extracted_exc:
            raise LlmSummaryError("LLM returned invalid JSON", status_code=502) from extracted_exc
    if not isinstance(payload, dict):
        raise LlmSummaryError("LLM JSON must be an object", status_code=502)
    return payload


def parse_llm_json_object(raw_text: str) -> dict[str, Any]:
    """Parse a JSON object from model output; tolerates fences and a missing leading ``{``."""
    cleaned = _strip_json_fences(raw_text.strip())
    if not cleaned.startswith("{"):
        cleaned = f"{{{cleaned}"
    try:
        return _parse_llm_json_payload(cleaned)
    except LlmSummaryError as first_error:
        extracted = _extract_json_object(cleaned)
        if extracted != cleaned:
            try:
                return _parse_llm_json_payload(extracted)
            except LlmSummaryError:
                pass
        logger.warning("LLM JSON parse failed: %s preview=%s", first_error.message, cleaned[:240])
        raise


def anthropic_model_not_found_message(body_text: str) -> Optional[str]:
    try:
        error_payload = json.loads(body_text)
    except json.JSONDecodeError:
        return None
    error_message = str((error_payload.get("error") or {}).get("message") or "").strip()
    if error_message.startswith("model:"):
        return f"Anthropic model not found: {error_message.removeprefix('model:').strip()}"
    return None


def read_anthropic_response_text(
    data: dict[str, Any],
    *,
    truncated_message: str = "LLM response was truncated",
) -> str:
    stop_reason = str(data.get("stop_reason") or "")
    if stop_reason == "max_tokens":
        raise LlmSummaryError(truncated_message, status_code=502)

    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise LlmSummaryError("LLM response has no content", status_code=502)

    text_parts = [str(block.get("text", "")) for block in blocks if block.get("type") == "text"]
    combined = "\n".join(part for part in text_parts if part).strip()
    if not combined:
        raise LlmSummaryError("LLM response was empty", status_code=502)
    return combined


def _validate_summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    description = str(payload.get("description") or "").strip()
    if not description:
        raise LlmSummaryError("LLM summary is missing description", status_code=502)

    methods_raw = payload.get("methods")
    if not isinstance(methods_raw, list):
        raise LlmSummaryError("LLM summary is missing methods", status_code=502)
    methods = [str(item).strip() for item in methods_raw if str(item).strip()]
    if not methods:
        raise LlmSummaryError("LLM summary methods must not be empty", status_code=502)
    methods = methods[:4]

    complexity = str(payload.get("complexity") or "").strip()
    if not complexity:
        raise LlmSummaryError("LLM summary is missing complexity", status_code=502)

    sp_dev_raw = payload.get("sp_dev")
    sp_test_raw = payload.get("sp_test")
    if not isinstance(sp_dev_raw, int) or sp_dev_raw not in STORY_POINT_SCALE:
        raise LlmSummaryError("LLM summary has invalid sp_dev", status_code=502)
    if not isinstance(sp_test_raw, int) or sp_test_raw not in STORY_POINT_SCALE:
        raise LlmSummaryError("LLM summary has invalid sp_test", status_code=502)

    derived_final = max(sp_dev_raw, sp_test_raw)
    sp_final_raw = payload.get("sp_final")
    if isinstance(sp_final_raw, int) and sp_final_raw in STORY_POINT_SCALE:
        sp_final = max(sp_final_raw, derived_final)
    else:
        sp_final = derived_final

    scale_label = str(payload.get("scale_label") or "").strip()
    if not scale_label:
        scale_label = f"{sp_final} SP"

    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"

    assumptions_raw = payload.get("assumptions")
    if isinstance(assumptions_raw, list):
        assumptions = [str(item).strip()[:180] for item in assumptions_raw if str(item).strip()][:3]
    else:
        assumptions = []

    return {
        "description": description[:900],
        "methods": methods,
        "complexity": complexity[:320],
        "sp_dev": sp_dev_raw,
        "sp_test": sp_test_raw,
        "sp_final": sp_final,
        "scale_label": scale_label[:120],
        "confidence": confidence,
        "assumptions": assumptions,
        "estimation_model": "max(sp_dev, sp_test)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "anthropic",
    }


def _build_task_context(task: Task, jira_context: Optional[dict[str, Any]]) -> str:
    """Pack the task context Anthropic sees.

    The prompt uses both the task summary (title) and — when available —
    the Jira description body. Order of preference for the description:

    1. ``jira_context["description"]`` — freshest, comes from a live
       jira-service call (handles edits made after import).
    2. ``task.description`` — captured at Jira import time. Pre-fetched
       and stored on the Task so subsequent AI generations don't need to
       hit jira-service, and so the voter UI can show the same body.

    Manual tasks have neither and the prompt happily works off the
    summary alone.
    """
    lines = [
        f"summary: {task.summary.strip()}",
        f"source: {task.source}",
    ]
    if task.jira_key:
        lines.append(f"jira_key: {task.jira_key}")
    if task.url:
        lines.append(f"url: {task.url}")
    if task.story_points is not None:
        lines.append(f"story_points_in_session: {task.story_points}")

    description_from_context = ""
    if jira_context:
        for key in ("key", "summary", "url", "issue_type", "story_points"):
            value = jira_context.get(key)
            if value not in (None, ""):
                lines.append(f"jira_{key}: {value}")
        labels = jira_context.get("labels") or []
        if labels:
            lines.append(f"jira_labels: {', '.join(str(label) for label in labels)}")
        components = jira_context.get("components") or []
        if components:
            lines.append(f"jira_components: {', '.join(str(item) for item in components)}")
        description_from_context = str(jira_context.get("description") or "").strip()
        if description_from_context:
            lines.append(f"jira_description:\n{_compact_long_text(description_from_context, 3000)}")

    # Fallback to the description we captured at import time when the
    # live jira-service call didn't return one (offline jira, demo mode,
    # cache miss, …). Avoids dropping the planning spec from the prompt.
    if not description_from_context and task.description:
        lines.append(f"jira_description:\n{_compact_long_text(task.description.strip(), 3000)}")

    return truncate_text("\n".join(lines), _task_context_chars())


def _system_prompt() -> str:
    """Build the system prompt for Anthropic.

    Encodes the team's Story Points rubric in a form the model can apply
    consistently across tasks:

    * the schema is fixed (``_validate_summary_payload`` enforces it on
      the way out, so any drift here will surface as 502 from the
      handler rather than silently corrupt the UI);
    * the only valid SP values are the Fibonacci-ish scale 1, 2, 3, 5,
      8, 13, 18 — anything else is rejected by the validator;
    * the formula ``SP = max(SP dev, SP test)`` is repeated in three
      different places (schema notes, rules, output reminder) because
      it is the single most important rule and the model otherwise
      defaults to averaging or to ``SP dev`` alone;
    * a small handful of calibration examples is inlined to anchor the
      model — full team handbook is too long to ship every request and
      would inflate token cost without measurable quality gain.

    Output language is Russian to match the rest of the product UI.
    """
    return (
        "Ты помогаешь оценивать задачи в Story Points (SP). Верни ОДИН компактный JSON без markdown, строго по схеме:\n"
        '{"description": string, "methods": string[], "complexity": string, '
        '"sp_dev": 1|2|3|5|8|13|18, "sp_test": 1|2|3|5|8|13|18, '
        '"sp_final": 1|2|3|5|8|13|18, "scale_label": string, '
        '"confidence": "low"|"medium"|"high", "assumptions": string[]}\n'
        "Лимиты: description 1-2 предложения; methods 2-4; complexity 1 предложение; assumptions 0-3, строки до 140 символов.\n"
        "Оценивай именно инженерную сложность реализации задачи, а не длину документации и не количество текста в Jira/Confluence. "
        "Документация — только источник сигналов: какие компоненты менять, какие интеграции, данные, состояния, права, миграции, обратная совместимость, тесты и edge cases затронуты. "
        "Story Points = относительная сложность/неопределённость/риск изменения, не время и не объём документа. Формула: sp_final = max(sp_dev, sp_test) / max(SP dev, SP test). "
        "Шкала Фибоначчи: 1 SP, 2 SP, 3 SP, 5 SP, 8 SP, 13 SP, 18 SP; других значений нельзя. "
        "18 SP = декомпозиция. Эпики не оценивай как одну задачу: укажи декомпозицию и ставь 13/18. "
        "Большое описание само по себе не повышает SP: повышай оценку только когда из контекста видны сложные изменения, неизвестные зависимости или риск регрессий. "
        "Если требования размыты, короткие или противоречивые: confidence=low/medium, assumptions с вопросами; SP повышай только за реальную неопределённость реализации. "
        "Если это spike/исследование без реализации: 1-2 SP. Не оценивай митинги, ретро, обучение, чаты и чистую документацию: 1 SP и пояснение. "
        "В поле complexity объясни, почему задача сложна или проста именно технически. Опирайся только на контекст, не выдумывай объём. Верни только валидный JSON."
    )


def _user_prompt(task_context: str) -> str:
    return f"Generate a planning-poker hint for this task:\n\n{task_context}"


def _repair_user_prompt(task_context: str, error_message: str) -> str:
    return (
        "Previous answer was rejected by the JSON validator: "
        f"{error_message}. Generate the planning-poker hint again.\n"
        "Return one compact valid JSON object only, with all required fields. "
        "Keep strings short enough to avoid truncation.\n\n"
        f"Task context:\n{task_context}"
    )


async def _call_anthropic(
    http_session: aiohttp.ClientSession,
    task_context: str,
    *,
    repair_error: Optional[str] = None,
) -> str:
    api_key = _anthropic_api_key()
    if not api_key:
        raise LlmSummaryError("LLM is not configured", status_code=503)

    user_content = _repair_user_prompt(task_context, repair_error) if repair_error else _user_prompt(task_context)
    payload = {
        "model": _anthropic_model(),
        "max_tokens": _max_output_tokens(),
        "temperature": 0.2,
        "system": _system_prompt(),
        "messages": [
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    try:
        async with http_session.post(
            ANTHROPIC_API_URL,
            json=payload,
            headers=headers,
            timeout=_anthropic_timeout(),
        ) as response:
            body_text = await response.text()
            if response.status in {401, 403}:
                raise LlmSummaryError("LLM authentication failed", status_code=502)
            if response.status == 429:
                raise LlmSummaryError("LLM rate limit exceeded, try again shortly", status_code=503)
            if response.status >= 500:
                raise LlmSummaryError("LLM service is temporarily unavailable", status_code=503)
            if response.status != 200:
                logger.warning("Anthropic API error status=%s body=%s", response.status, body_text[:300])
                model_error = anthropic_model_not_found_message(body_text)
                if response.status == 404 and model_error:
                    raise LlmSummaryError(model_error, status_code=502)
                raise LlmSummaryError("LLM request failed", status_code=502)

            data = json.loads(body_text) if body_text else {}
    except aiohttp.ClientError as exc:
        raise LlmSummaryError("LLM service is unreachable", status_code=503) from exc
    except json.JSONDecodeError as exc:
        raise LlmSummaryError("LLM returned an unreadable response", status_code=502) from exc

    return read_anthropic_response_text(data, truncated_message="LLM response was truncated — retry shortly")


def _parse_and_validate_summary(raw_text: str) -> dict[str, Any]:
    payload = parse_llm_json_object(raw_text)
    return _validate_summary_payload(payload)


async def generate_ai_summary_llm(
    http_session: aiohttp.ClientSession,
    task: Task,
    jira_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Generate and validate AI summary via Anthropic (strict — no heuristic fallback).

    ``http_session`` is the long-lived ``aiohttp.ClientSession`` owned by the
    voting-service lifespan. Re-using it avoids tearing down the connection
    pool / TLS state between calls — the same fix that lets the jira-service
    cache do its job.
    """
    task_context = _build_task_context(task, jira_context)
    raw = await _call_anthropic(http_session, task_context)
    try:
        return _parse_and_validate_summary(raw)
    except LlmSummaryError as exc:
        logger.warning("LLM summary response failed validation; retrying once: %s", exc.message)
        retry_raw = await _call_anthropic(http_session, task_context, repair_error=exc.message)
        return _parse_and_validate_summary(retry_raw)


async def fetch_jira_issue_context(
    http_session: aiohttp.ClientSession,
    issue_key: str,
) -> Optional[dict[str, Any]]:
    """Fetch rich Jira issue context from jira-service for LLM prompts."""
    key = (issue_key or "").strip().upper()
    if not key:
        return None

    base_url = os.getenv("JIRA_SERVICE_URL", "http://jira-service:8001").rstrip("/")
    timeout = aiohttp.ClientTimeout(total=int(os.getenv("JIRA_SERVICE_TIMEOUT_SECONDS", "30")))
    url = f"{base_url}/api/v1/issue/{key}/context"

    try:
        async with http_session.get(url, timeout=timeout) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                body = (await response.text())[:300]
                logger.warning("Jira context fetch failed key=%s status=%s body=%s", key, response.status, body)
                raise LlmSummaryError("Failed to load Jira issue context", status_code=502)
            data = await response.json()
    except aiohttp.ClientError as exc:
        raise LlmSummaryError("Jira service is unreachable", status_code=503) from exc

    return data if isinstance(data, dict) else None
