"""CMS store mixin: CMS team CRUD."""

from __future__ import annotations

import json
from typing import Any, Optional

from planning_poker_common.scope.team_questions import (
    empty_team_scope_questions,
    extract_team_scope_questions_from_snapshot,
    normalize_team_scope_questions,
)
from services.voting_service.cms_store._helpers import _decode_jsonb, _team_row, normalize_team_slug


class TeamsMixin:
    """Team listing and mutation."""

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

    async def get_team_scope_questions(self, team_id: int) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT scope_questions FROM cms_teams WHERE id = $1",
                team_id,
            )
        return normalize_team_scope_questions(_decode_jsonb(raw))

    async def save_team_scope_questions(self, team_id: int, questions: dict[str, Any]) -> None:
        payload = normalize_team_scope_questions(questions)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE cms_teams
                SET scope_questions = $2::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                """,
                team_id,
                json.dumps(payload),
            )

    async def backfill_team_scope_questions_from_boards(self, team_id: int) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT snapshot
                FROM cms_scope_boards
                WHERE team_id = $1
                  AND snapshot IS NOT NULL
                ORDER BY updated_at DESC, id DESC
                LIMIT 20
                """,
                team_id,
            )
        merged = empty_team_scope_questions()
        for row in rows:
            snapshot = _decode_jsonb(row["snapshot"])
            if not isinstance(snapshot, dict):
                continue
            candidate = extract_team_scope_questions_from_snapshot(snapshot)
            candidate_manual = len(candidate.get("manual_questions") or [])
            candidate_resolved = len(candidate.get("resolved_questions") or [])
            merged_manual = len(merged.get("manual_questions") or [])
            merged_resolved = len(merged.get("resolved_questions") or [])
            if candidate_manual + candidate_resolved > merged_manual + merged_resolved:
                merged = candidate
        return normalize_team_scope_questions(merged)

