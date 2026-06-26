"""Partitioned product radar refresh."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.domain.product_radar_refresh import refresh_product_radar_partition


class _FakeClient:
    def __init__(self, batches: dict[str, list[dict]]):
        self.batches = batches
        self.calls: list[tuple[str, bool, int | None]] = []

    async def parse_jira_scope_issues(
        self,
        jql: str,
        *,
        max_results: int,
        force_refresh: bool = False,
        enrich_changelog: bool = False,
        changelog_max_issues: int | None = None,
    ):
        self.calls.append((jql, enrich_changelog, changelog_max_issues))
        for pattern, issues in self.batches.items():
            if pattern in jql:
                return {"issues": issues}
        if "project = X" in jql:
            return {"issues": self.batches.get("start", [])}
        return {"issues": []}

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_refresh_start_loads_light_list():
    client = _FakeClient(
        {
            "start": [
                {"key": "A-1", "summary": "One", "status": "В работе", "status_category": "indeterminate"},
                {"key": "A-2", "summary": "Two", "status": "К выполнению", "status_category": "new"},
            ],
        }
    )
    snapshot = await refresh_product_radar_partition(
        jql="project = X",
        client=client,
        previous_snapshot=None,
        phase="start",
        partition_size=25,
    )
    assert snapshot["issue_count"] == 2
    assert snapshot["refresh_state"]["status"] == "in_progress"
    assert snapshot["refresh_state"]["keys"] == ["A-1", "A-2"]
    assert snapshot["enrichment_complete"] is False
    assert client.calls[0][1] is False


@pytest.mark.asyncio
async def test_refresh_partition_enriches_batch():
    light_issues = [
        {"key": "A-1", "summary": "One", "status": "В работе", "status_category": "indeterminate"},
        {"key": "A-2", "summary": "Two", "status": "К выполнению", "status_category": "new"},
    ]
    enriched = [
        {
            **light_issues[0],
            "subtasks": [{"key": "A-1-1", "status": "В работе", "status_category": "indeterminate"}],
            "status_segments": [{"status": "В работе", "bucket": "in_work", "days": 2.0}],
        }
    ]
    client = _FakeClient({"start": light_issues, "A-1": enriched})
    start = await refresh_product_radar_partition(
        jql="project = X",
        client=client,
        previous_snapshot=None,
        phase="start",
        partition_size=1,
    )
    part = await refresh_product_radar_partition(
        jql="project = X",
        client=client,
        previous_snapshot=start,
        phase="partition",
        partition_size=1,
    )
    assert part["refresh_state"]["next_index"] == 1
    assert part["refresh_state"]["enriched_count"] == 1
    issues_by_key = {issue["key"]: issue for issue in part["issues"]}
    assert issues_by_key["A-1"].get("subtasks")
    assert client.calls[-1][1] is True
    assert client.calls[-1][2] == 1
