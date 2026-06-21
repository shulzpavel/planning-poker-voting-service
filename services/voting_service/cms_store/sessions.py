"""CMS store mixin: sessions."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.domain.session import Session

from services.voting_service.cms_store._helpers import (
    _attach_team_fields,
    _decode_cursor_timestamp,
    _row_to_dict,
    _serialize_session,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    session_key,
    token_hash,
    token_prefix,
)

logger = logging.getLogger(__name__)


class SessionsMixin:
    """Mixin for PostgresCmsStore."""

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

            async with self._pool.acquire() as conn:
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
            async with self._pool.acquire() as conn:
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
            async with self._pool.acquire() as conn:
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
            async with self._pool.acquire() as conn:
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

    def _session_row(self, row: asyncpg.Record, *, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        data = _row_to_dict(row)
        if extra:
            data.update(extra)
        return _attach_team_fields(data, row)

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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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

    async def list_web_tokens(
        self,
        limit: int,
        cursor: Optional[str] = None,
        active: Optional[bool] = None,
    ) -> dict[str, Any]:
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_id = cur.get("id")
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
