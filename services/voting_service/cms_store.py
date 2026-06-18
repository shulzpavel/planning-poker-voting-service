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
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.domain.session import Session, SessionFactory
from app.domain.task import Task
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
        "created_by": int(row["created_by"]) if row["created_by"] is not None else None,
        "created_by_username": row["created_by_username"] if "created_by_username" in row.keys() else None,
        "created_by_display_name": row["created_by_display_name"] if "created_by_display_name" in row.keys() else None,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
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


class PostgresCmsStore:
    """Normalized read model used by the CMS API."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def create(cls, dsn: str) -> "PostgresCmsStore":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        store = cls(pool)
        await store.ensure_schema()
        return store

    async def ensure_schema(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE EXTENSION IF NOT EXISTS pg_trgm;

                CREATE TABLE IF NOT EXISTS cms_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    session_key TEXT NOT NULL UNIQUE,
                    chat_id BIGINT NOT NULL,
                    topic_id BIGINT,
                    current_task_index INTEGER NOT NULL DEFAULT 0,
                    participants_count INTEGER NOT NULL DEFAULT 0,
                    tasks_queue_count INTEGER NOT NULL DEFAULT 0,
                    history_count INTEGER NOT NULL DEFAULT 0,
                    last_batch_count INTEGER NOT NULL DEFAULT 0,
                    total_tasks INTEGER NOT NULL DEFAULT 0,
                    total_votes INTEGER NOT NULL DEFAULT 0,
                    batch_completed BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active BOOLEAN NOT NULL DEFAULT FALSE,
                    current_batch_id TEXT,
                    current_batch_started_at TEXT,
                    current_task_id TEXT,
                    tasks_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    raw JSONB NOT NULL
                );
                ALTER TABLE cms_sessions
                    ADD COLUMN IF NOT EXISTS current_task_id TEXT;
                ALTER TABLE cms_sessions
                    ADD COLUMN IF NOT EXISTS tasks_version INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE cms_sessions
                    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
                ALTER TABLE cms_sessions
                    ADD COLUMN IF NOT EXISTS title TEXT;
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_title_trgm
                    ON cms_sessions USING GIN ((lower(title)) gin_trgm_ops)
                    WHERE title IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_updated
                    ON cms_sessions(updated_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_active_updated
                    ON cms_sessions(is_active, updated_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_chat
                    ON cms_sessions(chat_id, topic_id);
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_session_key_trgm
                    ON cms_sessions USING GIN (session_key gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_batch_id_trgm
                    ON cms_sessions USING GIN (current_batch_id gin_trgm_ops)
                    WHERE current_batch_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_cms_sessions_alive_updated
                    ON cms_sessions(updated_at DESC, id DESC)
                    WHERE deleted_at IS NULL;

                CREATE TABLE IF NOT EXISTS cms_users (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    is_web BOOLEAN NOT NULL DEFAULT FALSE,
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cms_users_last_seen
                    ON cms_users(last_seen_at DESC, user_id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_users_role
                    ON cms_users(role);
                CREATE INDEX IF NOT EXISTS idx_cms_users_name_trgm
                    ON cms_users USING GIN (name gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS idx_cms_users_id_text_trgm
                    ON cms_users USING GIN ((user_id::text) gin_trgm_ops);

                CREATE TABLE IF NOT EXISTS cms_session_participants (
                    session_id BIGINT NOT NULL REFERENCES cms_sessions(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'session',
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (session_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cms_session_participants_user
                    ON cms_session_participants(user_id);

                CREATE TABLE IF NOT EXISTS cms_tasks (
                    id BIGSERIAL PRIMARY KEY,
                    session_id BIGINT NOT NULL REFERENCES cms_sessions(id) ON DELETE CASCADE,
                    task_uid TEXT NOT NULL DEFAULT '',
                    bucket TEXT NOT NULL,
                    bucket_index INTEGER NOT NULL,
                    jira_key TEXT,
                    summary TEXT NOT NULL DEFAULT '',
                    url TEXT,
                    story_points INTEGER,
                    source TEXT NOT NULL DEFAULT 'jira',
                    votes_count INTEGER NOT NULL DEFAULT 0,
                    numeric_avg NUMERIC,
                    numeric_max INTEGER,
                    completed_at TEXT,
                    jql TEXT,
                    created_at_text TEXT,
                    domain_updated_at TEXT,
                    raw JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(session_id, bucket, bucket_index)
                );
                ALTER TABLE cms_tasks
                    ADD COLUMN IF NOT EXISTS task_uid TEXT NOT NULL DEFAULT '';
                ALTER TABLE cms_tasks
                    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'jira';
                ALTER TABLE cms_tasks
                    ADD COLUMN IF NOT EXISTS created_at_text TEXT;
                ALTER TABLE cms_tasks
                    ADD COLUMN IF NOT EXISTS domain_updated_at TEXT;
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_session_bucket
                    ON cms_tasks(session_id, bucket, bucket_index, id);
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_session_uid
                    ON cms_tasks(session_id, task_uid);
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_jira_key
                    ON cms_tasks(jira_key);
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_updated
                    ON cms_tasks(updated_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_summary_trgm
                    ON cms_tasks USING GIN (summary gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_jira_key_trgm
                    ON cms_tasks USING GIN (jira_key gin_trgm_ops)
                    WHERE jira_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_cms_tasks_uid_trgm
                    ON cms_tasks USING GIN (task_uid gin_trgm_ops)
                    WHERE task_uid <> '';

                CREATE TABLE IF NOT EXISTS cms_votes (
                    id BIGSERIAL PRIMARY KEY,
                    task_id BIGINT NOT NULL REFERENCES cms_tasks(id) ON DELETE CASCADE,
                    session_id BIGINT NOT NULL REFERENCES cms_sessions(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    value TEXT NOT NULL,
                    is_numeric BOOLEAN NOT NULL DEFAULT FALSE,
                    numeric_value INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cms_votes_task_id
                    ON cms_votes(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_cms_votes_session_id
                    ON cms_votes(session_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_votes_user_id
                    ON cms_votes(user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_votes_id_desc
                    ON cms_votes(id DESC);

                CREATE TABLE IF NOT EXISTS cms_web_tokens (
                    id BIGSERIAL PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    topic_id BIGINT,
                    session_key TEXT NOT NULL,
                    participants_joined INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cms_web_tokens_expires
                    ON cms_web_tokens(expires_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_web_tokens_session
                    ON cms_web_tokens(session_key, id DESC);

                CREATE TABLE IF NOT EXISTS cms_web_participants (
                    id BIGSERIAL PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    participant_id TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    topic_id BIGINT,
                    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(token_hash, participant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cms_web_participants_user
                    ON cms_web_participants(user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_web_participants_token
                    ON cms_web_participants(token_hash, id DESC);

                CREATE TABLE IF NOT EXISTS cms_audit_events (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    action TEXT NOT NULL,
                    actor TEXT,
                    status TEXT NOT NULL DEFAULT 'ok',
                    ip TEXT,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb
                );
                CREATE INDEX IF NOT EXISTS idx_cms_audit_events_ts
                    ON cms_audit_events(ts DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_cms_audit_events_action
                    ON cms_audit_events(action, ts DESC);

                CREATE TABLE IF NOT EXISTS cms_permissions (
                    key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS cms_pages (
                    key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    permission_key TEXT NOT NULL REFERENCES cms_permissions(key),
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cms_pages_sort
                    ON cms_pages(sort_order, key);

                CREATE TABLE IF NOT EXISTS cms_roles (
                    id BIGSERIAL PRIMARY KEY,
                    key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    is_system BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS cms_role_permissions (
                    role_id BIGINT NOT NULL REFERENCES cms_roles(id) ON DELETE CASCADE,
                    permission_key TEXT NOT NULL REFERENCES cms_permissions(key) ON DELETE CASCADE,
                    PRIMARY KEY (role_id, permission_key)
                );

                CREATE TABLE IF NOT EXISTS cms_admin_accounts (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_superuser BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_login_at TIMESTAMPTZ
                );
                ALTER TABLE cms_admin_accounts
                    ADD COLUMN IF NOT EXISTS theme_preference TEXT NOT NULL DEFAULT 'system';
                ALTER TABLE cms_admin_accounts
                    DROP CONSTRAINT IF EXISTS cms_admin_accounts_theme_preference_check;
                ALTER TABLE cms_admin_accounts
                    ADD CONSTRAINT cms_admin_accounts_theme_preference_check
                    CHECK (theme_preference IN ('dark', 'light', 'system'));
                CREATE INDEX IF NOT EXISTS idx_cms_admin_accounts_active
                    ON cms_admin_accounts(is_active, username);
                CREATE INDEX IF NOT EXISTS idx_cms_admin_accounts_username_lower
                    ON cms_admin_accounts((lower(username)), id);
                CREATE INDEX IF NOT EXISTS idx_cms_admin_accounts_display_name_lower
                    ON cms_admin_accounts((lower(display_name)), id)
                    WHERE display_name IS NOT NULL;

                CREATE TABLE IF NOT EXISTS cms_admin_roles (
                    admin_id BIGINT NOT NULL REFERENCES cms_admin_accounts(id) ON DELETE CASCADE,
                    role_id BIGINT NOT NULL REFERENCES cms_roles(id) ON DELETE CASCADE,
                    PRIMARY KEY (admin_id, role_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cms_admin_roles_role_admin
                    ON cms_admin_roles(role_id, admin_id);

                CREATE TABLE IF NOT EXISTS cms_sprint_plans (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_by BIGINT REFERENCES cms_admin_accounts(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cms_sprint_plans_updated
                    ON cms_sprint_plans(updated_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS cms_retros (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'live', 'done')),
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    snapshot JSONB,
                    ai_summary JSONB,
                    created_by BIGINT REFERENCES cms_admin_accounts(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cms_retros_updated
                    ON cms_retros(updated_at DESC, id DESC);
                """
            )
            await self._ensure_team_schema(conn)

    async def _ensure_team_schema(self, conn: asyncpg.Connection) -> None:
        """Team tables/columns are applied in a separate execute so upgrades from
        older deployments always run even if the main schema block was cached."""
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cms_teams (
                id BIGSERIAL PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cms_teams_active_name
                ON cms_teams(is_active, lower(name), id);

            CREATE TABLE IF NOT EXISTS cms_admin_teams (
                admin_id BIGINT NOT NULL REFERENCES cms_admin_accounts(id) ON DELETE CASCADE,
                team_id BIGINT NOT NULL REFERENCES cms_teams(id) ON DELETE CASCADE,
                PRIMARY KEY (admin_id, team_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cms_admin_teams_team_admin
                ON cms_admin_teams(team_id, admin_id);
            CREATE INDEX IF NOT EXISTS idx_cms_admin_teams_admin_team
                ON cms_admin_teams(admin_id, team_id);

            ALTER TABLE cms_sessions
                ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;
            ALTER TABLE cms_sprint_plans
                ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;
            ALTER TABLE cms_retros
                ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;

            CREATE TABLE IF NOT EXISTS cms_scope_boards (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                month TEXT NOT NULL,
                capacity_sp NUMERIC(10, 2) NOT NULL DEFAULT 0,
                plan_jql TEXT NOT NULL DEFAULT '',
                unplan_jql TEXT NOT NULL DEFAULT '',
                todo_jql TEXT NOT NULL DEFAULT '',
                test_jql TEXT NOT NULL DEFAULT '',
                scope_sections JSONB,
                snapshot JSONB,
                team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL,
                created_by BIGINT REFERENCES cms_admin_accounts(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS team_id BIGINT REFERENCES cms_teams(id) ON DELETE SET NULL;
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS todo_jql TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS test_jql TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS scope_sections JSONB;
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS ai_summary JSONB;
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS ai_summary_history JSONB NOT NULL DEFAULT '[]'::jsonb;
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS layout_order JSONB NOT NULL DEFAULT '[]'::jsonb;
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS report_type TEXT NOT NULL DEFAULT 'monthly';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS previous_release_jql TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS next_release_jql TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS custom_release_name TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS custom_release_jql TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS release_queries JSONB NOT NULL DEFAULT '[]'::jsonb;
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS release_comment TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS previous_release_comment TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS next_release_comment TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS custom_release_comment TEXT NOT NULL DEFAULT '';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS workload_mode TEXT NOT NULL DEFAULT 'sp';
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS capacity_sp_dev NUMERIC(10, 2);
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS capacity_sp_test NUMERIC(10, 2);
            ALTER TABLE cms_scope_boards
                ADD COLUMN IF NOT EXISTS plan_epic_key TEXT NOT NULL DEFAULT '';
            CREATE INDEX IF NOT EXISTS idx_cms_scope_boards_updated
                ON cms_scope_boards(updated_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_cms_sessions_team_updated
                ON cms_sessions(team_id, updated_at DESC, id DESC)
                WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_cms_sprint_plans_team_updated
                ON cms_sprint_plans(team_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_retros_team_updated
                ON cms_retros(team_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_cms_scope_boards_team_updated
                ON cms_scope_boards(team_id, updated_at DESC, id DESC);
            """
        )

    async def ensure_access_defaults(self, bootstrap_username: str, bootstrap_password: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for permission in CMS_PERMISSION_DEFINITIONS:
                    await conn.execute(
                        """
                        INSERT INTO cms_permissions (key, label, description)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (key) DO UPDATE SET
                            label = EXCLUDED.label,
                            description = EXCLUDED.description
                        """,
                        permission["key"],
                        permission["label"],
                        permission["description"],
                    )

                for page in CMS_PAGE_DEFINITIONS:
                    await conn.execute(
                        """
                        INSERT INTO cms_pages (key, label, path, permission_key, sort_order)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (key) DO UPDATE SET
                            label = EXCLUDED.label,
                            path = EXCLUDED.path,
                            permission_key = EXCLUDED.permission_key,
                            sort_order = EXCLUDED.sort_order,
                            updated_at = NOW()
                        """,
                        page["key"],
                        page["label"],
                        page["path"],
                        page["permission_key"],
                        page["sort_order"],
                    )

                if DEPRECATED_CMS_PAGE_KEYS:
                    await conn.execute(
                        """
                        UPDATE cms_pages
                        SET is_enabled = FALSE, updated_at = NOW()
                        WHERE key = ANY($1::text[]) AND is_enabled = TRUE
                        """,
                        list(DEPRECATED_CMS_PAGE_KEYS),
                    )

                superadmin_role_id = await self._upsert_system_role(
                    conn,
                    "superadmin",
                    "Superadmin",
                    "Full CMS access, including access management.",
                    ALL_PERMISSION_KEYS,
                )
                await self._upsert_system_role(
                    conn,
                    "viewer",
                    "Viewer",
                    "Read-only access to operational CMS pages.",
                    OPERATIONAL_VIEW_PERMISSIONS,
                )
                await self._upsert_system_role(
                    conn,
                    "access_manager",
                    "Access manager",
                    "Can view and manage CMS admins and roles.",
                    [PERM_ACCESS_VIEW, PERM_ACCESS_MANAGE],
                )
                await self._upsert_system_role(
                    conn,
                    "session_manager",
                    "Session manager",
                    "Can facilitate planning sessions and manage active task queues.",
                    [
                        PERM_SESSIONS_VIEW,
                        PERM_TASKS_MANAGE,
                        PERM_APP_SESSIONS_MANAGE,
                        PERM_PLANNER_VIEW,
                    ],
                )

                if bootstrap_username and bootstrap_password:
                    admin_id = await conn.fetchval(
                        """
                        INSERT INTO cms_admin_accounts (
                            username, password_hash, display_name,
                            is_active, is_superuser, updated_at
                        )
                        VALUES ($1, $2, $3, TRUE, TRUE, NOW())
                        ON CONFLICT (username) DO UPDATE SET
                            password_hash = EXCLUDED.password_hash,
                            display_name = COALESCE(cms_admin_accounts.display_name, EXCLUDED.display_name),
                            is_active = TRUE,
                            is_superuser = TRUE,
                            updated_at = NOW()
                        RETURNING id
                        """,
                        bootstrap_username,
                        hash_password(bootstrap_password),
                        bootstrap_username,
                    )
                    await conn.execute(
                        """
                        INSERT INTO cms_admin_roles (admin_id, role_id)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                        """,
                        admin_id,
                        superadmin_role_id,
                    )

    async def _upsert_system_role(
        self,
        conn: asyncpg.Connection,
        key: str,
        name: str,
        description: str,
        permission_keys: list[str],
    ) -> int:
        role_id = await conn.fetchval(
            """
            INSERT INTO cms_roles (key, name, description, is_system, updated_at)
            VALUES ($1, $2, $3, TRUE, NOW())
            ON CONFLICT (key) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                is_system = TRUE,
                updated_at = NOW()
            RETURNING id
            """,
            key,
            name,
            description,
        )
        await conn.execute("DELETE FROM cms_role_permissions WHERE role_id = $1", role_id)
        if permission_keys:
            await conn.executemany(
                """
                INSERT INTO cms_role_permissions (role_id, permission_key)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                [(role_id, permission_key) for permission_key in permission_keys],
            )
        return int(role_id)

    async def sync_session(self, session: Session) -> None:
        """Upsert a session and its normalized CMS children."""
        try:
            data = _serialize_session(session)
            queue = data["tasks_queue"]
            history = data["history"]
            last_batch = data["last_batch"]
            all_tasks = queue + history + last_batch
            votes_total = sum(len(task.get("votes") or {}) for task in all_tasks)
            participants = data["participants"]
            key = session_key(session.chat_id, session.topic_id)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    session_id = await conn.fetchval(
                        """
                        INSERT INTO cms_sessions (
                            session_key, chat_id, topic_id, current_task_index,
                            participants_count, tasks_queue_count, history_count,
                            last_batch_count, total_tasks, total_votes,
                            batch_completed, is_active, current_batch_id,
                            current_batch_started_at, current_task_id,
                            tasks_version, updated_at, raw
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8,
                            $9, $10, $11, $12, $13, $14, $15,
                            $16, NOW(), $17::jsonb
                        )
                        ON CONFLICT (session_key) DO UPDATE SET
                            chat_id = EXCLUDED.chat_id,
                            topic_id = EXCLUDED.topic_id,
                            current_task_index = EXCLUDED.current_task_index,
                            participants_count = EXCLUDED.participants_count,
                            tasks_queue_count = EXCLUDED.tasks_queue_count,
                            history_count = EXCLUDED.history_count,
                            last_batch_count = EXCLUDED.last_batch_count,
                            total_tasks = EXCLUDED.total_tasks,
                            total_votes = EXCLUDED.total_votes,
                            batch_completed = EXCLUDED.batch_completed,
                            is_active = EXCLUDED.is_active,
                            current_batch_id = EXCLUDED.current_batch_id,
                            current_batch_started_at = EXCLUDED.current_batch_started_at,
                            current_task_id = EXCLUDED.current_task_id,
                            tasks_version = EXCLUDED.tasks_version,
                            updated_at = NOW(),
                            raw = EXCLUDED.raw
                        WHERE cms_sessions.deleted_at IS NULL
                        RETURNING id
                        """,
                        key,
                        session.chat_id,
                        session.topic_id,
                        session.current_task_index,
                        len(participants),
                        len(queue),
                        len(history),
                        len(last_batch),
                        len(all_tasks),
                        votes_total,
                        session.batch_completed,
                        bool(session.current_batch_started_at and not session.batch_completed),
                        session.current_batch_id,
                        session.current_batch_started_at,
                        session.current_task_id,
                        session.tasks_version,
                        json.dumps(data),
                    )

                    if session_id is None:
                        # Session is soft-deleted in the CMS read model;
                        # skip downstream writes so deleted state is preserved.
                        return

                    user_ids: list[int] = []
                    for raw_uid, participant in participants.items():
                        user_id = int(raw_uid)
                        user_ids.append(user_id)
                        role = participant.get("role", "participant")
                        name = participant.get("name") or "Unknown"
                        is_web = True
                        await conn.execute(
                            """
                            INSERT INTO cms_users (user_id, name, role, is_web)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (user_id) DO UPDATE SET
                                name = EXCLUDED.name,
                                role = EXCLUDED.role,
                                is_web = cms_users.is_web OR EXCLUDED.is_web,
                                last_seen_at = NOW()
                            """,
                            user_id,
                            name,
                            role,
                            is_web,
                        )
                        await conn.execute(
                            """
                            INSERT INTO cms_session_participants (
                                session_id, user_id, name, role, source, last_seen_at
                            )
                            VALUES ($1, $2, $3, $4, $5, NOW())
                            ON CONFLICT (session_id, user_id) DO UPDATE SET
                                name = EXCLUDED.name,
                                role = EXCLUDED.role,
                                source = EXCLUDED.source,
                                last_seen_at = NOW()
                            """,
                            session_id,
                            user_id,
                            name,
                            role,
                            "web",
                        )

                    await conn.execute(
                        """
                        DELETE FROM cms_session_participants
                        WHERE session_id = $1 AND NOT (user_id = ANY($2::bigint[]))
                        """,
                        session_id,
                        user_ids,
                    )

                    await conn.execute("DELETE FROM cms_tasks WHERE session_id = $1", session_id)

                    for bucket, tasks in (
                        ("tasks_queue", queue),
                        ("history", history),
                        ("last_batch", last_batch),
                    ):
                        for idx, task in enumerate(tasks):
                            votes = task.get("votes") or {}
                            numeric_votes = [
                                int(value)
                                for value in votes.values()
                                if str(value).lstrip("-").isdigit()
                            ]
                            numeric_avg = (
                                Decimal(sum(numeric_votes)) / Decimal(len(numeric_votes))
                                if numeric_votes
                                else None
                            )
                            numeric_max = max(numeric_votes) if numeric_votes else None
                            task_id = await conn.fetchval(
                                """
                                INSERT INTO cms_tasks (
                                    session_id, task_uid, bucket, bucket_index, jira_key,
                                    summary, url, story_points, source, votes_count,
                                    numeric_avg, numeric_max, completed_at, jql,
                                    created_at_text, domain_updated_at, raw, updated_at
                                )
                                VALUES (
                                    $1, $2, $3, $4, $5, $6, $7, $8,
                                    $9, $10, $11, $12, $13, $14, $15,
                                    $16, $17::jsonb, NOW()
                                )
                                RETURNING id
                                """,
                                session_id,
                                task.get("task_id") or "",
                                bucket,
                                idx,
                                task.get("jira_key"),
                                task.get("summary") or "",
                                task.get("url"),
                                task.get("story_points"),
                                task.get("source") or ("jira" if task.get("jira_key") else "manual"),
                                len(votes),
                                numeric_avg,
                                numeric_max,
                                task.get("completed_at"),
                                task.get("jql"),
                                task.get("created_at"),
                                task.get("updated_at"),
                                json.dumps(task),
                            )
                            for raw_uid, value in votes.items():
                                value_text = str(value)
                                is_numeric = value_text.lstrip("-").isdigit()
                                await conn.execute(
                                    """
                                    INSERT INTO cms_votes (
                                        task_id, session_id, user_id, value,
                                        is_numeric, numeric_value
                                    )
                                    VALUES ($1, $2, $3, $4, $5, $6)
                                    """,
                                    task_id,
                                    session_id,
                                    int(raw_uid),
                                    value_text,
                                    is_numeric,
                                    int(value_text) if is_numeric else None,
                                )
        except Exception as exc:
            logger.warning("CMS read-model sync failed: %s", exc)

    async def record_web_token(self, token: str, chat_id: int, topic_id: Optional[int], ttl_seconds: int) -> None:
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            hashed = token_hash(token)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO cms_web_tokens (
                        token_hash, token_prefix, chat_id, topic_id, session_key,
                        expires_at, last_seen_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (token_hash) DO UPDATE SET
                        chat_id = EXCLUDED.chat_id,
                        topic_id = EXCLUDED.topic_id,
                        session_key = EXCLUDED.session_key,
                        expires_at = EXCLUDED.expires_at,
                        last_seen_at = NOW()
                    """,
                    hashed,
                    token_prefix(token),
                    chat_id,
                    topic_id,
                    session_key(chat_id, topic_id),
                    expires_at,
                )
        except Exception as exc:
            logger.warning("CMS web token record failed: %s", exc)

    async def record_web_participant(
        self,
        token: str,
        participant_id: str,
        user_id: int,
        name: str,
        role: str,
        chat_id: int,
        topic_id: Optional[int],
        ttl_seconds: int,
    ) -> None:
        try:
            hashed = token_hash(token)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO cms_web_participants (
                            token_hash, participant_id, user_id, name, role,
                            chat_id, topic_id, expires_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (token_hash, participant_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            role = EXCLUDED.role,
                            expires_at = EXCLUDED.expires_at
                        """,
                        hashed,
                        participant_id,
                        user_id,
                        name,
                        role,
                        chat_id,
                        topic_id,
                        expires_at,
                    )
                    await conn.execute(
                        """
                        INSERT INTO cms_users (user_id, name, role, is_web)
                        VALUES ($1, $2, $3, TRUE)
                        ON CONFLICT (user_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            role = EXCLUDED.role,
                            is_web = TRUE,
                            last_seen_at = NOW()
                        """,
                        user_id,
                        name,
                        role,
                    )
                    await conn.execute(
                        """
                        UPDATE cms_web_tokens
                        SET participants_joined = (
                                SELECT COUNT(*)
                                FROM cms_web_participants
                                WHERE token_hash = $1
                            ),
                            last_seen_at = NOW()
                        WHERE token_hash = $1
                        """,
                        hashed,
                    )
        except Exception as exc:
            logger.warning("CMS web participant record failed: %s", exc)

    async def record_audit_event(
        self,
        action: str,
        actor: Optional[str] = None,
        status: str = "ok",
        ip: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO cms_audit_events (action, actor, status, ip, payload)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    """,
                    action,
                    actor,
                    status,
                    ip,
                    json.dumps(payload or {}),
                )
        except Exception as exc:
            logger.warning("CMS audit record failed: %s", exc)

    async def verify_admin_login(self, username: str, password: str) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, password_hash, is_active
                FROM cms_admin_accounts
                WHERE username = $1
                """,
                username,
            )
            password_hash = row["password_hash"] if row else ""
            password_ok = verify_password(password, password_hash)
            if not row or not row["is_active"] or not password_ok:
                return None
            await conn.execute(
                "UPDATE cms_admin_accounts SET last_login_at = NOW(), updated_at = NOW() WHERE id = $1",
                row["id"],
            )
        return await self.get_admin_principal(admin_id=int(row["id"]))

    async def get_admin_principal(
        self,
        admin_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, display_name,
                       is_active, is_superuser, created_at, updated_at, last_login_at,
                       COALESCE(theme_preference, 'system') AS theme_preference
                FROM cms_admin_accounts
                WHERE ($1::bigint IS NOT NULL AND id = $1)
                   OR ($2::text IS NOT NULL AND username = $2)
                """,
                admin_id,
                username,
            )
            if not row or not row["is_active"]:
                return None

            role_rows = await conn.fetch(
                """
                SELECT r.id, r.key, r.name, r.description, r.is_system
                FROM cms_roles r
                JOIN cms_admin_roles ar ON ar.role_id = r.id
                WHERE ar.admin_id = $1
                ORDER BY r.name ASC, r.id ASC
                """,
                row["id"],
            )

            if row["is_superuser"]:
                permission_rows = await conn.fetch("SELECT key FROM cms_permissions ORDER BY key ASC")
            else:
                permission_rows = await conn.fetch(
                    """
                    SELECT DISTINCT p.key
                    FROM cms_permissions p
                    JOIN cms_role_permissions rp ON rp.permission_key = p.key
                    JOIN cms_admin_roles ar ON ar.role_id = rp.role_id
                    WHERE ar.admin_id = $1
                    ORDER BY p.key ASC
                    """,
                    row["id"],
                )

            permission_keys = [item["key"] for item in permission_rows]
            page_rows = await conn.fetch(
                """
                SELECT key, label, path, permission_key, sort_order
                FROM cms_pages
                WHERE is_enabled
                  AND ($1::boolean OR permission_key = ANY($2::text[]))
                ORDER BY sort_order ASC, key ASC
                """,
                row["is_superuser"],
                permission_keys,
            )

            team_rows = await conn.fetch(
                """
                SELECT t.id, t.slug, t.name, t.description, t.is_active,
                       t.created_at, t.updated_at
                FROM cms_teams t
                JOIN cms_admin_teams at ON at.team_id = t.id
                WHERE at.admin_id = $1 AND t.is_active
                ORDER BY lower(t.name) ASC, t.id ASC
                """,
                row["id"],
            )

        data = _row_to_dict(row)
        data["roles"] = [_row_to_dict(role) for role in role_rows]
        data["permissions"] = permission_keys
        data["pages"] = [_row_to_dict(page) for page in page_rows]
        data["teams"] = [_team_row(team) for team in team_rows]
        data["team_ids"] = [int(team["id"]) for team in team_rows]
        return data

    async def update_admin_theme_preference(self, admin_id: int, theme_preference: str) -> bool:
        """Persist the admin's theme choice. Returns True if the account exists and is active."""
        if theme_preference not in ("dark", "light", "system"):
            raise ValueError(f"invalid theme_preference: {theme_preference!r}")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE cms_admin_accounts
                SET theme_preference = $2,
                    updated_at = NOW()
                WHERE id = $1 AND is_active
                """,
                admin_id,
                theme_preference,
            )
        try:
            affected = int(result.split()[-1])
        except (ValueError, IndexError):
            affected = 0
        return affected > 0

    async def list_cms_permissions(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, label, description
                FROM cms_permissions
                ORDER BY key ASC
                """
            )
        return [_row_to_dict(row) for row in rows]

    async def list_cms_pages(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, label, path, permission_key, sort_order, is_enabled
                FROM cms_pages
                ORDER BY sort_order ASC, key ASC
                """
            )
        return [_row_to_dict(row) for row in rows]

    async def list_cms_roles(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, key, name, description, is_system, created_at, updated_at
                FROM cms_roles
                ORDER BY is_system DESC, name ASC, id ASC
                """
            )
            permissions = await conn.fetch(
                """
                SELECT role_id, permission_key
                FROM cms_role_permissions
                ORDER BY permission_key ASC
                """
            )
        permission_map: dict[int, list[str]] = {}
        for permission in permissions:
            permission_map.setdefault(int(permission["role_id"]), []).append(permission["permission_key"])
        roles = []
        for row in rows:
            item = _row_to_dict(row)
            item["permission_keys"] = permission_map.get(int(row["id"]), [])
            roles.append(item)
        return roles

    async def create_cms_role(
        self,
        key: str,
        name: str,
        description: str,
        permission_keys: list[str],
    ) -> dict[str, Any]:
        clean_permissions = await self._valid_permission_keys(permission_keys)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                role_id = await conn.fetchval(
                    """
                    INSERT INTO cms_roles (key, name, description, is_system, updated_at)
                    VALUES ($1, $2, $3, FALSE, NOW())
                    RETURNING id
                    """,
                    key,
                    name,
                    description,
                )
                await self._replace_role_permissions(conn, int(role_id), clean_permissions)
        return await self.get_cms_role(int(role_id))

    async def update_cms_role(
        self,
        role_id: int,
        name: str,
        description: str,
        permission_keys: list[str],
    ) -> Optional[dict[str, Any]]:
        clean_permissions = await self._valid_permission_keys(permission_keys)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchrow(
                    """
                    UPDATE cms_roles
                    SET name = $2, description = $3, updated_at = NOW()
                    WHERE id = $1 AND is_system = FALSE
                    RETURNING id
                    """,
                    role_id,
                    name,
                    description,
                )
                if not updated:
                    return None
                await self._replace_role_permissions(conn, role_id, clean_permissions)
        return await self.get_cms_role(role_id)

    async def get_cms_role(self, role_id: int) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, key, name, description, is_system, created_at, updated_at
                FROM cms_roles
                WHERE id = $1
                """,
                role_id,
            )
            permission_rows = await conn.fetch(
                """
                SELECT permission_key
                FROM cms_role_permissions
                WHERE role_id = $1
                ORDER BY permission_key ASC
                """,
                role_id,
            )
        item = _row_to_dict(row)
        item["permission_keys"] = [permission["permission_key"] for permission in permission_rows]
        return item

    async def list_teams(
        self,
        *,
        is_superuser: bool,
        actor_team_ids: Optional[list[int]] = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        actor_team_ids = actor_team_ids or []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, slug, name, description, is_active, created_at, updated_at
                FROM cms_teams
                WHERE ($1::boolean OR id = ANY($2::bigint[]))
                  AND ($3::boolean OR is_active = TRUE)
                ORDER BY lower(name) ASC, id ASC
                """,
                is_superuser,
                actor_team_ids,
                include_inactive,
            )
        return [_team_row(row) for row in rows]

    async def get_team(self, team_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, slug, name, description, is_active, created_at, updated_at
                FROM cms_teams
                WHERE id = $1
                """,
                team_id,
            )
        return _team_row(row) if row else None

    async def create_team(
        self,
        slug: str,
        name: str,
        description: str = "",
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_teams (slug, name, description, updated_at)
                VALUES ($1, $2, $3, NOW())
                RETURNING id, slug, name, description, is_active, created_at, updated_at
                """,
                normalize_team_slug(slug),
                name.strip(),
                (description or "").strip(),
            )
        return _team_row(row)

    async def update_team(
        self,
        team_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cms_teams
                SET name = COALESCE($2, name),
                    description = COALESCE($3, description),
                    is_active = COALESCE($4, is_active),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id, slug, name, description, is_active, created_at, updated_at
                """,
                team_id,
                name.strip() if name is not None else None,
                description.strip() if description is not None else None,
                is_active,
            )
        return _team_row(row) if row else None

    async def _load_admin_teams(self, conn: asyncpg.Connection, admin_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not admin_ids:
            return {}
        rows = await conn.fetch(
            """
            SELECT at.admin_id, t.id, t.slug, t.name, t.description, t.is_active,
                   t.created_at, t.updated_at
            FROM cms_admin_teams at
            JOIN cms_teams t ON t.id = at.team_id
            WHERE at.admin_id = ANY($1::bigint[])
            ORDER BY lower(t.name) ASC, t.id ASC
            """,
            admin_ids,
        )
        team_map: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            team_map.setdefault(int(row["admin_id"]), []).append(_team_row(row))
        return team_map

    async def _replace_admin_teams(
        self,
        conn: asyncpg.Connection,
        admin_id: int,
        team_ids: list[int],
    ) -> None:
        await conn.execute("DELETE FROM cms_admin_teams WHERE admin_id = $1", admin_id)
        if team_ids:
            await conn.executemany(
                """
                INSERT INTO cms_admin_teams (admin_id, team_id)
                SELECT $1, id
                FROM cms_teams
                WHERE id = $2 AND is_active
                ON CONFLICT DO NOTHING
                """,
                [(admin_id, team_id) for team_id in sorted(set(team_ids))],
            )

    async def list_cms_admins(
        self,
        limit: int,
        cursor: Optional[str] = None,
        q: Optional[str] = None,
        active: Optional[bool] = None,
        role_id: Optional[int] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_username = cur.get("username")
        cursor_id = cur.get("id")
        clean_q = q.strip().lower() if q and q.strip() else None
        pattern = f"{clean_q}%" if clean_q else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, username, display_name, is_active,
                       is_superuser, created_at, updated_at, last_login_at
                FROM cms_admin_accounts
                WHERE (
                    $1::text IS NULL
                    OR lower(username) LIKE $1
                    OR lower(COALESCE(display_name, '')) LIKE $1
                )
                  AND ($2::boolean IS NULL OR is_active = $2)
                  AND (
                    $3::bigint IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM cms_admin_roles role_filter
                        WHERE role_filter.admin_id = cms_admin_accounts.id
                          AND role_filter.role_id = $3
                    )
                  )
                  AND (
                      $4::text IS NULL
                      OR (lower(username), id) > ($4::text, $5::bigint)
                  )
                ORDER BY lower(username) ASC, id ASC
                LIMIT $6
                """,
                pattern,
                active,
                role_id,
                cursor_username,
                cursor_id,
                limit + 1,
            )
            page_rows = rows[:limit]
            admin_ids = [int(row["id"]) for row in page_rows]
            roles = []
            team_map: dict[int, list[dict[str, Any]]] = {}
            if admin_ids:
                roles = await conn.fetch(
                    """
                    SELECT ar.admin_id, r.id, r.key, r.name, r.is_system
                    FROM cms_admin_roles ar
                    JOIN cms_roles r ON r.id = ar.role_id
                    WHERE ar.admin_id = ANY($1::bigint[])
                    ORDER BY r.name ASC, r.id ASC
                    """,
                    admin_ids,
                )
                team_map = await self._load_admin_teams(conn, admin_ids)
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_cursor({"username": last["username"].lower(), "id": int(last["id"])})
        role_map: dict[int, list[dict[str, Any]]] = {}
        for role in roles:
            role_map.setdefault(int(role["admin_id"]), []).append(
                {
                    "id": int(role["id"]),
                    "key": role["key"],
                    "name": role["name"],
                    "is_system": role["is_system"],
                }
            )
        admins = []
        for row in page_rows:
            item = _row_to_dict(row)
            admin_id = int(row["id"])
            teams = team_map.get(admin_id, [])
            item["roles"] = role_map.get(admin_id, [])
            item["teams"] = teams
            item["team_ids"] = [int(team["id"]) for team in teams]
            admins.append(item)
        return {"items": admins, "next_cursor": next_cursor, "limit": limit}

    async def list_all_cms_admins(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, username, display_name, is_active,
                       is_superuser, created_at, updated_at, last_login_at
                FROM cms_admin_accounts
                ORDER BY lower(username) ASC, id ASC
                """
            )
            roles = await conn.fetch(
                """
                SELECT ar.admin_id, r.id, r.key, r.name, r.is_system
                FROM cms_admin_roles ar
                JOIN cms_roles r ON r.id = ar.role_id
                ORDER BY r.name ASC, r.id ASC
                """
            )
            team_map = await self._load_admin_teams(conn, [int(row["id"]) for row in rows])
        role_map: dict[int, list[dict[str, Any]]] = {}
        for role in roles:
            role_map.setdefault(int(role["admin_id"]), []).append(
                {
                    "id": int(role["id"]),
                    "key": role["key"],
                    "name": role["name"],
                    "is_system": role["is_system"],
                }
            )
        admins = []
        for row in rows:
            item = _row_to_dict(row)
            admin_id = int(row["id"])
            teams = team_map.get(admin_id, [])
            item["roles"] = role_map.get(admin_id, [])
            item["teams"] = teams
            item["team_ids"] = [int(team["id"]) for team in teams]
            admins.append(item)
        return admins

    async def get_cms_admin_account(self, admin_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, display_name, is_active,
                       is_superuser, created_at, updated_at, last_login_at
                FROM cms_admin_accounts
                WHERE id = $1
                """,
                admin_id,
            )
            if not row:
                return None
            roles = await conn.fetch(
                """
                SELECT r.id, r.key, r.name, r.is_system
                FROM cms_admin_roles ar
                JOIN cms_roles r ON r.id = ar.role_id
                WHERE ar.admin_id = $1
                ORDER BY r.name ASC, r.id ASC
                """,
                admin_id,
            )
            team_map = await self._load_admin_teams(conn, [admin_id])
        item = _row_to_dict(row)
        teams = team_map.get(admin_id, [])
        item["roles"] = [
            {
                "id": int(role["id"]),
                "key": role["key"],
                "name": role["name"],
                "is_system": role["is_system"],
            }
            for role in roles
        ]
        item["teams"] = teams
        item["team_ids"] = [int(team["id"]) for team in teams]
        return item

    async def create_cms_admin(
        self,
        username: str,
        password: str,
        display_name: Optional[str],
        is_active: bool,
        role_ids: list[int],
        team_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                admin_id = await conn.fetchval(
                    """
                    INSERT INTO cms_admin_accounts (
                        username, password_hash, display_name, is_active, is_superuser, updated_at
                    )
                    VALUES ($1, $2, $3, $4, FALSE, NOW())
                    RETURNING id
                    """,
                    username,
                    hash_password(password),
                    display_name,
                    is_active,
                )
                await self._replace_admin_roles(conn, int(admin_id), role_ids)
                await self._replace_admin_teams(conn, int(admin_id), team_ids or [])
        return await self.get_cms_admin_account(int(admin_id)) or {}

    async def update_cms_admin(
        self,
        admin_id: int,
        display_name: Optional[str],
        is_active: bool,
        role_ids: list[int],
        password: Optional[str] = None,
        team_ids: Optional[list[int]] = None,
        *,
        update_teams: bool = False,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if password:
                    row = await conn.fetchrow(
                        """
                        UPDATE cms_admin_accounts
                        SET display_name = $2,
                            is_active = $3,
                            password_hash = $4,
                            updated_at = NOW()
                        WHERE id = $1
                        RETURNING id
                        """,
                        admin_id,
                        display_name,
                        is_active,
                        hash_password(password),
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE cms_admin_accounts
                        SET display_name = $2,
                            is_active = $3,
                            updated_at = NOW()
                        WHERE id = $1
                        RETURNING id
                        """,
                        admin_id,
                        display_name,
                        is_active,
                    )
                if not row:
                    return None
                await self._replace_admin_roles(conn, admin_id, role_ids)
                if update_teams:
                    await self._replace_admin_teams(conn, admin_id, team_ids or [])
        return await self.get_cms_admin_account(admin_id)

    async def _valid_permission_keys(self, permission_keys: list[str]) -> list[str]:
        if not permission_keys:
            return []
        unique_keys = sorted(set(permission_keys))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key
                FROM cms_permissions
                WHERE key = ANY($1::text[])
                ORDER BY key ASC
                """,
                unique_keys,
            )
        return [row["key"] for row in rows]

    async def _replace_role_permissions(
        self,
        conn: asyncpg.Connection,
        role_id: int,
        permission_keys: list[str],
    ) -> None:
        await conn.execute("DELETE FROM cms_role_permissions WHERE role_id = $1", role_id)
        if permission_keys:
            await conn.executemany(
                """
                INSERT INTO cms_role_permissions (role_id, permission_key)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                [(role_id, permission_key) for permission_key in permission_keys],
            )

    async def _replace_admin_roles(
        self,
        conn: asyncpg.Connection,
        admin_id: int,
        role_ids: list[int],
    ) -> None:
        await conn.execute("DELETE FROM cms_admin_roles WHERE admin_id = $1", admin_id)
        if role_ids:
            await conn.executemany(
                """
                INSERT INTO cms_admin_roles (admin_id, role_id)
                SELECT $1, id
                FROM cms_roles
                WHERE id = $2
                ON CONFLICT DO NOTHING
                """,
                [(admin_id, role_id) for role_id in sorted(set(role_ids))],
            )

    _PLAN_SELECT = """
        SELECT p.id, p.name, p.payload, p.created_by, p.created_at, p.updated_at,
               p.team_id, t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
    """

    async def list_sprint_plans(
        self,
        *,
        is_superuser: bool = True,
        actor_team_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
        sort_team: bool = False,
    ) -> list[dict[str, Any]]:
        actor_team_ids = actor_team_ids or []
        order_by = (
            "lower(t.name) ASC NULLS LAST, p.updated_at DESC, p.id DESC"
            if sort_team
            else "p.updated_at DESC, p.id DESC"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                {self._PLAN_SELECT}
                FROM cms_sprint_plans p
                LEFT JOIN cms_teams t ON t.id = p.team_id
                LEFT JOIN cms_admin_accounts a ON a.id = p.created_by
                WHERE ($1::boolean OR p.team_id IS NULL OR p.team_id = ANY($2::bigint[]))
                  AND ($3::bigint IS NULL OR p.team_id IS NOT DISTINCT FROM $3)
                ORDER BY {order_by}
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
        return [_sprint_plan_row(row) for row in rows]

    async def get_sprint_plan(self, plan_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                self._PLAN_SELECT
                + """
                FROM cms_sprint_plans p
                LEFT JOIN cms_teams t ON t.id = p.team_id
                LEFT JOIN cms_admin_accounts a ON a.id = p.created_by
                WHERE p.id = $1
                """,
                plan_id,
            )
        return _sprint_plan_row(row) if row else None

    async def create_sprint_plan(
        self,
        name: str,
        payload: dict[str, Any],
        created_by: Optional[int],
        team_id: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_sprint_plans (name, payload, created_by, team_id)
                VALUES ($1, $2::jsonb, $3, $4)
                RETURNING id
                """,
                name.strip(),
                json.dumps(payload),
                created_by,
                team_id,
            )
        plan = await self.get_sprint_plan(int(row["id"]))
        assert plan is not None
        return plan

    async def update_sprint_plan(
        self,
        plan_id: int,
        name: str,
        payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_sprint_plans
                SET name = $2, payload = $3::jsonb, updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                plan_id,
                name.strip(),
                json.dumps(payload),
            )
        if not updated:
            return None
        return await self.get_sprint_plan(plan_id)

    async def delete_sprint_plan(self, plan_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM cms_sprint_plans WHERE id = $1 RETURNING id",
                plan_id,
            )
        return row is not None

    # -- monthly scope boards --------------------------------------------

    _SCOPE_BOARD_SELECT = """
        SELECT b.id, b.name, b.month, b.capacity_sp, b.capacity_sp_dev, b.capacity_sp_test,
               b.workload_mode, b.plan_jql, b.unplan_jql,
               b.todo_jql, b.test_jql, b.report_type, b.previous_release_jql,
               b.next_release_jql, b.custom_release_name, b.custom_release_jql,
               b.release_queries,
               b.release_comment, b.previous_release_comment, b.next_release_comment, b.custom_release_comment,
               b.plan_epic_key,
               b.scope_sections, b.snapshot,
               b.ai_summary, b.ai_summary_history, b.layout_order,
               b.created_by, b.created_at, b.updated_at, b.team_id,
               t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
        FROM cms_scope_boards b
        LEFT JOIN cms_teams t ON t.id = b.team_id
        LEFT JOIN cms_admin_accounts a ON a.id = b.created_by
    """

    async def list_scope_boards(
        self,
        *,
        is_superuser: bool = True,
        actor_team_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
        sort_team: bool = False,
    ) -> list[dict[str, Any]]:
        actor_team_ids = actor_team_ids or []
        order_by = (
            "lower(t.name) ASC NULLS LAST, b.updated_at DESC, b.id DESC"
            if sort_team
            else "b.updated_at DESC, b.id DESC"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                {self._SCOPE_BOARD_SELECT}
                WHERE ($1::boolean OR b.team_id IS NULL OR b.team_id = ANY($2::bigint[]))
                  AND ($3::bigint IS NULL OR b.team_id IS NOT DISTINCT FROM $3)
                ORDER BY {order_by}
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
        return [_scope_board_row(row) for row in rows]

    async def get_scope_board(self, board_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                self._SCOPE_BOARD_SELECT + " WHERE b.id = $1",
                board_id,
            )
        return _scope_board_row(row) if row else None

    async def create_scope_board(
        self,
        *,
        name: str,
        month: str,
        capacity_sp: float,
        plan_jql: str,
        unplan_jql: str,
        todo_jql: str = "",
        test_jql: str = "",
        workload_mode: str = "sp",
        capacity_sp_dev: Optional[float] = None,
        capacity_sp_test: Optional[float] = None,
        report_type: str = "monthly",
        previous_release_jql: str = "",
        next_release_jql: str = "",
        custom_release_name: str = "",
        custom_release_jql: str = "",
        release_queries: Optional[list[dict[str, Any]]] = None,
        release_comment: str = "",
        previous_release_comment: str = "",
        next_release_comment: str = "",
        custom_release_comment: str = "",
        plan_epic_key: str = "",
        scope_sections: Optional[list[dict[str, Any]]] = None,
        created_by: Optional[int] = None,
        team_id: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_scope_boards
                    (name, month, capacity_sp, capacity_sp_dev, capacity_sp_test, plan_jql, unplan_jql, todo_jql, test_jql, workload_mode,
                     report_type, previous_release_jql, next_release_jql, custom_release_name, custom_release_jql,
                     release_queries,
                     release_comment, previous_release_comment, next_release_comment, custom_release_comment,
                     plan_epic_key, scope_sections, created_by, team_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::jsonb, $17, $18, $19, $20, $21, $22::jsonb, $23, $24)
                RETURNING id
                """,
                name.strip(),
                month.strip(),
                capacity_sp,
                capacity_sp_dev,
                capacity_sp_test,
                plan_jql.strip(),
                unplan_jql.strip(),
                todo_jql.strip(),
                test_jql.strip(),
                workload_mode.strip() or "sp",
                report_type.strip() or "monthly",
                previous_release_jql.strip(),
                next_release_jql.strip(),
                custom_release_name.strip(),
                custom_release_jql.strip(),
                json.dumps(release_queries or []),
                release_comment.strip(),
                previous_release_comment.strip(),
                next_release_comment.strip(),
                custom_release_comment.strip(),
                plan_epic_key.strip().upper(),
                json.dumps(scope_sections) if scope_sections is not None else None,
                created_by,
                team_id,
            )
        board = await self.get_scope_board(int(row["id"]))
        assert board is not None
        return board

    async def update_scope_board(
        self,
        board_id: int,
        *,
        name: str,
        month: str,
        capacity_sp: float,
        plan_jql: str,
        unplan_jql: str,
        todo_jql: str = "",
        test_jql: str = "",
        workload_mode: str = "sp",
        capacity_sp_dev: Optional[float] = None,
        capacity_sp_test: Optional[float] = None,
        report_type: str = "monthly",
        previous_release_jql: str = "",
        next_release_jql: str = "",
        custom_release_name: str = "",
        custom_release_jql: str = "",
        release_queries: Optional[list[dict[str, Any]]] = None,
        release_comment: str = "",
        previous_release_comment: str = "",
        next_release_comment: str = "",
        custom_release_comment: str = "",
        plan_epic_key: str = "",
        scope_sections: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET name = $2,
                    month = $3,
                    capacity_sp = $4,
                    capacity_sp_dev = $5,
                    capacity_sp_test = $6,
                    plan_jql = $7,
                    unplan_jql = $8,
                    todo_jql = $9,
                    test_jql = $10,
                    workload_mode = $11,
                    report_type = $12,
                    previous_release_jql = $13,
                    next_release_jql = $14,
                    custom_release_name = $15,
                    custom_release_jql = $16,
                    release_queries = $17::jsonb,
                    release_comment = $18,
                    previous_release_comment = $19,
                    next_release_comment = $20,
                    custom_release_comment = $21,
                    plan_epic_key = $22,
                    scope_sections = $23::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                name.strip(),
                month.strip(),
                capacity_sp,
                capacity_sp_dev,
                capacity_sp_test,
                plan_jql.strip(),
                unplan_jql.strip(),
                todo_jql.strip(),
                test_jql.strip(),
                workload_mode.strip() or "sp",
                report_type.strip() or "monthly",
                previous_release_jql.strip(),
                next_release_jql.strip(),
                custom_release_name.strip(),
                custom_release_jql.strip(),
                json.dumps(release_queries or []),
                release_comment.strip(),
                previous_release_comment.strip(),
                next_release_comment.strip(),
                custom_release_comment.strip(),
                plan_epic_key.strip().upper(),
                json.dumps(scope_sections) if scope_sections is not None else None,
            )
        if not updated:
            return None
        return await self.get_scope_board(board_id)

    async def update_scope_board_release_comments(
        self,
        board_id: int,
        *,
        release_comment: str = "",
        previous_release_comment: str = "",
        next_release_comment: str = "",
        custom_release_comment: str = "",
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET release_comment = $2,
                    previous_release_comment = $3,
                    next_release_comment = $4,
                    custom_release_comment = $5,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                release_comment.strip(),
                previous_release_comment.strip(),
                next_release_comment.strip(),
                custom_release_comment.strip(),
            )
        if not updated:
            return None
        return await self.get_scope_board(board_id)

    async def update_scope_board_layout(
        self,
        board_id: int,
        layout_order: list[str],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(layout_order, list):
            raise ValueError("layout_order must be a list")
        cleaned: list[str] = []
        for item in layout_order:
            if not isinstance(item, str):
                raise ValueError("layout_order items must be strings")
            key = item.strip()
            if not key:
                raise ValueError("layout_order items must be non-empty strings")
            cleaned.append(key)
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET layout_order = $2::jsonb, updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                json.dumps(cleaned),
            )
        if not updated:
            return None
        return await self.get_scope_board(board_id)

    async def save_scope_board_snapshot(
        self,
        board_id: int,
        snapshot: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET snapshot = $2::jsonb, updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                json.dumps(snapshot),
            )
        if not updated:
            return None
        return await self.get_scope_board(board_id)

    async def save_scope_board_ai_summary(
        self,
        board_id: int,
        ai_summary: dict[str, Any],
        *,
        snapshot_refreshed_at: Optional[str] = None,
        history_limit: int = 15,
    ) -> Optional[dict[str, Any]]:
        entry = {
            "id": str(uuid.uuid4()),
            "generated_at": ai_summary.get("generated_at"),
            "snapshot_refreshed_at": snapshot_refreshed_at,
            "health": ai_summary.get("health"),
            "summary": str(ai_summary.get("summary") or "")[:400],
            "analysis": ai_summary,
        }
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ai_summary_history FROM cms_scope_boards WHERE id = $1",
                board_id,
            )
            if not row:
                return None
            history_raw = _decode_jsonb(row["ai_summary_history"])
            history = history_raw if isinstance(history_raw, list) else []
            history = [entry, *history][: max(1, history_limit)]
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET ai_summary = $2::jsonb,
                    ai_summary_history = $3::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                json.dumps(ai_summary),
                json.dumps(history),
            )
        if not updated:
            return None
        return await self.get_scope_board(board_id)

    async def merge_scope_board_ai_summary_jira_export(
        self,
        board_id: int,
        jira_export: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ai_summary FROM cms_scope_boards WHERE id = $1",
                board_id,
            )
            if not row:
                return None
            current = _decode_jsonb(row["ai_summary"])
            if not isinstance(current, dict):
                return await self.get_scope_board(board_id)
            merged = dict(current)
            merged["jira_export"] = jira_export
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET ai_summary = $2::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                json.dumps(merged),
            )
        if not updated:
            return None
        return await self.get_scope_board(board_id)

    async def delete_scope_board(self, board_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM cms_scope_boards WHERE id = $1 RETURNING id",
                board_id,
            )
        return row is not None

    # -- retrospectives --------------------------------------------------

    _RETRO_SELECT = """
        SELECT r.id, r.title, r.status, r.config, r.snapshot, r.ai_summary,
               r.created_by, r.created_at, r.updated_at, r.team_id,
               t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
        FROM cms_retros r
        LEFT JOIN cms_teams t ON t.id = r.team_id
        LEFT JOIN cms_admin_accounts a ON a.id = r.created_by
    """

    async def list_retros(
        self,
        *,
        is_superuser: bool = True,
        actor_team_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
        sort_team: bool = False,
    ) -> list[dict[str, Any]]:
        actor_team_ids = actor_team_ids or []
        order_by = (
            "lower(t.name) ASC NULLS LAST, r.updated_at DESC, r.id DESC"
            if sort_team
            else "r.updated_at DESC, r.id DESC"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                self._RETRO_SELECT
                + f"""
                 WHERE ($1::boolean OR r.team_id IS NULL OR r.team_id = ANY($2::bigint[]))
                   AND ($3::bigint IS NULL OR r.team_id IS NOT DISTINCT FROM $3)
                 ORDER BY {order_by}
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
        return [_retro_row(row) for row in rows]

    async def get_retro(self, retro_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(self._RETRO_SELECT + " WHERE r.id = $1", retro_id)
        return _retro_row(row) if row else None

    async def create_retro(
        self,
        title: str,
        config: dict[str, Any],
        created_by: Optional[int],
        team_id: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_retros (title, config, status, created_by, team_id)
                VALUES ($1, $2::jsonb, 'draft', $3, $4)
                RETURNING id
                """,
                title.strip(),
                json.dumps(config),
                created_by,
                team_id,
            )
        retro = await self.get_retro(int(row["id"]))
        assert retro is not None
        return retro

    async def update_retro_config(
        self,
        retro_id: int,
        title: str,
        config: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_retros
                SET title = $2, config = $3::jsonb, updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                retro_id,
                title.strip(),
                json.dumps(config),
            )
        if not updated:
            return None
        return await self.get_retro(retro_id)

    async def update_retro_status(self, retro_id: int, status: str) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                "UPDATE cms_retros SET status = $2, updated_at = NOW() WHERE id = $1 RETURNING id",
                retro_id,
                status,
            )
        if not updated:
            return None
        return await self.get_retro(retro_id)

    async def save_retro_snapshot(
        self,
        retro_id: int,
        snapshot: dict[str, Any],
        status: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_retros
                SET snapshot = $2::jsonb,
                    status = COALESCE($3, status),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                retro_id,
                json.dumps(snapshot),
                status,
            )
        if not updated:
            return None
        return await self.get_retro(retro_id)

    async def save_retro_ai_summary(
        self,
        retro_id: int,
        ai_summary: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                "UPDATE cms_retros SET ai_summary = $2::jsonb, updated_at = NOW() WHERE id = $1 RETURNING id",
                retro_id,
                json.dumps(ai_summary),
            )
        if not updated:
            return None
        return await self.get_retro(retro_id)

    async def delete_retro(self, retro_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM cms_retros WHERE id = $1 RETURNING id",
                retro_id,
            )
        return row is not None

    _SESSION_SCOPE = """
        ($1::boolean OR s.team_id IS NULL OR s.team_id = ANY($2::bigint[]))
        AND ($3::bigint IS NULL OR s.team_id IS NOT DISTINCT FROM $3)
    """

    _SESSION_LIST_SELECT = """
        SELECT s.id, s.session_key, s.chat_id, s.topic_id, s.title, s.current_task_index,
               s.participants_count, s.tasks_queue_count, s.history_count,
               s.last_batch_count, s.total_tasks, s.total_votes, s.batch_completed,
               s.is_active, s.current_batch_id, s.current_batch_started_at,
               s.current_task_id, s.tasks_version, s.updated_at, s.team_id
    """

    _SESSION_DETAIL_SELECT = """
        SELECT s.id, s.session_key, s.chat_id, s.topic_id, s.title, s.current_task_index,
               s.participants_count, s.tasks_queue_count, s.history_count,
               s.last_batch_count, s.total_tasks, s.total_votes, s.batch_completed,
               s.is_active, s.current_batch_id, s.current_batch_started_at,
               s.current_task_id, s.tasks_version, s.updated_at, s.team_id,
               t.name AS team_name, t.slug AS team_slug
    """

    def _session_row(self, row: asyncpg.Record, *, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        data = _row_to_dict(row)
        if extra:
            data.update(extra)
        return _attach_team_fields(data, row)

    async def overview(
        self,
        *,
        is_superuser: bool = True,
        actor_team_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
    ) -> dict[str, Any]:
        actor_team_ids = actor_team_ids or []
        scope = self._SESSION_SCOPE
        async with self.pool.acquire() as conn:
            sessions = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*)::bigint AS total_sessions,
                    COUNT(*) FILTER (WHERE s.is_active)::bigint AS active_sessions,
                    COALESCE(SUM(s.total_votes), 0)::bigint AS total_votes,
                    COALESCE(SUM(s.total_tasks), 0)::bigint AS total_tasks
                FROM cms_sessions s
                WHERE s.deleted_at IS NULL
                  AND {scope}
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
            users = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::bigint AS total_users,
                    COUNT(*) FILTER (WHERE is_web)::bigint AS web_users
                FROM cms_users
                """
            )
            # Tokens tied to deleted sessions are excluded so the overview
            # stays consistent with the visible session list.
            tokens = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE wt.expires_at > NOW())::bigint AS active_web_tokens,
                    COUNT(*)::bigint AS total_web_tokens
                FROM cms_web_tokens wt
                LEFT JOIN cms_sessions s ON s.session_key = wt.session_key
                WHERE (s.id IS NULL OR s.deleted_at IS NULL)
                  AND (s.id IS NULL OR {scope})
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
            sprint_plans = await conn.fetchval(
                """
                SELECT COUNT(*)::bigint
                FROM cms_sprint_plans p
                WHERE ($1::boolean OR p.team_id IS NULL OR p.team_id = ANY($2::bigint[]))
                  AND ($3::bigint IS NULL OR p.team_id IS NOT DISTINCT FROM $3)
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
            retros = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::bigint AS total_retros,
                    COUNT(*) FILTER (WHERE status = 'live')::bigint AS live_retros
                FROM cms_retros r
                WHERE ($1::boolean OR r.team_id IS NULL OR r.team_id = ANY($2::bigint[]))
                  AND ($3::bigint IS NULL OR r.team_id IS NOT DISTINCT FROM $3)
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
            votes = await conn.fetchval(
                f"""
                SELECT COUNT(*)::bigint
                FROM cms_votes v
                JOIN cms_sessions s ON s.id = v.session_id
                WHERE s.deleted_at IS NULL
                  AND {scope}
                """,
                is_superuser,
                actor_team_ids,
                team_id,
            )
            return {
                **_row_to_dict(sessions),
                **_row_to_dict(users),
                **_row_to_dict(tokens),
                **_row_to_dict(retros),
                "total_sprint_plans": sprint_plans or 0,
                "votes_rows": votes or 0,
            }

    async def list_sessions(
        self,
        limit: int,
        cursor: Optional[str] = None,
        q: Optional[str] = None,
        active: Optional[bool] = None,
        chat_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        *,
        is_superuser: bool = True,
        actor_team_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
        sort_team: bool = False,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        actor_team_ids = actor_team_ids or []
        cur = decode_cursor(cursor)
        cursor_ts = _decode_cursor_timestamp(cur.get("updated_at"))
        cursor_id = cur.get("id")
        cursor_team_name = cur.get("team_name")
        pattern = f"%{q.strip()}%" if q and q.strip() else None
        scope = self._SESSION_SCOPE
        async with self.pool.acquire() as conn:
            if sort_team:
                rows = await conn.fetch(
                    f"""
                    {self._SESSION_DETAIL_SELECT}
                    FROM cms_sessions s
                    LEFT JOIN cms_teams t ON t.id = s.team_id
                    WHERE s.deleted_at IS NULL
                      AND {scope}
                      AND (
                          $4::text IS NULL
                          OR s.session_key ILIKE $4
                          OR s.current_batch_id ILIKE $4
                          OR s.title ILIKE $4
                      )
                      AND ($5::boolean IS NULL OR s.is_active = $5)
                      AND ($6::bigint IS NULL OR s.chat_id = $6)
                      AND ($7::bigint IS NULL OR s.topic_id IS NOT DISTINCT FROM $7)
                      AND (
                          $8::text IS NULL
                          OR (lower(t.name), s.updated_at, s.id) > ($8::text, $9::timestamptz, $10::bigint)
                      )
                    ORDER BY lower(t.name) ASC NULLS LAST, s.updated_at DESC, s.id DESC
                    LIMIT $11
                    """,
                    is_superuser,
                    actor_team_ids,
                    team_id,
                    pattern,
                    active,
                    chat_id,
                    topic_id,
                    cursor_team_name,
                    cursor_ts,
                    cursor_id,
                    limit + 1,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    {self._SESSION_DETAIL_SELECT}
                    FROM cms_sessions s
                    LEFT JOIN cms_teams t ON t.id = s.team_id
                    WHERE s.deleted_at IS NULL
                      AND {scope}
                      AND (
                          $4::text IS NULL
                          OR s.session_key ILIKE $4
                          OR s.current_batch_id ILIKE $4
                          OR s.title ILIKE $4
                      )
                      AND ($5::boolean IS NULL OR s.is_active = $5)
                      AND ($6::bigint IS NULL OR s.chat_id = $6)
                      AND ($7::bigint IS NULL OR s.topic_id IS NOT DISTINCT FROM $7)
                      AND (
                          $8::timestamptz IS NULL
                          OR (s.updated_at, s.id) < ($8::timestamptz, $9::bigint)
                      )
                    ORDER BY s.updated_at DESC, s.id DESC
                    LIMIT $10
                    """,
                    is_superuser,
                    actor_team_ids,
                    team_id,
                    pattern,
                    active,
                    chat_id,
                    topic_id,
                    cursor_ts,
                    cursor_id,
                    limit + 1,
                )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [self._session_row(row) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            if sort_team:
                next_cursor = encode_cursor(
                    {
                        "team_name": (last["team_name"] or "").lower(),
                        "updated_at": last["updated_at"],
                        "id": int(last["id"]),
                    }
                )
            else:
                next_cursor = encode_cursor({"updated_at": last["updated_at"], "id": int(last["id"])})
        return {"items": items, "next_cursor": next_cursor, "limit": limit}

    async def get_session(
        self,
        session_id: int,
        *,
        include_deleted: bool = False,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                {self._SESSION_DETAIL_SELECT}, s.deleted_at, s.raw
                FROM cms_sessions s
                LEFT JOIN cms_teams t ON t.id = s.team_id
                WHERE s.id = $1
                  AND ($2::boolean OR s.deleted_at IS NULL)
                """,
                session_id,
                include_deleted,
            )
        return self._session_row(row) if row else None

    async def soft_delete_session(self, session_id: int) -> Optional[tuple[int, Optional[int]]]:
        """Mark a session as deleted. Returns (chat_id, topic_id) for callers
        that need to clean up live Redis state, or None if the row was already
        missing/deleted.

        Children (tasks, votes, participants, web tokens, web participants)
        remain in their tables. They naturally disappear from CMS listings via
        the same ``deleted_at`` filter on the session join.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cms_sessions
                SET deleted_at = NOW(), is_active = FALSE, updated_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                RETURNING chat_id, topic_id
                """,
                session_id,
            )
        if not row:
            return None
        return int(row["chat_id"]), (int(row["topic_id"]) if row["topic_id"] is not None else None)

    async def get_session_by_chat(
        self,
        chat_id: int,
        topic_id: Optional[int],
    ) -> Optional[dict[str, Any]]:
        """Lookup a CMS session row by its live identity (chat+topic). Used
        by the app API when serving manager state to attach the stored title.
        Returns ``None`` for missing or soft-deleted sessions."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                {self._SESSION_DETAIL_SELECT}, s.is_active, s.batch_completed
                FROM cms_sessions s
                LEFT JOIN cms_teams t ON t.id = s.team_id
                WHERE s.chat_id = $1
                  AND s.topic_id IS NOT DISTINCT FROM $2
                  AND s.deleted_at IS NULL
                """,
                chat_id,
                topic_id,
            )
        return self._session_row(row) if row else None

    async def set_session_team_by_chat(
        self,
        chat_id: int,
        topic_id: Optional[int],
        team_id: Optional[int],
    ) -> bool:
        """Persist team_id on the cms_sessions row for a live session."""
        key = session_key(chat_id, topic_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_sessions (session_key, chat_id, topic_id, team_id, raw)
                VALUES ($1, $2, $3, $4, '{}'::jsonb)
                ON CONFLICT (session_key) DO UPDATE SET
                    team_id = COALESCE(EXCLUDED.team_id, cms_sessions.team_id),
                    updated_at = NOW()
                WHERE cms_sessions.deleted_at IS NULL
                RETURNING id
                """,
                key,
                chat_id,
                topic_id,
                team_id,
            )
        return row is not None

    async def set_session_title_by_chat(
        self,
        chat_id: int,
        topic_id: Optional[int],
        title: Optional[str],
        *,
        only_if_empty: bool = True,
        team_id: Optional[int] = None,
    ) -> bool:
        """Write a human-readable title onto the cms_sessions row for the given
        chat+topic. Idempotent — safe to call from session-create paths even
        before the background ``sync_session`` job has materialized the row.

        When ``only_if_empty`` is set (default), an existing title is
        preserved so re-running the create flow never clobbers a manually
        renamed session. Returns True when the title is now stored as
        requested.
        """
        normalized = (title or "").strip()
        if not normalized:
            return False
        key = session_key(chat_id, topic_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_sessions (session_key, chat_id, topic_id, title, team_id, raw)
                VALUES ($1, $2, $3, $4, $5, '{}'::jsonb)
                ON CONFLICT (session_key) DO UPDATE SET
                    title = CASE
                        WHEN $6::boolean = FALSE THEN EXCLUDED.title
                        WHEN cms_sessions.title IS NULL OR cms_sessions.title = ''
                            THEN EXCLUDED.title
                        ELSE cms_sessions.title
                    END,
                    team_id = COALESCE(EXCLUDED.team_id, cms_sessions.team_id),
                    updated_at = NOW()
                WHERE cms_sessions.deleted_at IS NULL
                RETURNING id
                """,
                key,
                chat_id,
                topic_id,
                normalized,
                team_id,
                only_if_empty,
            )
        return row is not None

    async def rename_session(
        self,
        session_id: int,
        title: Optional[str],
    ) -> Optional[tuple[int, Optional[int], Optional[str]]]:
        """Update or clear the title on a CMS session row. Returns
        ``(chat_id, topic_id, new_title)`` for callers that audit the change,
        or ``None`` if the row is missing/deleted."""
        normalized = (title or "").strip() or None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cms_sessions
                SET title = $2, updated_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                RETURNING chat_id, topic_id, title
                """,
                session_id,
                normalized,
            )
        if not row:
            return None
        return (
            int(row["chat_id"]),
            (int(row["topic_id"]) if row["topic_id"] is not None else None),
            row["title"],
        )

    async def revoke_web_token(self, token_id: int) -> Optional[str]:
        """Force-expire a web invite token. Returns the token_hash so the
        caller can also wipe the Redis ``web:<token>`` key when known."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cms_web_tokens
                SET expires_at = NOW() - INTERVAL '1 second',
                    last_seen_at = NOW()
                WHERE id = $1 AND expires_at > NOW()
                RETURNING token_hash
                """,
                token_id,
            )
        return row["token_hash"] if row else None

    async def list_session_participants(
        self,
        session_id: int,
        limit: int,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_user_id = cur.get("user_id")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT session_id, user_id, name, role, source, first_seen_at, last_seen_at
                FROM cms_session_participants
                WHERE session_id = $1
                  AND ($2::bigint IS NULL OR user_id > $2)
                ORDER BY user_id ASC
                LIMIT $3
                """,
                session_id,
                cursor_user_id,
                limit + 1,
            )
        return self._paged_rows(rows, limit, "user_id")

    async def list_session_tasks(
        self,
        session_id: int,
        limit: int,
        cursor: Optional[str] = None,
        bucket: Optional[str] = None,
        q: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_id = cur.get("id")
        pattern = f"%{q.strip()}%" if q and q.strip() else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, task_uid, bucket, bucket_index, jira_key,
                       summary, url, story_points, source, votes_count,
                       numeric_avg, numeric_max, completed_at, jql,
                       created_at_text, domain_updated_at, updated_at
                FROM cms_tasks
                WHERE session_id = $1
                  AND ($2::text IS NULL OR bucket = $2)
                  AND ($3::bigint IS NULL OR id > $3)
                  AND (
                      $4::text IS NULL
                      OR task_uid ILIKE $4
                      OR jira_key ILIKE $4
                      OR summary ILIKE $4
                  )
                ORDER BY
                    CASE bucket
                        WHEN 'tasks_queue' THEN 1
                        WHEN 'history' THEN 2
                        WHEN 'last_batch' THEN 3
                        ELSE 4
                    END,
                    bucket_index ASC,
                    id ASC
                LIMIT $5
                """,
                session_id,
                bucket,
                cursor_id,
                pattern,
                limit + 1,
            )
        return self._paged_rows(rows, limit, "id")

    async def list_users(
        self,
        limit: int,
        cursor: Optional[str] = None,
        q: Optional[str] = None,
        role: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_ts = _decode_cursor_timestamp(cur.get("last_seen_at"))
        cursor_user_id = cur.get("user_id")
        if cursor_user_id is not None:
            cursor_user_id = int(cursor_user_id)
        pattern = f"%{q.strip()}%" if q and q.strip() else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH related AS (
                    SELECT user_id, name, role, source = 'web' AS is_web,
                           first_seen_at, last_seen_at
                    FROM cms_session_participants
                    UNION ALL
                    SELECT user_id, name, role, TRUE AS is_web,
                           joined_at AS first_seen_at, joined_at AS last_seen_at
                    FROM cms_web_participants
                ),
                orphan_users AS (
                    SELECT
                        user_id,
                        (array_agg(name ORDER BY last_seen_at DESC))[1] AS name,
                        (array_agg(role ORDER BY last_seen_at DESC))[1] AS role,
                        bool_or(is_web) AS is_web,
                        MIN(first_seen_at) AS first_seen_at,
                        MAX(last_seen_at) AS last_seen_at
                    FROM related
                    WHERE NOT EXISTS (
                        SELECT 1 FROM cms_users existing WHERE existing.user_id = related.user_id
                    )
                    GROUP BY user_id
                ),
                all_users AS (
                    SELECT user_id, name, role, is_web, first_seen_at, last_seen_at
                    FROM cms_users
                    UNION ALL
                    SELECT user_id, name, role, is_web, first_seen_at, last_seen_at
                    FROM orphan_users
                )
                SELECT user_id, name, role, is_web, first_seen_at, last_seen_at
                FROM all_users
                WHERE ($1::text IS NULL OR name ILIKE $1 OR user_id::text ILIKE $1)
                  AND ($2::text IS NULL OR role = $2)
                  AND (
                      $3::timestamptz IS NULL
                      OR (last_seen_at, user_id) < ($3::timestamptz, $4::bigint)
                  )
                ORDER BY last_seen_at DESC, user_id DESC
                LIMIT $5
                """,
                pattern,
                role,
                cursor_ts,
                cursor_user_id,
                limit + 1,
            )
        return self._paged_user_rows(rows, limit)

    def _paged_user_rows(self, rows: list[asyncpg.Record], limit: int) -> dict[str, Any]:
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [_user_row_dict(row) for row in page_rows]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(
                {"last_seen_at": last["last_seen_at"], "user_id": last["user_id"]}
            )
        return {"items": items, "next_cursor": next_cursor, "limit": limit}

    async def hard_delete_user(self, user_id: int, confirm_name: str) -> Optional[dict[str, Any]]:
        """Hard-delete a participant from the CMS read model.

        This intentionally removes the aggregate user row plus CMS-only traces
        that point to the same user_id. It does not mutate live session state in
        Redis, so a participant who joins again can be backfilled as a new CMS
        record later.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow(
                    """
                    SELECT user_id, name, role, is_web, first_seen_at, last_seen_at
                    FROM cms_users
                    WHERE user_id = $1
                    """,
                    user_id,
                )
                if not user:
                    user = await conn.fetchrow(
                        """
                        WITH related AS (
                            SELECT user_id, name, role, source = 'web' AS is_web,
                                   first_seen_at, last_seen_at
                            FROM cms_session_participants
                            WHERE user_id = $1
                            UNION ALL
                            SELECT user_id, name, role, TRUE AS is_web,
                                   joined_at AS first_seen_at, joined_at AS last_seen_at
                            FROM cms_web_participants
                            WHERE user_id = $1
                        )
                        SELECT
                            user_id,
                            (array_agg(name ORDER BY last_seen_at DESC))[1] AS name,
                            (array_agg(role ORDER BY last_seen_at DESC))[1] AS role,
                            bool_or(is_web) AS is_web,
                            MIN(first_seen_at) AS first_seen_at,
                            MAX(last_seen_at) AS last_seen_at
                        FROM related
                        GROUP BY user_id
                        """,
                        user_id,
                    )
                if not user:
                    return None
                if confirm_name.strip() != str(user["name"]):
                    raise ValueError("participant name confirmation mismatch")

                task_rows = await conn.fetch(
                    "SELECT DISTINCT task_id FROM cms_votes WHERE user_id = $1",
                    user_id,
                )
                affected_task_ids = [int(row["task_id"]) for row in task_rows]

                votes_deleted = await conn.fetchval(
                    "WITH deleted AS (DELETE FROM cms_votes WHERE user_id = $1 RETURNING 1) SELECT COUNT(*) FROM deleted",
                    user_id,
                )
                session_participants_deleted = await conn.fetchval(
                    "WITH deleted AS (DELETE FROM cms_session_participants WHERE user_id = $1 RETURNING 1) SELECT COUNT(*) FROM deleted",
                    user_id,
                )
                web_participants_deleted = await conn.fetchval(
                    "WITH deleted AS (DELETE FROM cms_web_participants WHERE user_id = $1 RETURNING 1) SELECT COUNT(*) FROM deleted",
                    user_id,
                )
                await conn.execute("DELETE FROM cms_users WHERE user_id = $1", user_id)

                if affected_task_ids:
                    await conn.execute(
                        """
                        WITH affected(task_id) AS (
                            SELECT unnest($1::bigint[])
                        ),
                        agg AS (
                            SELECT
                                task_id,
                                COUNT(*)::integer AS votes_count,
                                AVG(numeric_value) FILTER (WHERE is_numeric)::numeric AS numeric_avg,
                                MAX(numeric_value) FILTER (WHERE is_numeric)::integer AS numeric_max
                            FROM cms_votes
                            WHERE task_id = ANY($1::bigint[])
                            GROUP BY task_id
                        )
                        UPDATE cms_tasks AS task
                        SET
                            votes_count = COALESCE(agg.votes_count, 0),
                            numeric_avg = agg.numeric_avg,
                            numeric_max = agg.numeric_max,
                            updated_at = NOW()
                        FROM affected
                        LEFT JOIN agg ON agg.task_id = affected.task_id
                        WHERE task.id = affected.task_id
                        """,
                        affected_task_ids,
                    )

        data = _user_row_dict(user)
        data["votes_deleted"] = int(votes_deleted or 0)
        data["session_participants_deleted"] = int(session_participants_deleted or 0)
        data["web_participants_deleted"] = int(web_participants_deleted or 0)
        return data

    async def list_web_tokens(
        self,
        limit: int,
        cursor: Optional[str] = None,
        active: Optional[bool] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_id = cur.get("id")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, token_prefix, token_hash, chat_id, topic_id, session_key,
                       participants_joined, created_at, expires_at, last_seen_at,
                       expires_at > NOW() AS is_active
                FROM cms_web_tokens
                WHERE ($1::boolean IS NULL OR (expires_at > NOW()) = $1)
                  AND ($2::bigint IS NULL OR id < $2)
                ORDER BY id DESC
                LIMIT $3
                """,
                active,
                cursor_id,
                limit + 1,
            )
        return self._paged_rows(rows, limit, "id")

    async def list_web_participants(
        self,
        limit: int,
        cursor: Optional[str] = None,
        token_hash_filter: Optional[str] = None,
        active: Optional[bool] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_id = cur.get("id")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, token_hash, participant_id, user_id, name, role,
                       chat_id, topic_id, joined_at, expires_at,
                       expires_at > NOW() AS is_active
                FROM cms_web_participants
                WHERE ($1::text IS NULL OR token_hash = $1)
                  AND ($2::boolean IS NULL OR (expires_at > NOW()) = $2)
                  AND ($3::bigint IS NULL OR id < $3)
                ORDER BY id DESC
                LIMIT $4
                """,
                token_hash_filter,
                active,
                cursor_id,
                limit + 1,
            )
        return self._paged_rows(rows, limit, "id")

    async def list_votes(
        self,
        limit: int,
        cursor: Optional[str] = None,
        session_id: Optional[int] = None,
        task_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_id = cur.get("id")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.id, v.task_id, v.session_id, v.user_id, v.value,
                       v.is_numeric, v.numeric_value, v.created_at,
                       u.name AS user_name, u.role AS user_role,
                       t.jira_key, t.summary, t.bucket,
                       s.chat_id, s.topic_id, s.session_key
                FROM cms_votes v
                JOIN cms_tasks t ON t.id = v.task_id
                JOIN cms_sessions s ON s.id = v.session_id
                LEFT JOIN cms_users u ON u.user_id = v.user_id
                WHERE ($1::bigint IS NULL OR v.session_id = $1)
                  AND ($2::bigint IS NULL OR v.task_id = $2)
                  AND ($3::bigint IS NULL OR v.user_id = $3)
                  AND ($4::bigint IS NULL OR v.id < $4)
                ORDER BY v.id DESC
                LIMIT $5
                """,
                session_id,
                task_id,
                user_id,
                cursor_id,
                limit + 1,
            )
        return self._paged_rows(rows, limit, "id")

    async def list_audit_events(
        self,
        limit: int,
        cursor: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
        ts_from: Optional[datetime] = None,
        ts_to: Optional[datetime] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_ts = _decode_cursor_timestamp(cur.get("ts"))
        cursor_id = cur.get("id")
        normalized_actor = actor.strip() if actor and actor.strip() else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, action, actor, status, ip, payload
                FROM cms_audit_events
                WHERE ($1::text IS NULL OR action = $1)
                  AND ($2::text IS NULL OR status = $2)
                  AND ($6::text IS NULL OR actor = $6)
                  AND ($7::timestamptz IS NULL OR ts >= $7::timestamptz)
                  AND ($8::timestamptz IS NULL OR ts <= $8::timestamptz)
                  AND (
                      $3::timestamptz IS NULL
                      OR (ts, id) < ($3::timestamptz, $4::bigint)
                  )
                ORDER BY ts DESC, id DESC
                LIMIT $5
                """,
                action,
                status,
                cursor_ts,
                cursor_id,
                limit + 1,
                normalized_actor,
                ts_from,
                ts_to,
            )
        return self._paged_rows(rows, limit, "ts")

    def _paged_rows(self, rows: list[asyncpg.Record], limit: int, cursor_field: str) -> dict[str, Any]:
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [_row_to_dict(row) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            last = items[-1]
            payload = {"id": last.get("id")}
            if "user_id" in last:
                payload["user_id"] = last.get("user_id")
            if cursor_field in last:
                payload[cursor_field] = last[cursor_field]
            elif cursor_field == "user_id":
                payload["user_id"] = last.get("user_id")
            next_cursor = encode_cursor(payload)
        return {"items": items, "next_cursor": next_cursor, "limit": limit}

    async def close(self) -> None:
        await self.pool.close()
