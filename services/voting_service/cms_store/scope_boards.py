"""CMS store mixin: monthly scope boards."""

from __future__ import annotations

import json
from typing import Any, Optional

from services.voting_service.cms_store._helpers import _scope_board_row


class ScopeBoardsMixin:
    """Scope board CRUD and snapshot updates."""

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
                {self._SCOPE_BOARD_LIST_SELECT}
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

    async def update_scope_board_flow_pace_chart_order(
        self,
        board_id: int,
        chart_order: list[str],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(chart_order, list):
            raise ValueError("chart_order must be a list")
        cleaned: list[str] = []
        for item in chart_order:
            if not isinstance(item, str):
                raise ValueError("chart_order items must be strings")
            key = item.strip()
            if not key:
                raise ValueError("chart_order items must be non-empty strings")
            cleaned.append(key)
        normalized = normalize_flow_pace_chart_order(cleaned)
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE cms_scope_boards
                SET flow_pace_chart_order = $2::jsonb, updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                board_id,
                json.dumps(normalized),
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

    async def get_scope_board_ai_jira_export(self, board_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ai_summary->'jira_export' AS jira_export,
                       ai_summary->>'health' AS health
                FROM cms_scope_boards
                WHERE id = $1
                """,
                board_id,
            )
        if not row:
            return None
        jira_export = _decode_jsonb(row["jira_export"]) if row["jira_export"] is not None else None
        return {
            "jira_export": jira_export if isinstance(jira_export, dict) else None,
            "health": row["health"],
        }

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

