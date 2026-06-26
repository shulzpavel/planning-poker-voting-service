"""Precomputed cross-team blocking rows for radar UI."""

from __future__ import annotations

from typing import Any


def build_snapshot_blocking_feed(
    signals: list[dict[str, Any]],
    team_blocking: dict[str, Any] | None,
    *,
    limit: int = 48,
) -> dict[str, Any]:
    """Build a compact blocking feed for the CMS UI."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for signal in signals:
        if str(signal.get("kind") or "") != "cross_team_block":
            continue
        blocked_key = str(signal.get("issue_key") or "").strip()
        blocker_key = str(signal.get("blocker_key") or "").strip()
        row_id = f"signal:{blocked_key}:{blocker_key}"
        if not blocked_key or row_id in seen:
            continue
        seen.add(row_id)
        rows.append(
            {
                "id": row_id,
                "category": "blocking",
                "severity": str(signal.get("severity") or "high"),
                "blockingTeam": str(
                    signal.get("blocking_team") or signal.get("blocker_team") or blocker_key or "—"
                ),
                "blockedTeam": str(signal.get("blocked_team") or "—"),
                "blockedKey": blocked_key,
                "blockerKey": blocker_key or None,
                "blockerStatus": str(signal.get("blocker_status") or signal.get("status") or ""),
                "title": str(signal.get("title") or "Блокировка другой командой"),
                "detail": str(signal.get("detail") or ""),
                "issueUrl": signal.get("issue_url"),
            }
        )

    for team in (team_blocking or {}).get("teams") or []:
        if not isinstance(team, dict):
            continue
        team_key = str(team.get("key") or team.get("label") or "—")
        team_label = str(team.get("label") or team_key)
        for item in team.get("items") or []:
            if not isinstance(item, dict):
                continue
            blocked_key = str(item.get("issue_key") or "").strip()
            blocker_key = str(item.get("blocker_key") or "").strip()
            row_id = f"team:{team_key}:{blocked_key}:{blocker_key}"
            if not blocked_key or row_id in seen:
                continue
            seen.add(row_id)
            rows.append(
                {
                    "id": row_id,
                    "category": "blocking",
                    "severity": "high",
                    "blockingTeam": team_label,
                    "blockedTeam": str(item.get("blocked_team") or item.get("team") or "—"),
                    "blockedKey": blocked_key,
                    "blockerKey": blocker_key or None,
                    "blockerStatus": str(item.get("blocker_status") or ""),
                    "title": f"{team_label} блокирует",
                    "detail": str(item.get("summary") or item.get("detail") or blocked_key),
                    "issueUrl": item.get("issue_url"),
                }
            )

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(
        key=lambda row: (
            severity_rank.get(str(row.get("severity") or "low"), 9),
            str(row.get("blockingTeam") or ""),
            str(row.get("blockedKey") or ""),
        )
    )
    trimmed = rows[:limit]
    return {"total": len(rows), "blockings": trimmed}


def _team_blocking_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    analytics = snapshot.get("analytics")
    if not isinstance(analytics, dict):
        return None
    periods = analytics.get("periods")
    if isinstance(periods, dict):
        all_period = periods.get("all")
        if isinstance(all_period, dict):
            team_blocking = all_period.get("team_blocking")
            if isinstance(team_blocking, dict):
                return team_blocking
    team_blocking = analytics.get("team_blocking")
    return team_blocking if isinstance(team_blocking, dict) else None


def ensure_snapshot_blocking_feed(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach blocking_feed when missing or empty (legacy snapshots, lean clients)."""
    if not isinstance(snapshot, dict):
        return snapshot
    existing = snapshot.get("blocking_feed")
    if isinstance(existing, dict) and int(existing.get("total") or 0) > 0:
        return snapshot

    signals = [item for item in (snapshot.get("signals") or []) if isinstance(item, dict)]
    team_blocking = _team_blocking_from_snapshot(snapshot)
    blocking_feed = build_snapshot_blocking_feed(signals, team_blocking)
    if int(blocking_feed.get("total") or 0) <= 0:
        return snapshot

    enriched = dict(snapshot)
    enriched["blocking_feed"] = blocking_feed
    return enriched
