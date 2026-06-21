"""HTTP client adapter for Jira Service microservice."""

import asyncio
import logging
import os
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote, urlencode

import aiohttp

from app.ports.jira_client import JiraClient

logger = logging.getLogger(__name__)


class JiraServiceHttpClient(JiraClient):
    """HTTP client for Jira Service microservice."""

    def __init__(self, base_url: str = None, timeout: int = 30, retry_attempts: int = 3):
        self.base_url = base_url or os.getenv("JIRA_SERVICE_URL", "http://localhost:8001")
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retry_attempts = max(1, retry_attempts)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        """Close aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        session = await self._get_session()
        transient_statuses = {429, 500, 502, 503, 504}
        last_error: Optional[BaseException] = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                async with session.post(url, json=body) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in transient_statuses and attempt < self._retry_attempts:
                        await asyncio.sleep(0.2 * attempt)
                        continue
                    raise RuntimeError(f"Jira Service returned status {resp.status}: {(await resp.text())[:500]}")
            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt >= self._retry_attempts:
                    break
                logger.warning("Jira Service request failed, retrying: url=%s attempt=%s", url, attempt)
                await asyncio.sleep(0.2 * attempt)
        raise RuntimeError(f"Jira Service unavailable: {last_error}") from last_error

    async def _put_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        session = await self._get_session()
        transient_statuses = {429, 500, 502, 503, 504}
        last_error: Optional[BaseException] = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                async with session.put(url, json=body) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in transient_statuses and attempt < self._retry_attempts:
                        await asyncio.sleep(0.2 * attempt)
                        continue
                    raise RuntimeError(f"Jira Service returned status {resp.status}: {(await resp.text())[:500]}")
            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt >= self._retry_attempts:
                    break
                logger.warning("Jira Service request failed, retrying: url=%s attempt=%s", url, attempt)
                await asyncio.sleep(0.2 * attempt)
        raise RuntimeError(f"Jira Service unavailable: {last_error}") from last_error

    async def search_issues(self, jql: str, max_results: int = 100) -> Optional[Dict[str, Any]]:
        """Search issues via Jira Service."""
        url = f"{self.base_url}/api/v1/search"

        try:
            data = await self._post_json(url, {"jql": jql, "max_results": max_results})
            issues = data.get("issues", [])
            return {
                "issues": [
                    {
                        "key": issue["key"],
                        "summary": issue["summary"],
                        "url": issue["url"],
                        "story_points": issue.get("story_points", 0),
                    }
                    for issue in issues
                ]
            }
        except Exception as e:
            raise RuntimeError(f"Failed to search issues via Jira Service: {e}") from e

    def get_issue_url(self, issue_key: str) -> str:
        """Get issue URL (constructs from base URL)."""
        # Extract Jira base URL from service URL or use default
        jira_base = os.getenv("JIRA_URL", "https://jira.example.com")
        return f"{jira_base}/browse/{issue_key}"

    async def update_story_points(self, issue_key: str, story_points: int) -> bool:
        """Update story points via Jira Service.

        Retries are applied for both transient HTTP statuses (429/5xx) and
        transport-level network errors so a flaky connection does not surface
        as a failed Story Points write on the first hiccup.
        """
        url = f"{self.base_url}/api/v1/issue/{issue_key}/story-points"
        session = await self._get_session()
        transient_statuses = {429, 500, 502, 503, 504}
        last_error: Optional[BaseException] = None

        for attempt in range(1, self._retry_attempts + 1):
            try:
                async with session.put(
                    url,
                    json={"issue_key": issue_key, "story_points": story_points},
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status in transient_statuses and attempt < self._retry_attempts:
                        await asyncio.sleep(0.2 * attempt)
                        continue
                    body = (await resp.text())[:500]
                    raise RuntimeError(
                        f"Jira Service returned status {resp.status}: {body}"
                    )
            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt >= self._retry_attempts:
                    break
                logger.warning(
                    "Jira Service update_story_points retrying: key=%s attempt=%s err=%r",
                    issue_key,
                    attempt,
                    exc,
                )
                await asyncio.sleep(0.2 * attempt)

        raise RuntimeError(f"Jira Service unavailable: {last_error}") from last_error

    async def update_story_points_fields(self, issue_key: str, fields: Mapping[str, int]) -> Dict[str, bool]:
        """Update multiple SP fields via Jira Service with partial success."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/story-points/fields"
        session = await self._get_session()
        async with session.put(url, json={"issue_key": issue_key, "fields": dict(fields)}) as resp:
            if resp.status != 200:
                return {field_id: False for field_id in fields}
            data = await resp.json()
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            return {field_id: False for field_id in fields}
        return {str(field_id): bool(ok) for field_id, ok in results.items()}

    async def update_story_points_tracks(
        self, issue_key: str, tracks: Mapping[str, int]
    ) -> tuple[Dict[str, bool], List[str]]:
        """Update SP by semantic track keys; field mapping stays in jira-service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/story-points/tracks"
        data = await self._put_json(url, {"issue_key": issue_key, "tracks": dict(tracks)})
        results_raw = data.get("results") if isinstance(data, dict) else None
        skipped_raw = data.get("skipped_tracks") if isinstance(data, dict) else None
        results = {str(k): bool(v) for k, v in results_raw.items()} if isinstance(results_raw, dict) else {}
        skipped = [str(k) for k in skipped_raw] if isinstance(skipped_raw, list) else []
        return results, skipped

    async def update_due_date(self, issue_key: str, due_date: str) -> bool:
        """Update Jira due date via Jira Service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/due-date"
        session = await self._get_session()
        async with session.put(url, json={"issue_key": issue_key, "due_date": due_date}) as resp:
            if resp.status != 200:
                body = (await resp.text())[:500]
                raise RuntimeError(f"Jira Service returned status {resp.status}: {body}")
            data = await resp.json()
        return bool(data.get("success")) if isinstance(data, dict) else False

    async def update_significance(self, issue_key: str, significance: int) -> bool:
        """Update grooming significance via Jira Service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/significance"
        session = await self._get_session()
        async with session.put(url, json={"issue_key": issue_key, "significance": significance}) as resp:
            if resp.status != 200:
                body = (await resp.text())[:500]
                raise RuntimeError(f"Jira Service returned status {resp.status}: {body}")
            data = await resp.json()
        return bool(data.get("success")) if isinstance(data, dict) else False

    async def clear_significance(self, issue_key: str) -> bool:
        """Clear grooming significance via Jira Service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/significance"
        session = await self._get_session()
        async with session.delete(url) as resp:
            if resp.status != 200:
                body = (await resp.text())[:500]
                raise RuntimeError(f"Jira Service returned status {resp.status}: {body}")
            data = await resp.json()
        return bool(data.get("success")) if isinstance(data, dict) else False

    async def add_issue_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        """Append a comment through Jira Service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/comment"
        try:
            return await self._post_json(url, {"text": text})
        except Exception as e:
            raise RuntimeError(f"Failed to add Jira comment via Jira Service: {e}") from e

    async def add_issue_comment_adf(self, issue_key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Append an ADF comment through Jira Service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/comment/adf"
        try:
            return await self._post_json(url, {"body": dict(body)})
        except Exception as e:
            raise RuntimeError(f"Failed to add Jira ADF comment via Jira Service: {e}") from e

    async def update_issue_comment_adf(
        self,
        issue_key: str,
        comment_id: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update an existing Jira comment with ADF through Jira Service."""
        url = f"{self.base_url}/api/v1/issue/{issue_key}/comment/{comment_id}/adf"
        try:
            return await self._put_json(url, {"body": dict(body)})
        except Exception as e:
            raise RuntimeError(f"Failed to update Jira ADF comment via Jira Service: {e}") from e

    async def parse_jira_request(self, text: str, max_results: int = 500) -> Optional[List[Dict[str, Any]]]:
        """Parse Jira request via Jira Service."""
        url = f"{self.base_url}/api/v1/parse"

        try:
            data = await self._post_json(url, {"jql": text, "max_results": max_results})
            issues = data.get("issues", [])
            return [
                {
                    "key": issue["key"],
                    "summary": issue["summary"],
                    "url": issue["url"],
                    "story_points": issue.get("story_points", 0),
                }
                for issue in issues
            ]
        except Exception as e:
            raise RuntimeError(f"Failed to parse Jira request via Jira Service: {e}") from e

    async def parse_jira_scope_issues(
        self,
        text: str,
        max_results: int = 500,
        *,
        force_refresh: bool = False,
        milestone_status_targets: Optional[list[str]] = None,
        enrich_changelog: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Fetch scope-dashboard issues via Jira Service."""
        url = f"{self.base_url}/api/v1/search/scope"
        payload: dict[str, Any] = {
            "jql": text,
            "max_results": max_results,
            "force_refresh": force_refresh,
            "enrich_changelog": enrich_changelog,
        }
        if milestone_status_targets:
            payload["milestone_status_targets"] = milestone_status_targets
        try:
            data = await self._post_json(url, payload)
            issues = data.get("issues", [])
            mapped = [
                {
                    "key": issue["key"],
                    "summary": issue["summary"],
                    "url": issue["url"],
                    "story_points": issue.get("story_points"),
                    "story_points_source": issue.get("story_points_source"),
                    "story_points_plan": issue.get("story_points_plan"),
                    "story_points_fact": issue.get("story_points_fact"),
                    "story_points_dev": issue.get("story_points_dev"),
                    "story_points_test": issue.get("story_points_test"),
                    "story_point_estimate": issue.get("story_point_estimate"),
                    "status": issue.get("status") or {},
                    "issue_type": {"name": issue.get("issue_type") or ""},
                    "labels": issue.get("labels") or [],
                    "epic_labels": issue.get("epic_labels") or [],
                    "created": issue.get("created"),
                    "updated": issue.get("updated"),
                    "status_changed_at": issue.get("status_changed_at"),
                    "status_entered_at": issue.get("status_entered_at"),
                    "epic_linked_at": issue.get("epic_linked_at"),
                    "start_date": issue.get("start_date"),
                    "due_date": issue.get("due_date"),
                    "resolution": issue.get("resolution"),
                    "resolution_date": issue.get("resolution_date"),
                    "parent_key": issue.get("parent_key"),
                    "epic_key": issue.get("epic_key"),
                    "linked_epic_key": issue.get("linked_epic_key"),
                    "priority": issue.get("priority"),
                    "assignee": issue.get("assignee"),
                    "developer": issue.get("developer"),
                    "developer_source": issue.get("developer_source"),
                    "role_contributors": issue.get("role_contributors") or {},
                    "jira_role_assignees": issue.get("jira_role_assignees") or {},
                    "role_contributors_list": issue.get("role_contributors_list") or [],
                    "role_workload_items": issue.get("role_workload_items") or [],
                    "role_evidence": issue.get("role_evidence") or [],
                    "story_points_front": issue.get("story_points_front"),
                    "story_points_back": issue.get("story_points_back"),
                    "story_points_qa": issue.get("story_points_qa"),
                    "reporter": issue.get("reporter"),
                    "components": issue.get("components") or [],
                    "fix_versions": issue.get("fix_versions") or [],
                    "versions": issue.get("versions") or [],
                    "sprints": issue.get("sprints") or [],
                    "sprint": issue.get("sprint"),
                    "team": issue.get("team"),
                    "team_labels": issue.get("team_labels") or [],
                    "plan_status": issue.get("plan_status"),
                    "plan_change_reason": issue.get("plan_change_reason"),
                    "plan_change_reasons": issue.get("plan_change_reasons") or [],
                    "final_priority": issue.get("final_priority"),
                    "significance": issue.get("significance"),
                    "severity": issue.get("severity"),
                    "domain": issue.get("domain"),
                    "request_type": issue.get("request_type"),
                    "checklist_progress": issue.get("checklist_progress"),
                    "status_durations": issue.get("status_durations") or {},
                    "status_bucket_durations": issue.get("status_bucket_durations") or {},
                    "status_flow_bucket_map": issue.get("status_flow_bucket_map") or {},
                    "status_segments": issue.get("status_segments") or [],
                    "current_status_assignee": issue.get("current_status_assignee"),
                    "current_status_days": issue.get("current_status_days"),
                    "last_comment": issue.get("last_comment"),
                    "last_comment_author": issue.get("last_comment_author"),
                    "last_comment_at": issue.get("last_comment_at"),
                }
                for issue in issues
            ]
            return {
                "issues": mapped,
                "jira_role_fields_configured": data.get("jira_role_fields_configured") or {},
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch scope issues via Jira Service: {e}") from e

    async def _get_json(self, url: str) -> dict[str, Any]:
        session = await self._get_session()
        transient_statuses = {429, 500, 502, 503, 504}
        last_error: Optional[BaseException] = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in transient_statuses and attempt < self._retry_attempts:
                        await asyncio.sleep(0.2 * attempt)
                        continue
                    raise RuntimeError(f"Jira Service returned status {resp.status}: {(await resp.text())[:500]}")
            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt >= self._retry_attempts:
                    break
                logger.warning("Jira Service request failed, retrying: url=%s attempt=%s", url, attempt)
                await asyncio.sleep(0.2 * attempt)
        raise RuntimeError(f"Jira Service unavailable: {last_error}") from last_error

    async def get_version(self, version_id: str, *, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch Jira fix version metadata via Jira Service."""
        cleaned = str(version_id or "").strip()
        if not cleaned:
            return None
        url = f"{self.base_url}/api/v1/version/{quote(cleaned, safe='')}"
        if force_refresh:
            url = f"{url}?force_refresh=1"
        try:
            data = await self._get_json(url)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Jira version fetch failed version_id=%s error=%s", cleaned, exc)
            return None

    async def resolve_version(
        self,
        *,
        project_key: str = "",
        version_id: str = "",
        version_name: str = "",
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Resolve Jira version metadata by id or project + name."""
        if not (version_id or version_name):
            return None
        params = urlencode(
            {
                "project_key": project_key or "",
                "version_id": version_id or "",
                "version_name": version_name or "",
            }
        )
        url = f"{self.base_url}/api/v1/version/resolve?{params}"
        if force_refresh:
            url = f"{url}&force_refresh=1"
        try:
            data = await self._get_json(url)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning(
                "Jira version resolve failed project=%s version_id=%s version_name=%s error=%s",
                project_key,
                version_id,
                version_name,
                exc,
            )
            return None
