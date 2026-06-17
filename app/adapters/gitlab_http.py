"""Read-only GitLab API client for scope role attribution."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def gitlab_configured() -> bool:
    return bool(_gitlab_base_url() and _gitlab_token())


def _gitlab_base_url() -> str:
    return (os.getenv("GITLAB_BASE_URL") or os.getenv("GITLAB_URL") or "").strip().rstrip("/")


def _gitlab_token() -> str:
    return (os.getenv("GITLAB_TOKEN") or os.getenv("GITLAB_PRIVATE_TOKEN") or "").strip()


def _gitlab_group_id() -> str:
    return (os.getenv("GITLAB_GROUP_ID") or "").strip()


def _gitlab_search_per_page() -> int:
    return max(1, min(100, int(os.getenv("GITLAB_SEARCH_PER_PAGE", "20"))))


def _gitlab_timeout() -> aiohttp.ClientTimeout:
    seconds = max(5, int(os.getenv("GITLAB_TIMEOUT_SECONDS", "20")))
    return aiohttp.ClientTimeout(total=seconds)


def _gitlab_batch_size() -> int:
    return max(1, int(os.getenv("SCOPE_GITLAB_BATCH_SIZE", "8")))


class GitLabHttpClient:
    """Search GitLab merge requests and commits linked to Jira issue keys."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or _gitlab_base_url()).rstrip("/")
        self.token = token or _gitlab_token()
        self.group_id = (group_id if group_id is not None else _gitlab_group_id()).strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._project_paths: dict[int, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_gitlab_timeout())
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
        if not self.enabled:
            return None
        session = await self._get_session()
        url = f"{self.base_url}/api/v4{path}"
        headers = {"PRIVATE-TOKEN": self.token}
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 401:
                    logger.warning("GitLab API authentication failed")
                    return None
                if response.status == 404:
                    return None
                if response.status != 200:
                    body = await response.text()
                    logger.warning("GitLab API error status=%s path=%s body=%s", response.status, path, body[:200])
                    return None
                return await response.json()
        except aiohttp.ClientError as exc:
            logger.warning("GitLab API request failed path=%s error=%s", path, exc)
            return None

    async def _project_path(self, project_id: int) -> str:
        cached = self._project_paths.get(project_id)
        if cached:
            return cached
        payload = await self._request(f"/projects/{project_id}")
        path = ""
        if isinstance(payload, dict):
            path = str(payload.get("path_with_namespace") or payload.get("path") or "")
        self._project_paths[project_id] = path
        return path

    def _search_path(self, scope: str) -> str:
        if self.group_id:
            return f"/groups/{quote(self.group_id, safe='')}/search"
        return "/search"

    async def _search(self, scope: str, query: str) -> list[dict[str, Any]]:
        payload = await self._request(
            self._search_path(scope),
            params={"scope": scope, "search": query, "per_page": _gitlab_search_per_page()},
        )
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def search_merge_requests(self, issue_key: str) -> list[dict[str, Any]]:
        rows = await self._search("merge_requests", issue_key)
        matched: list[dict[str, Any]] = []
        for row in rows:
            haystack = " ".join(
                str(row.get(field) or "")
                for field in ("title", "description", "source_branch", "target_branch")
            )
            if issue_key.upper() in haystack.upper() or issue_key.upper() in str(row.get("web_url") or "").upper():
                matched.append(row)
            elif _ISSUE_KEY_RE.search(haystack) and issue_key.upper() in haystack.upper():
                matched.append(row)
        if not matched and rows:
            matched = rows
        return matched

    async def search_commits(self, issue_key: str) -> list[dict[str, Any]]:
        rows = await self._search("commits", issue_key)
        matched: list[dict[str, Any]] = []
        for row in rows:
            haystack = " ".join(str(row.get(field) or "") for field in ("title", "message", "short_id"))
            if issue_key.upper() in haystack.upper():
                matched.append(row)
        return matched

    async def fetch_issue_evidence_raw(self, issue_key: str) -> dict[str, Any]:
        if not self.enabled or not issue_key:
            return {"merge_requests": [], "commits": []}
        merge_requests, commits = await asyncio.gather(
            self.search_merge_requests(issue_key),
            self.search_commits(issue_key),
        )
        enriched_mrs: list[dict[str, Any]] = []
        for row in merge_requests:
            project_id = row.get("project_id")
            project_path = ""
            if isinstance(project_id, int):
                project_path = await self._project_path(project_id)
            enriched_mrs.append({**row, "project_path": project_path})

        enriched_commits: list[dict[str, Any]] = []
        for row in commits:
            project_id = row.get("project_id")
            project_path = ""
            if isinstance(project_id, int):
                project_path = await self._project_path(project_id)
            enriched_commits.append({**row, "project_path": project_path})

        return {"merge_requests": enriched_mrs, "commits": enriched_commits}

    async def fetch_evidence_by_keys(self, issue_keys: list[str]) -> dict[str, dict[str, Any]]:
        keys = [str(key).strip().upper() for key in issue_keys if str(key).strip()]
        unique = list(dict.fromkeys(keys))
        if not unique or not self.enabled:
            return {}

        batch_size = _gitlab_batch_size()
        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique), batch_size):
            batch = unique[start : start + batch_size]
            batch_results = await asyncio.gather(*(self.fetch_issue_evidence_raw(key) for key in batch))
            for key, payload in zip(batch, batch_results):
                results[key] = payload if isinstance(payload, dict) else {"merge_requests": [], "commits": []}
        return results
