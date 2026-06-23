"""CMS store mixin: daily standups and team rosters."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from services.voting_service.cms_store._helpers import (
    _decode_jsonb,
    _standup_roster_row,
    _standup_row,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


class StandupsMixin:
    """Daily standup CRUD and per-team participant rosters."""

    async def list_standups(
        self,
        *,
        limit: int,
        cursor: Optional[str] = None,
        is_superuser: bool = True,
        actor_team_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        published_only: bool = False,
        sort_team: bool = False,
    ) -> dict[str, Any]:
        actor_team_ids = actor_team_ids or []
        limit = clamp_limit(limit)
        cur = decode_cursor(cursor)
        cursor_date_raw = cur.get("meeting_date")
        cursor_date: Optional[date] = None
        if isinstance(cursor_date_raw, str) and cursor_date_raw.strip():
            try:
                cursor_date = date.fromisoformat(cursor_date_raw.strip())
            except ValueError:
                cursor_date = None
        cursor_id_raw = cur.get("id")
        cursor_id: Optional[int] = None
        if cursor_id_raw is not None:
            try:
                cursor_id = int(cursor_id_raw)
            except (TypeError, ValueError):
                cursor_id = None
        cursor_team_name: Optional[str] = None
        if sort_team:
            raw_team_name = cur.get("team_name")
            if isinstance(raw_team_name, str) and raw_team_name.strip():
                cursor_team_name = raw_team_name.strip().lower()
        order_by = (
            "lower(t.name) ASC NULLS LAST, s.meeting_date DESC, s.id DESC"
            if sort_team
            else "s.meeting_date DESC, s.id DESC"
        )
        async with self.pool.acquire() as conn:
            if sort_team:
                rows = await conn.fetch(
                    f"""
                    {self._STANDUP_SELECT}
                    WHERE ($1::boolean OR s.team_id IS NULL OR s.team_id = ANY($2::bigint[]))
                      AND ($3::bigint IS NULL OR s.team_id IS NOT DISTINCT FROM $3)
                      AND ($4::date IS NULL OR s.meeting_date >= $4)
                      AND ($5::date IS NULL OR s.meeting_date <= $5)
                      AND (NOT $6::boolean OR s.status = 'published')
                      AND (
                          $7::text IS NULL
                          OR (lower(t.name), s.meeting_date, s.id) < ($7::text, $8::date, $9::bigint)
                      )
                    ORDER BY {order_by}
                    LIMIT $10
                    """,
                    is_superuser,
                    actor_team_ids,
                    team_id,
                    date_from,
                    date_to,
                    published_only,
                    cursor_team_name,
                    cursor_date,
                    cursor_id,
                    limit + 1,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    {self._STANDUP_SELECT}
                    WHERE ($1::boolean OR s.team_id IS NULL OR s.team_id = ANY($2::bigint[]))
                      AND ($3::bigint IS NULL OR s.team_id IS NOT DISTINCT FROM $3)
                      AND ($4::date IS NULL OR s.meeting_date >= $4)
                      AND ($5::date IS NULL OR s.meeting_date <= $5)
                      AND (NOT $6::boolean OR s.status = 'published')
                      AND (
                          $7::date IS NULL
                          OR (s.meeting_date, s.id) < ($7::date, $8::bigint)
                      )
                    ORDER BY {order_by}
                    LIMIT $9
                    """,
                    is_superuser,
                    actor_team_ids,
                    team_id,
                    date_from,
                    date_to,
                    published_only,
                    cursor_date,
                    cursor_id,
                    limit + 1,
                )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [_standup_row(row) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            if sort_team:
                next_cursor = encode_cursor(
                    {
                        "team_name": (last["team_name"] or "").lower(),
                        "meeting_date": last["meeting_date"],
                        "id": int(last["id"]),
                    }
                )
            else:
                next_cursor = encode_cursor(
                    {
                        "meeting_date": last["meeting_date"],
                        "id": int(last["id"]),
                    }
                )
        return {"items": items, "next_cursor": next_cursor, "limit": limit}

    async def find_last_standup_jira_due_date(
        self,
        *,
        team_id: int,
        jira_key: str,
        before_meeting_date: Optional[date] = None,
    ) -> Optional[dict[str, str]]:
        """Most recent due_date for ``jira_key`` in earlier team standups."""
        key_upper = (jira_key or "").strip().upper()
        if not key_upper:
            return None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT meeting_date, payload
                FROM cms_standups
                WHERE team_id = $1
                  AND ($2::date IS NULL OR meeting_date < $2)
                ORDER BY meeting_date DESC, id DESC
                LIMIT 50
                """,
                team_id,
                before_meeting_date,
            )
        for row in rows:
            payload = _decode_jsonb(row["payload"])
            if not isinstance(payload, dict):
                continue
            participants = payload.get("participants")
            if not isinstance(participants, list):
                continue
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                items = participant.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_key = str(item.get("jira_key") or "").strip().upper()
                    if item_key != key_upper:
                        continue
                    due = str(item.get("due_date") or "").strip()
                    if not due:
                        continue
                    meeting = row["meeting_date"]
                    meeting_iso = meeting.isoformat() if hasattr(meeting, "isoformat") else str(meeting)
                    return {"due_date": due, "meeting_date": meeting_iso}
        return None

    async def get_standup(self, standup_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                self._STANDUP_SELECT + " WHERE s.id = $1",
                standup_id,
            )
        return _standup_row(row) if row else None

    async def get_standup_for_team_date(self, team_id: int, meeting_date: date) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                self._STANDUP_SELECT + " WHERE s.team_id = $1 AND s.meeting_date = $2",
                team_id,
                meeting_date,
            )
        return _standup_row(row) if row else None

    async def create_standup(
        self,
        *,
        team_id: int,
        meeting_date: date,
        payload: dict[str, Any],
        created_by: Optional[int],
        status: str = "draft",
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_standups (team_id, meeting_date, status, payload, created_by)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                RETURNING id
                """,
                team_id,
                meeting_date,
                status,
                json.dumps(payload),
                created_by,
            )
        standup = await self.get_standup(int(row["id"]))
        assert standup is not None
        return standup

    async def update_standup(
        self,
        standup_id: int,
        *,
        payload: dict[str, Any],
        status: Optional[str] = None,
        published_by: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            if status == "published":
                updated = await conn.fetchrow(
                    """
                    UPDATE cms_standups
                    SET payload = $2::jsonb,
                        status = 'published',
                        published_by = $3,
                        published_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                    RETURNING id
                    """,
                    standup_id,
                    json.dumps(payload),
                    published_by,
                )
            elif status is not None:
                updated = await conn.fetchrow(
                    """
                    UPDATE cms_standups
                    SET payload = $2::jsonb,
                        status = $3,
                        updated_at = NOW()
                    WHERE id = $1
                    RETURNING id
                    """,
                    standup_id,
                    json.dumps(payload),
                    status,
                )
            else:
                updated = await conn.fetchrow(
                    """
                    UPDATE cms_standups
                    SET payload = $2::jsonb,
                        updated_at = NOW()
                    WHERE id = $1
                    RETURNING id
                    """,
                    standup_id,
                    json.dumps(payload),
                )
        if not updated:
            return None
        return await self.get_standup(standup_id)

    async def publish_standup(self, standup_id: int, *, published_by: Optional[int]) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_standups
                SET status = 'published',
                    published_by = $2,
                    published_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                standup_id,
                published_by,
            )
        if not updated:
            return None
        return await self.get_standup(standup_id)

    async def delete_standup(self, standup_id: int) -> bool:
        async with self.pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM cms_standups WHERE id = $1 RETURNING id",
                standup_id,
            )
        return deleted is not None

    async def get_standup_roster(self, team_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT team_id, members, updated_by, created_at, updated_at FROM cms_standup_rosters WHERE team_id = $1",
                team_id,
            )
        return _standup_roster_row(row) if row else None

    async def upsert_standup_roster(
        self,
        team_id: int,
        members: list[dict[str, Any]],
        updated_by: Optional[int],
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_standup_rosters (team_id, members, updated_by, updated_at)
                VALUES ($1, $2::jsonb, $3, NOW())
                ON CONFLICT (team_id) DO UPDATE SET
                    members = EXCLUDED.members,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                RETURNING team_id, members, updated_by, created_at, updated_at
                """,
                team_id,
                json.dumps(members),
                updated_by,
            )
        return _standup_roster_row(row)
