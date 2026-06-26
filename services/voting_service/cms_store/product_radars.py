"""CMS store mixin: product radar boards."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import _decode_jsonb

DEFAULT_PRODUCT_RADAR_JQL = (
    "project = BTBMGLBL AND (statusCategory != Done OR resolved >= -120d) "
    "ORDER BY updated DESC"
)
DEFAULT_PRODUCT_RADAR_NAME = "Продукт BTBMGLBL"


def _product_radar_row(row: Any) -> dict[str, Any]:
    snapshot = _decode_jsonb(row["snapshot"]) if row.get("snapshot") is not None else None
    return {
        "id": int(row["id"]),
        "name": str(row["name"] or ""),
        "jql": str(row["jql"] or ""),
        "snapshot": snapshot,
        "created_by": row.get("created_by"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


class ProductRadarsMixin:
    """Product radar CRUD and snapshot updates."""

    async def list_product_radars(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, jql,
                       CASE
                         WHEN snapshot IS NULL THEN NULL
                         ELSE jsonb_build_object(
                           'issue_count', snapshot->'issue_count',
                           'active_count', snapshot->'active_count',
                           'health_status', snapshot->'health_status',
                           'refreshed_at', snapshot->'refreshed_at'
                         )
                       END AS snapshot,
                       created_by, created_at, updated_at
                FROM cms_product_radars
                ORDER BY updated_at DESC, id DESC
                """
            )
        return [_product_radar_row(row) for row in rows]

    async def get_product_radar(self, radar_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, jql, snapshot, created_by, created_at, updated_at
                FROM cms_product_radars
                WHERE id = $1
                """,
                radar_id,
            )
        return _product_radar_row(row) if row else None

    async def ensure_default_product_radar(self, *, created_by: Optional[int] = None) -> dict[str, Any]:
        existing = await self.list_product_radars()
        if existing:
            return existing[0]
        return await self.create_product_radar(
            name=DEFAULT_PRODUCT_RADAR_NAME,
            jql=DEFAULT_PRODUCT_RADAR_JQL,
            created_by=created_by,
        )

    async def create_product_radar(
        self,
        *,
        name: str,
        jql: str,
        created_by: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO cms_product_radars (name, jql, created_by)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                name.strip(),
                jql.strip(),
                created_by,
            )
        radar = await self.get_product_radar(int(row["id"]))
        assert radar is not None
        return radar

    async def update_product_radar(
        self,
        radar_id: int,
        *,
        name: Optional[str] = None,
        jql: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        fields: list[str] = []
        values: list[Any] = []
        if name is not None:
            fields.append(f"name = ${len(values) + 1}")
            values.append(name.strip())
        if jql is not None:
            fields.append(f"jql = ${len(values) + 1}")
            values.append(jql.strip())
        if not fields:
            return await self.get_product_radar(radar_id)
        fields.append("updated_at = NOW()")
        values.append(radar_id)
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE cms_product_radars SET {', '.join(fields)} WHERE id = ${len(values)}",
                *values,
            )
        return await self.get_product_radar(radar_id)

    async def delete_product_radar(self, radar_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM cms_product_radars WHERE id = $1", radar_id)
        return str(result).endswith("1")

    async def save_product_radar_snapshot(self, radar_id: int, snapshot: dict[str, Any]) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE cms_product_radars
                SET snapshot = $2::jsonb, updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                radar_id,
                json.dumps(snapshot),
            )
        if not row:
            return None
        return await self.get_product_radar(radar_id)
