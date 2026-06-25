#!/usr/bin/env python3
"""Repair team-level scope questions from historical release board snapshots.

Usage on production (from planning-poker-dev checkout on the server):

  docker compose -f docker-compose.prod.yml --env-file .env exec -T voting-service \
    python3 /app/scripts/repair_team_scope_questions.py --slug igaming-ios --slug igaming-android --apply

Dry-run (default) only prints the merged registry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# voting-service image: vendor + app on PYTHONPATH
sys.path[:0] = ["vendor/planning-poker-common", "."]

from planning_poker_common.scope.team_questions import (  # noqa: E402
    normalize_team_scope_questions,
    union_team_scope_questions,
)
from services.voting_service.cms_store.teams import TeamsMixin  # noqa: E402


class _RepairStore(TeamsMixin):
    def __init__(self, pool: Any) -> None:
        self.pool = pool


async def _run(slugs: list[str], apply: bool) -> int:
    import asyncpg

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 1

    pool = await asyncpg.create_pool(database_url)
    store = _RepairStore(pool)
    exit_code = 0

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, slug, name, scope_questions
                FROM cms_teams
                WHERE slug = ANY($1::text[])
                ORDER BY slug
                """,
                slugs,
            )

        if not rows:
            print(f"error: no teams found for slugs={slugs}", file=sys.stderr)
            return 1

        for row in rows:
            team_id = int(row["id"])
            slug = str(row["slug"])
            current = normalize_team_scope_questions(json.loads(row["scope_questions"] or "{}"))
            repaired = await store.backfill_team_scope_questions_from_boards(team_id)
            merged = union_team_scope_questions(current, repaired)
            current_open = {item["id"]: item.get("summary") for item in current["manual_questions"]}
            merged_open = {item["id"]: item.get("summary") for item in merged["manual_questions"]}
            restored = sorted(set(merged_open) - set(current_open))

            print(f"\n== {slug} (team_id={team_id}) ==")
            print(f"current open: {len(current_open)}")
            print(f"merged open:  {len(merged_open)}")
            if restored:
                print("restored questions:")
                for question_id in restored:
                    print(f"  - {question_id}: {merged_open[question_id]}")
            else:
                print("no additional open questions found in board snapshots")

            if apply and merged != current:
                await store.save_team_scope_questions(team_id, merged)
                print("saved merged registry to cms_teams.scope_questions")
            elif apply:
                print("nothing to save")
    finally:
        await pool.close()

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slug",
        action="append",
        default=["igaming-ios", "igaming-android"],
        help="Team slug to repair (repeatable)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist merged registry (default: dry-run)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.slug, args.apply)))


if __name__ == "__main__":
    main()
