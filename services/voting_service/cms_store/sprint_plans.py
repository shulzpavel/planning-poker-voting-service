"""CMS store mixin: sprint planner persistence."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import _sprint_plan_row


class SprintPlansMixin:
    """Sprint plan CRUD."""

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

