"""CMS store mixin: CMS session overview and paginated reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import asyncpg

from services.voting_service.cms_store._helpers import (
    _attach_team_fields,
    _decode_cursor_timestamp,
    _row_to_dict,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


class SessionsListMixin:
    """Overview metrics and paginated CMS session queries."""

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
        scope_boards = await conn.fetchval(
            """
            SELECT COUNT(*)::bigint
            FROM cms_scope_boards b
            WHERE ($1::boolean OR b.team_id IS NULL OR b.team_id = ANY($2::bigint[]))
              AND ($3::bigint IS NULL OR b.team_id IS NOT DISTINCT FROM $3)
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
            "total_scope_boards": scope_boards or 0,
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

async def get_web_token(self, token_id: int) -> Optional[dict[str, Any]]:
    """Load a web invite token and the parent session's team_id."""
    async with self.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT wt.id, wt.token_hash, wt.token_prefix, wt.chat_id, wt.topic_id,
                   wt.session_key, wt.expires_at > NOW() AS is_active, s.team_id
            FROM cms_web_tokens wt
            LEFT JOIN cms_sessions s
              ON s.chat_id = wt.chat_id
             AND s.topic_id IS NOT DISTINCT FROM wt.topic_id
             AND s.deleted_at IS NULL
            WHERE wt.id = $1
            """,
            token_id,
        )
    if not row:
        return None
    data = dict(row)
    team_id = data.get("team_id")
    data["team_id"] = int(team_id) if team_id is not None else None
    return data

async def get_user_session_team_ids(self, user_id: int) -> list[Optional[int]]:
    """Distinct team_ids for sessions a participant touched in the CMS read model."""
    async with self.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT s.team_id
            FROM cms_sessions s
            WHERE s.deleted_at IS NULL
              AND (
                EXISTS (
                    SELECT 1
                    FROM cms_session_participants sp
                    WHERE sp.session_id = s.id AND sp.user_id = $1
                )
                OR EXISTS (
                    SELECT 1
                    FROM cms_votes v
                    WHERE v.session_id = s.id AND v.user_id = $1
                )
                OR EXISTS (
                    SELECT 1
                    FROM cms_web_participants wp
                    WHERE wp.user_id = $1
                      AND wp.chat_id = s.chat_id
                      AND wp.topic_id IS NOT DISTINCT FROM s.topic_id
                )
              )
            """,
            user_id,
        )
    return [int(row["team_id"]) if row["team_id"] is not None else None for row in rows]

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

