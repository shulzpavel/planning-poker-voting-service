"""Partitioned Jira refresh for product radar."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.domain.product_radar import compute_product_radar_snapshot, normalize_radar_issue

ProductRadarRefreshPhase = Literal["start", "partition"]

PRODUCT_RADAR_PARTITION_SIZE = max(5, int(os.getenv("PRODUCT_RADAR_PARTITION_SIZE", "25")))
SCOPE_JQL_MAX_RESULTS = max(1, int(os.getenv("SCOPE_JQL_MAX_RESULTS", "500")))
PRODUCT_RADAR_DONE_WINDOW_DAYS = max(30, int(os.getenv("PRODUCT_RADAR_DONE_WINDOW_DAYS", "120")))


@dataclass
class _RadarJqlFetchResult:
    jql: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    failed: bool = False
    truncated: bool = False


def _extract_project_key(jql: str) -> str:
    match = re.search(r"project\s*=\s*([A-Z][A-Z0-9]+)", jql, re.IGNORECASE)
    return match.group(1).upper() if match else "BTBMGLBL"


def _done_supplement_jql(project: str) -> str:
    return (
        f"project = {project} AND statusCategory = Done "
        f"AND resolved >= -{PRODUCT_RADAR_DONE_WINDOW_DAYS}d ORDER BY resolved DESC"
    )


def _jql_includes_done_window(jql: str) -> bool:
    lowered = jql.lower()
    return "statuscategory = done" in lowered or "statuscategory!=done" in lowered or "resolved >=" in lowered


async def _fetch_radar_batch(
    jql: str,
    client: Any,
    *,
    force_refresh: bool = False,
    enrich_changelog: bool = False,
    changelog_max_issues: int | None = None,
) -> _RadarJqlFetchResult:
    cleaned = (jql or "").strip()
    if not cleaned:
        return _RadarJqlFetchResult(jql="", issues=[])
    try:
        raw_payload = await client.parse_jira_scope_issues(
            cleaned,
            max_results=SCOPE_JQL_MAX_RESULTS,
            force_refresh=force_refresh,
            enrich_changelog=enrich_changelog,
            changelog_max_issues=changelog_max_issues,
        )
    except Exception:
        return _RadarJqlFetchResult(jql=cleaned, issues=[], failed=True)
    if raw_payload is None:
        return _RadarJqlFetchResult(jql=cleaned, issues=[], failed=True)
    if isinstance(raw_payload, dict):
        raw_issues = raw_payload.get("issues") or []
    else:
        raw_issues = raw_payload
    issues = [normalize_radar_issue(issue) for issue in raw_issues]
    return _RadarJqlFetchResult(
        jql=cleaned,
        issues=issues,
        truncated=len(issues) >= SCOPE_JQL_MAX_RESULTS,
    )


def _issues_by_key(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(issue.get("key") or ""): issue for issue in issues if str(issue.get("key") or "").strip()}


def _merge_issues(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = _issues_by_key(existing)
    for issue in incoming:
        key = str(issue.get("key") or "").strip()
        if key:
            merged[key] = issue
    return list(merged.values())


async def _merge_done_supplement(
    issues: list[dict[str, Any]],
    *,
    jql: str,
    client: Any,
    force_refresh: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Always merge recently closed issues for closure metrics."""
    supplement_jql = _done_supplement_jql(_extract_project_key(jql))
    fetch = await _fetch_radar_batch(
        supplement_jql,
        client,
        force_refresh=force_refresh,
        enrich_changelog=False,
    )
    if fetch.failed or not fetch.issues:
        return issues, False
    merged = _merge_issues(issues, fetch.issues)
    return merged, len(merged) > len(issues)


async def refresh_product_radar_partition(
    *,
    jql: str,
    client: Any,
    previous_snapshot: dict[str, Any] | None,
    phase: ProductRadarRefreshPhase,
    partition_size: int,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Load radar snapshot in partitions: start (light) then enrich batches."""
    partition_size = max(1, min(partition_size, 80))
    previous_snapshot = previous_snapshot if isinstance(previous_snapshot, dict) else {}
    refresh_state = dict(previous_snapshot.get("refresh_state") or {})
    issues = list(previous_snapshot.get("issues") or [])

    if phase == "start" or not refresh_state or refresh_state.get("status") == "complete":
        fetch = await _fetch_radar_batch(jql, client, force_refresh=force_refresh, enrich_changelog=False)
        if fetch.failed:
            raise RuntimeError("jira_fetch_failed")
        issues = fetch.issues
        issues, done_merged = await _merge_done_supplement(
            issues,
            jql=jql,
            client=client,
            force_refresh=force_refresh,
        )
        keys = [str(issue.get("key") or "") for issue in issues if str(issue.get("key") or "").strip()]
        refresh_state = {
            "status": "in_progress" if keys else "complete",
            "keys": keys,
            "next_index": 0,
            "partition_size": partition_size,
            "total": len(keys),
            "enriched_count": 0,
        }
        snapshot = compute_product_radar_snapshot(issues)
        snapshot["jql"] = jql
        snapshot["issues"] = issues
        snapshot["truncated"] = fetch.truncated
        snapshot["refresh_state"] = refresh_state
        snapshot["enrichment_complete"] = refresh_state.get("status") == "complete"
        warnings = list(snapshot.get("warnings") or [])
        if fetch.truncated:
            warnings.append(
                {
                    "code": "jql_truncated",
                    "message": f"Jira вернула лимит {SCOPE_JQL_MAX_RESULTS}+ задач — отчёт может быть неполным",
                }
            )
        if done_merged:
            warnings.append(
                {
                    "code": "done_supplement_merged",
                    "message": f"Добавлены закрытые задачи за {PRODUCT_RADAR_DONE_WINDOW_DAYS} дн. для метрик закрытий",
                }
            )
        if not _jql_includes_done_window(jql):
            warnings.append(
                {
                    "code": "jql_missing_done_window",
                    "message": "JQL не включает Done — метрики закрытий дополняются отдельным запросом",
                }
            )
        if warnings:
            snapshot["warnings"] = warnings
        return snapshot

    keys = [str(key) for key in (refresh_state.get("keys") or []) if str(key).strip()]
    next_index = int(refresh_state.get("next_index") or 0)
    if next_index >= len(keys):
        refresh_state["status"] = "complete"
        snapshot = compute_product_radar_snapshot(issues)
        snapshot["jql"] = jql
        snapshot["issues"] = issues
        snapshot["refresh_state"] = refresh_state
        snapshot["enrichment_complete"] = True
        return snapshot

    batch_keys = keys[next_index : next_index + partition_size]
    batch_jql = f"key in ({','.join(batch_keys)}) ORDER BY key"
    fetch = await _fetch_radar_batch(
        batch_jql,
        client,
        force_refresh=force_refresh,
        enrich_changelog=True,
        changelog_max_issues=len(batch_keys),
    )
    if fetch.failed:
        raise RuntimeError("jira_partition_failed")

    issues = _merge_issues(issues, fetch.issues)
    refresh_state["next_index"] = next_index + len(batch_keys)
    refresh_state["enriched_count"] = int(refresh_state.get("enriched_count") or 0) + len(batch_keys)
    if refresh_state["next_index"] >= len(keys):
        refresh_state["status"] = "complete"

    snapshot = compute_product_radar_snapshot(issues)
    snapshot["jql"] = jql
    snapshot["issues"] = issues
    snapshot["refresh_state"] = refresh_state
    snapshot["enrichment_complete"] = refresh_state.get("status") == "complete"
    snapshot["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    return snapshot
