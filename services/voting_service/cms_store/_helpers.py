"""Postgres read model for CMS/admin screens.

The app stores live session state as compact domain JSON. CMS screens need a
different shape: indexed, normalized, and pageable tables. This module keeps
that read model in sync without making normal voting depend on CMS writes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from services.voting_service.cms_store import PostgresCmsStore

import asyncpg

from app.domain.session import Session, SessionFactory
from app.domain.task import Task
from app.domain.scope_flow_pace import apply_flow_pace_chart_order, normalize_flow_pace_chart_order
from services.voting_service.cms_rbac import (
    ALL_PERMISSION_KEYS,
    CMS_PAGE_DEFINITIONS,
    CMS_PERMISSION_DEFINITIONS,
    DEPRECATED_CMS_PAGE_KEYS,
    OPERATIONAL_VIEW_PERMISSIONS,
    PERM_ACCESS_MANAGE,
    PERM_ACCESS_VIEW,
    PERM_APP_SESSIONS_MANAGE,
    PERM_PLANNER_VIEW,
    PERM_SESSIONS_VIEW,
    PERM_TASKS_MANAGE,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)


DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


_TEAM_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def normalize_team_slug(value: str) -> str:
    slug = _TEAM_SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("team slug cannot be empty")
    if not slug[0].isalpha():
        slug = f"team-{slug}"
    return slug[:64].rstrip("-")


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> dict[str, Any]:
    if not cursor:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_cursor_timestamp(value: Any) -> Optional[datetime]:
    """Cursors are serialised as JSON with ``default=str``, so timestamps
    arrive as ISO-8601 strings. asyncpg, however, binds ``timestamptz``
    parameters as ``datetime`` instances. Convert here so every paginated
    list endpoint can pass cursor TS straight through to the SQL query."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # ``datetime.fromisoformat`` handles the ``YYYY-MM-DDTHH:MM:SS[.ffffff][+HH:MM]``
            # shape produced by ``str(datetime)`` since Python 3.11.
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def session_key(chat_id: int, topic_id: Optional[int]) -> str:
    return f"{chat_id}:{'none' if topic_id is None else topic_id}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return token[:8]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return json.loads(json.dumps(dict(row), default=_json_default))


def _user_row_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Serialize a cms_users row; user_id as str for JS clients (int64-safe)."""
    data = _row_to_dict(row)
    data["user_id"] = str(data["user_id"])
    return data


def _sprint_plan_row(row: asyncpg.Record) -> dict[str, Any]:
    """Serialize a cms_sprint_plans row. Payload column is JSONB (asyncpg returns text)."""
    raw_payload = row["payload"]
    if isinstance(raw_payload, (bytes, bytearray)):
        payload = json.loads(raw_payload.decode("utf-8"))
    elif isinstance(raw_payload, str):
        payload = json.loads(raw_payload)
    else:
        payload = raw_payload or {}
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    data = {
        "id": int(row["id"]),
        "name": row["name"],
        "payload": payload,
        "created_by": int(row["created_by"]) if row["created_by"] is not None else None,
        "created_by_username": row["created_by_username"] if "created_by_username" in row.keys() else None,
        "created_by_display_name": row["created_by_display_name"] if "created_by_display_name" in row.keys() else None,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }
    return _attach_team_fields(data, row)


def _decode_jsonb(raw: Any) -> Any:
    """Decode an asyncpg JSONB column (text/bytes/native) into a Python object."""
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw.decode("utf-8"))
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _scope_board_row(row: asyncpg.Record) -> dict[str, Any]:
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    data = {
        "id": int(row["id"]),
        "name": row["name"],
        "month": row["month"],
        "capacity_sp": float(row["capacity_sp"]),
        "capacity_sp_dev": float(row["capacity_sp_dev"]) if "capacity_sp_dev" in row.keys() and row["capacity_sp_dev"] is not None else None,
        "capacity_sp_test": float(row["capacity_sp_test"]) if "capacity_sp_test" in row.keys() and row["capacity_sp_test"] is not None else None,
        "workload_mode": row["workload_mode"] if "workload_mode" in row.keys() else "sp",
        "plan_jql": row["plan_jql"],
        "unplan_jql": row["unplan_jql"],
        "todo_jql": row["todo_jql"],
        "test_jql": row["test_jql"],
        "report_type": row["report_type"] if "report_type" in row.keys() else "monthly",
        "previous_release_jql": row["previous_release_jql"] if "previous_release_jql" in row.keys() else "",
        "next_release_jql": row["next_release_jql"] if "next_release_jql" in row.keys() else "",
        "custom_release_name": row["custom_release_name"] if "custom_release_name" in row.keys() else "",
        "custom_release_jql": row["custom_release_jql"] if "custom_release_jql" in row.keys() else "",
        "release_queries": _decode_jsonb(row["release_queries"]) if "release_queries" in row.keys() and row["release_queries"] is not None else [],
        "release_comment": row["release_comment"] if "release_comment" in row.keys() else "",
        "previous_release_comment": row["previous_release_comment"] if "previous_release_comment" in row.keys() else "",
        "next_release_comment": row["next_release_comment"] if "next_release_comment" in row.keys() else "",
        "custom_release_comment": row["custom_release_comment"] if "custom_release_comment" in row.keys() else "",
        "plan_epic_key": str(row["plan_epic_key"] or "").strip() if "plan_epic_key" in row.keys() else "",
        "scope_sections": _decode_jsonb(row["scope_sections"]) if "scope_sections" in row.keys() and row["scope_sections"] is not None else None,
        "snapshot": _decode_jsonb(row["snapshot"]) if row["snapshot"] is not None else None,
        "ai_summary": _decode_jsonb(row["ai_summary"]) if "ai_summary" in row.keys() and row["ai_summary"] is not None else None,
        "ai_summary_history": _decode_jsonb(row["ai_summary_history"]) if "ai_summary_history" in row.keys() and row["ai_summary_history"] is not None else [],
        "layout_order": _decode_jsonb(row["layout_order"]) if "layout_order" in row.keys() and row["layout_order"] is not None else [],
        "flow_pace_chart_order": _decode_jsonb(row["flow_pace_chart_order"])
        if "flow_pace_chart_order" in row.keys() and row["flow_pace_chart_order"] is not None
        else [],
        "created_by": int(row["created_by"]) if row["created_by"] is not None else None,
        "created_by_username": row["created_by_username"] if "created_by_username" in row.keys() else None,
        "created_by_display_name": row["created_by_display_name"] if "created_by_display_name" in row.keys() else None,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }
    order = normalize_flow_pace_chart_order(data.get("flow_pace_chart_order"))
    data["flow_pace_chart_order"] = order
    snapshot = data.get("snapshot")
    if isinstance(snapshot, dict) and snapshot.get("flow_pace") is not None:
        data["snapshot"] = {
            **snapshot,
            "flow_pace": apply_flow_pace_chart_order(snapshot.get("flow_pace"), order),
        }
    return _attach_team_fields(data, row)


def _team_ref_from_row(row: asyncpg.Record) -> Optional[dict[str, Any]]:
    if "team_id" not in row.keys() or row["team_id"] is None:
        return None
    name = row["team_name"] if "team_name" in row.keys() else None
    slug = row["team_slug"] if "team_slug" in row.keys() else None
    if name is None and slug is None:
        return {"id": int(row["team_id"])}
    return {
        "id": int(row["team_id"]),
        "slug": slug,
        "name": name,
    }


def _attach_team_fields(data: dict[str, Any], row: asyncpg.Record) -> dict[str, Any]:
    team_id = row["team_id"] if "team_id" in row.keys() else None
    data["team_id"] = int(team_id) if team_id is not None else None
    data["team"] = _team_ref_from_row(row)
    return data


def _team_row(row: asyncpg.Record) -> dict[str, Any]:
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    return {
        "id": int(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"] or "",
        "is_active": bool(row["is_active"]),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


def _retro_row(row: asyncpg.Record) -> dict[str, Any]:
    """Serialize a cms_retros row. config/snapshot/ai_summary are JSONB."""
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    data = {
        "id": int(row["id"]),
        "title": row["title"],
        "status": row["status"],
        "config": _decode_jsonb(row["config"]) or {},
        "snapshot": _decode_jsonb(row["snapshot"]),
        "ai_summary": _decode_jsonb(row["ai_summary"]),
        "created_by": int(row["created_by"]) if row["created_by"] is not None else None,
        "created_by_username": row["created_by_username"] if "created_by_username" in row.keys() else None,
        "created_by_display_name": row["created_by_display_name"] if "created_by_display_name" in row.keys() else None,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }
    return _attach_team_fields(data, row)


def _standup_row(row: asyncpg.Record) -> dict[str, Any]:
    payload = _decode_jsonb(row["payload"]) if row["payload"] is not None else {}
    meeting_date = row["meeting_date"]
    published_at = row["published_at"]
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    data = {
        "id": int(row["id"]),
        "meeting_date": meeting_date.isoformat() if hasattr(meeting_date, "isoformat") else str(meeting_date),
        "status": row["status"],
        "payload": payload if isinstance(payload, dict) else {},
        "created_by": int(row["created_by"]) if row["created_by"] is not None else None,
        "created_by_username": row["created_by_username"] if "created_by_username" in row.keys() else None,
        "created_by_display_name": row["created_by_display_name"] if "created_by_display_name" in row.keys() else None,
        "published_by": int(row["published_by"]) if row["published_by"] is not None else None,
        "published_by_username": row["published_by_username"] if "published_by_username" in row.keys() else None,
        "published_by_display_name": row["published_by_display_name"] if "published_by_display_name" in row.keys() else None,
        "published_at": published_at.isoformat() if isinstance(published_at, datetime) else published_at,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }
    return _attach_team_fields(data, row)


def _standup_roster_row(row: asyncpg.Record) -> dict[str, Any]:
    members = _decode_jsonb(row["members"]) if row["members"] is not None else []
    updated_at = row["updated_at"]
    created_at = row["created_at"]
    return {
        "team_id": int(row["team_id"]),
        "members": members if isinstance(members, list) else [],
        "updated_by": int(row["updated_by"]) if row["updated_by"] is not None else None,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


def _serialize_session(session: Session) -> dict[str, Any]:
    return SessionFactory.to_dict(session)


def _deserialize_session(data: dict[str, Any], fallback_chat_id: int, fallback_topic_id: Optional[int]) -> Session:
    return SessionFactory.from_dict(data, fallback_chat_id, fallback_topic_id)


def _ids_from_session_key(key: str) -> tuple[int, Optional[int]]:
    parts = key.split(":")
    chat_id = int(parts[1])
    topic_raw = parts[2] if len(parts) > 2 else "none"
    topic_id = None if topic_raw == "none" else int(topic_raw)
    return chat_id, topic_id


async def backfill_cms_from_redis(redis_client, cms_store: "PostgresCmsStore") -> None:
    """Backfill current Redis live state into the CMS read model.

    Uses SCAN-style iteration so startup does not issue Redis KEYS against a
    large keyspace. The task is best-effort and voting continues if it fails.
    """
    try:
        session_count = 0
        async for key in redis_client.scan_iter(match="session:*", count=100):
            raw = await redis_client.get(key)
            if not raw:
                continue
            try:
                chat_id, topic_id = _ids_from_session_key(key)
                session = _deserialize_session(json.loads(raw), chat_id, topic_id)
                await cms_store.sync_session(session)
                session_count += 1
            except Exception as exc:
                logger.warning("CMS Redis session backfill skipped key=%s: %s", key, exc)

        token_count = 0
        async for key in redis_client.scan_iter(match="web:*", count=100):
            token = key.removeprefix("web:")
            raw = await redis_client.get(key)
            ttl = await redis_client.ttl(key)
            if not raw or ttl <= 0:
                continue
            try:
                info = json.loads(raw)
                await cms_store.record_web_token(token, int(info["chat_id"]), info.get("topic_id"), ttl)
                token_count += 1
            except Exception as exc:
                logger.warning("CMS Redis token backfill skipped key=%s: %s", key, exc)

        participant_count = 0
        async for key in redis_client.scan_iter(match="web_participant:*:*", count=100):
            raw = await redis_client.get(key)
            ttl = await redis_client.ttl(key)
            if not raw or ttl <= 0:
                continue
            try:
                _, token, participant_id = key.split(":", 2)
                token_raw = await redis_client.get(f"web:{token}")
                if not token_raw:
                    continue
                info = json.loads(token_raw)
                participant = json.loads(raw)
                await cms_store.record_web_participant(
                    token,
                    participant_id,
                    int(participant["user_id"]),
                    participant.get("name") or "Unknown",
                    participant.get("role") or "participant",
                    int(info["chat_id"]),
                    info.get("topic_id"),
                    ttl,
                )
                participant_count += 1
            except Exception as exc:
                logger.warning("CMS Redis participant backfill skipped key=%s: %s", key, exc)

        logger.info(
            "CMS Redis backfill completed: sessions=%s tokens=%s web_participants=%s",
            session_count,
            token_count,
            participant_count,
        )
    except Exception as exc:
        logger.warning("CMS Redis backfill failed: %s", exc)

