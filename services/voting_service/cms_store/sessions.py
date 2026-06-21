"""CMS store mixin: live session sync and web token writes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.domain.session import Session

from services.voting_service.cms_store._helpers import (
    _deserialize_session,
    _row_to_dict,
    _serialize_session,
    session_key,
    token_hash,
    token_prefix,
)

logger = logging.getLogger(__name__)


class SessionsMixin:
    """Sync Redis session state into the CMS read model."""

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

