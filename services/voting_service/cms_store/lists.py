"""CMS store mixin: paginated CMS list endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import asyncpg

from services.voting_service.cms_store._helpers import (
    _decode_cursor_timestamp,
    _row_to_dict,
    _user_row_dict,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


class ListsMixin:
    """Users, votes, audit events, and shared pagination helpers."""

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

