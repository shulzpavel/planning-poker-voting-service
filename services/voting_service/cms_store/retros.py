"""CMS store mixin: retros."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import (
    _retro_row,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


class RetrosMixin:
    """Mixin for PostgresCmsStore."""

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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._RETRO_SELECT + " WHERE r.id = $1", retro_id)
        return _retro_row(row) if row else None

    async def create_retro(
        self,
        title: str,
        config: dict[str, Any],
        created_by: Optional[int],
        team_id: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
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
        async with self._pool.acquire() as conn:
            updated = await conn.fetchrow(
                "UPDATE cms_retros SET ai_summary = $2::jsonb, updated_at = NOW() WHERE id = $1 RETURNING id",
                retro_id,
                json.dumps(ai_summary),
            )
        if not updated:
            return None
        return await self.get_retro(retro_id)

    async def delete_retro(self, retro_id: int) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM cms_retros WHERE id = $1 RETURNING id",
                retro_id,
            )
        return row is not None
