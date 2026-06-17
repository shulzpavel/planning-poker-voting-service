"""HTTP adapter for Jira API client."""

import asyncio
import html
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

import aiohttp

from app.ports.jira_client import JiraClient
from app.utils.jira_html import html_to_plain_text, sanitize_jira_html
from app.utils.jira_changelog import (
    epic_linked_at,
    infer_developer_from_changelog,
    infer_qa_from_changelog,
    qa_assignee_from_current_status_allowed,
    status_entered_at,
    status_entered_at_for_targets,
    status_matches_any_target,
)
from app.utils.jira_role_contributors import (
    build_subtask_workload_items,
    infer_role_contributors_from_comments,
    merge_role_contributors,
    role_contributors_list,
)
from app.utils.gitlab_role_evidence import (
    build_gitlab_api_contributors,
    build_gitlab_api_workload_items,
    unresolved_reason_for_role,
)
from app.adapters.gitlab_http import GitLabHttpClient, gitlab_configured
from app.utils.jira_text import adf_to_plain_text, truncate_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONTEXT_CHARS = 16000
_URL_RE = re.compile(r"https?://[^\s<>'\")]+", re.IGNORECASE)


def _plan_change_reason_field_id() -> str:
    return os.getenv("JIRA_PLAN_CHANGE_REASON_FIELD", "customfield_13047").strip()


def _plan_status_field_id() -> str:
    return os.getenv("JIRA_PLAN_STATUS_FIELD", "customfield_13045").strip()


def _due_date_field_id() -> str:
    return os.getenv("JIRA_DUE_DATE_FIELD", "duedate").strip()


def _due_date_fallback_field_id() -> str:
    return os.getenv("JIRA_DUE_DATE_FALLBACK_FIELD", "customfield_10624").strip()


def _jira_custom_field_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        cleaned = raw.strip()
        return [cleaned] if cleaned else []
    if isinstance(raw, dict):
        label = str(raw.get("value") or raw.get("name") or "").strip()
        return [label] if label else []
    if isinstance(raw, list):
        values: list[str] = []
        for item in raw:
            values.extend(_jira_custom_field_values(item))
        return values
    cleaned = str(raw).strip()
    return [cleaned] if cleaned else []


def _jira_user_field_names(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        cleaned = raw.strip()
        return [cleaned] if cleaned else []
    if isinstance(raw, dict):
        for key in ("displayName", "name", "value"):
            value = str(raw.get(key) or "").strip()
            if value:
                return [value]
        return []
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            names.extend(_jira_user_field_names(item))
        return names
    cleaned = str(raw).strip()
    return [cleaned] if cleaned else []


def _normalise_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _default_confluence_base_url(jira_base_url: str) -> str:
    parsed = urlparse(jira_base_url or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/wiki"


def _confluence_page_id_from_url(url: str, confluence_base_url: str) -> Optional[str]:
    parsed_url = urlparse(url)
    parsed_base = urlparse(confluence_base_url)
    if not parsed_url.scheme or not parsed_url.netloc or not parsed_base.netloc:
        return None
    if parsed_url.netloc.lower() != parsed_base.netloc.lower():
        return None

    query_page_id = (parse_qs(parsed_url.query).get("pageId") or [None])[0]
    if query_page_id and query_page_id.isdigit():
        return query_page_id

    path = parsed_url.path
    match = re.search(r"/pages/(\d+)(?:/|$)", path)
    if match:
        return match.group(1)
    return None


def _extract_urls_from_adf(node: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(node, list):
        for item in node:
            urls.extend(_extract_urls_from_adf(item))
        return urls
    if not isinstance(node, dict):
        return urls

    for mark in node.get("marks") or []:
        if isinstance(mark, dict):
            attrs = mark.get("attrs") or {}
            href = attrs.get("href")
            if isinstance(href, str):
                urls.append(href)
    for child in node.get("content") or []:
        urls.extend(_extract_urls_from_adf(child))
    return urls


def extract_confluence_page_ids(
    *,
    confluence_base_url: str,
    description: str = "",
    description_html: str = "",
    description_adf: Any = None,
) -> list[str]:
    """Extract safe Confluence page ids from Jira text/html/ADF fields."""
    if not confluence_base_url:
        return []
    candidates = []
    candidates.extend(_URL_RE.findall(description or ""))
    candidates.extend(_URL_RE.findall(description_html or ""))
    candidates.extend(_extract_urls_from_adf(description_adf))

    page_ids: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        page_id = _confluence_page_id_from_url(candidate.rstrip(".,;"), confluence_base_url)
        if page_id and page_id not in seen:
            page_ids.append(page_id)
            seen.add(page_id)
    return page_ids


class JiraHttpClient(JiraClient):
    """HTTP implementation of Jira client."""

    def __init__(
        self,
        base_url: str,
        username: str,
        api_token: str,
        story_points_field: str,
        timeout: int = 30,
        retry_attempts: int = 3,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.api_token = api_token
        self.story_points_field = story_points_field
        self._key_pattern = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retry_attempts = max(1, retry_attempts)
        self.confluence_base_url = _normalise_base_url(
            os.getenv("CONFLUENCE_BASE_URL") or _default_confluence_base_url(base_url)
        )
        self.confluence_username = os.getenv("CONFLUENCE_USERNAME") or username
        self.confluence_api_token = os.getenv("CONFLUENCE_API_TOKEN") or api_token
        self._confluence_max_pages = max(0, int(os.getenv("CONFLUENCE_MAX_PAGES_PER_ISSUE", "2")))

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        """Close aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        api_versions: Optional[Iterable[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Execute async HTTP request to Jira, trying multiple API versions."""
        if not self.api_token or not self.username:
            return None

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = aiohttp.BasicAuth(self.username, self.api_token)
        method = method.upper()
        versions = list(api_versions or ["3"])

        session = await self._get_session()
        transient_statuses = {429, 500, 502, 503, 504}

        for version in versions:
            url = f"{self.base_url}/rest/api/{version}/{endpoint}"
            for attempt in range(1, self._retry_attempts + 1):
                try:
                    async with session.request(
                        method, url, auth=auth, headers=headers, json=data
                    ) as response:
                        # Handle redirects and deprecated endpoints
                        if response.status in {301, 302, 303, 307, 308, 404, 410}:
                            if version != versions[-1]:
                                break
                            if response.status in {404, 410}:
                                return None

                        if response.status in transient_statuses and attempt < self._retry_attempts:
                            logger.warning(
                                "Jira API transient status, retrying: status=%s endpoint=%s attempt=%s",
                                response.status,
                                endpoint,
                                attempt,
                            )
                            await asyncio.sleep(0.25 * attempt)
                            continue

                        response.raise_for_status()

                        if response.status == 204 or response.content_length == 0:
                            return {"success": True}

                        return await response.json()

                except aiohttp.ClientResponseError as error:
                    status = error.status
                    # If API version is unavailable (e.g., 410), try next version
                    if status in {301, 302, 303, 307, 308, 404, 410} and version != versions[-1]:
                        break
                    # For authentication errors (401), provide detailed information without leaking token
                    if status == 401:
                        try:
                            body = await error.response.text()
                        except Exception:
                            body = "<no body>"
                        logger.error(
                            "Jira API authentication error: status=401 url=%s username=%s token=%s body=%s",
                            url,
                            self.username,
                            "***",
                            body,
                        )
                        return None
                    if status in {404, 403}:
                        return None
                    try:
                        body = await error.response.text()
                    except Exception:
                        body = "<no body>"
                    logger.warning("Jira API error: status=%s url=%s body=%s", status, url, body)
                    if status in transient_statuses and attempt < self._retry_attempts:
                        await asyncio.sleep(0.25 * attempt)
                        continue
                except aiohttp.ClientError as error:
                    logger.warning("Jira API request failed: url=%s attempt=%s error=%s", url, attempt, error)
                    if attempt < self._retry_attempts:
                        await asyncio.sleep(0.25 * attempt)
                        continue
                    break
                except ValueError as error:
                    logger.warning("Jira API JSON parsing error: url=%s error=%r", url, error)
                    break

        return None

    async def search_issues(self, jql: str, max_results: int = 100) -> Optional[Dict[str, Any]]:
        """Execute search for issues using arbitrary JQL."""
        max_results = max(1, min(max_results, 1000))
        page_size = min(100, max_results)
        issues: List[Dict[str, Any]] = []
        start_at = 0

        while len(issues) < max_results:
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": min(page_size, max_results - len(issues)),
                "fields": ["summary", self.story_points_field, "key"],
            }
            result = await self._make_request("POST", "search", payload, api_versions=["3"])
            page = result.get("issues", []) if result else []
            if not page:
                break
            issues.extend(page)
            total = result.get("total") if result else None
            start_at += len(page)
            if len(page) < payload["maxResults"] or (isinstance(total, int) and start_at >= total):
                break

        if issues:
            return {"issues": issues[:max_results], "maxResults": max_results}

        legacy_issues: List[Dict[str, Any]] = []
        next_page_token: Optional[str] = None
        while len(legacy_issues) < max_results:
            legacy_payload: Dict[str, Any] = {
                "jql": jql,
                "maxResults": min(page_size, max_results - len(legacy_issues)),
                "fields": ["summary", self.story_points_field, "key"],
            }
            if next_page_token:
                legacy_payload["nextPageToken"] = next_page_token
            legacy_result = await self._make_request("POST", "search/jql", legacy_payload, api_versions=["3"])
            page = legacy_result.get("issues", []) if legacy_result else []
            if not page:
                break
            legacy_issues.extend(page)
            next_page_token = legacy_result.get("nextPageToken") if legacy_result else None
            if not next_page_token or len(page) < legacy_payload["maxResults"]:
                break

        if legacy_issues:
            if legacy_issues and "id" in legacy_issues[0] and "key" not in legacy_issues[0]:
                detailed_issues = []
                for issue in legacy_issues[:max_results]:
                    detail = await self._make_request("GET", f"issue/{issue['id']}", api_versions=["3", "2"])
                    if detail:
                        detailed_issues.append(detail)
                if detailed_issues:
                    return {"issues": detailed_issues}
            return {"issues": legacy_issues[:max_results], "maxResults": max_results}

        return None

    _SCOPE_SEARCH_FIELDS = [
        "summary",
        "status",
        "issuetype",
        "labels",
        "created",
        "updated",
        "priority",
        "assignee",
        "reporter",
        "components",
        "fixVersions",
        "versions",
        "duedate",
        "resolution",
        "resolutiondate",
        "statuscategorychangedate",
        "parent",
        "issuelinks",
        "customfield_10013",  # Epic Link
        "customfield_10018",  # Sprint
        "customfield_10001",  # Team
        "customfield_10584",  # Severity
        "customfield_11905",  # Team labels
        "customfield_13012",  # Domain
        "customfield_10020",  # Request Type
        "customfield_10100",  # Checklist Progress %
        "customfield_10030",  # Story point estimate
        "customfield_10624",  # Due date fallback / "Срок исполнения"
        "customfield_11242",  # Story points (alt)
        "customfield_11407",  # Story Points_ plan
        "customfield_11408",  # Story Points_ fact
        "customfield_12978",  # Story Points dev
        "customfield_12979",  # Story Points test
        "comment",
    ]

    _SCOPE_STORY_POINT_FIELDS = [
        "customfield_10030",
        "customfield_11242",
        "customfield_11407",
        "customfield_12978",
        "customfield_12979",
    ]

    def _scope_search_field_ids(self) -> list[str]:
        from config import (
            JIRA_BACK_ASSIGNEE_FIELD,
            JIRA_FRONT_ASSIGNEE_FIELD,
            JIRA_QA_ASSIGNEE_FIELD,
            JIRA_SP_BACK_FIELD,
            JIRA_SP_DEV_FIELD,
            JIRA_SP_FRONT_FIELD,
            JIRA_SP_QA_FIELD,
            JIRA_SP_TEST_FIELD,
        )

        fields = [*self._SCOPE_SEARCH_FIELDS, self.story_points_field, "key"]
        for field_id in (
            _plan_status_field_id(),
            _plan_change_reason_field_id(),
            _due_date_field_id(),
            _due_date_fallback_field_id(),
            "customfield_13401",
        ):
            if field_id and field_id not in fields:
                fields.append(field_id)
        for field_id in (
            JIRA_SP_DEV_FIELD or "customfield_12978",
            JIRA_SP_TEST_FIELD or "customfield_12979",
            JIRA_SP_FRONT_FIELD,
            JIRA_SP_BACK_FIELD,
            JIRA_SP_QA_FIELD,
        ):
            if field_id and field_id not in fields:
                fields.append(field_id)
        for field_id in (JIRA_FRONT_ASSIGNEE_FIELD, JIRA_BACK_ASSIGNEE_FIELD, JIRA_QA_ASSIGNEE_FIELD):
            if field_id and field_id not in fields:
                fields.append(field_id)
        return fields

    async def search_scope_issues(self, jql: str, max_results: int = 500) -> Optional[Dict[str, Any]]:
        """Search issues with fields needed for the monthly scope dashboard."""
        max_results = max(1, min(max_results, 1000))
        page_size = min(100, max_results)
        fields = self._scope_search_field_ids()
        issues: List[Dict[str, Any]] = []
        start_at = 0

        while len(issues) < max_results:
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": min(page_size, max_results - len(issues)),
                "fields": fields,
            }
            result = await self._make_request("POST", "search", payload, api_versions=["3"])
            page = result.get("issues", []) if result else []
            if not page:
                break
            issues.extend(page)
            total = result.get("total") if result else None
            start_at += len(page)
            if len(page) < payload["maxResults"] or (isinstance(total, int) and start_at >= total):
                break

        if issues:
            page = issues[:max_results]
            return {
                "issues": page,
                "maxResults": max_results,
                "truncated": len(page) >= max_results,
            }

        legacy_issues: List[Dict[str, Any]] = []
        next_page_token: Optional[str] = None
        while len(legacy_issues) < max_results:
            legacy_payload: Dict[str, Any] = {
                "jql": jql,
                "maxResults": min(page_size, max_results - len(legacy_issues)),
                "fields": fields,
            }
            if next_page_token:
                legacy_payload["nextPageToken"] = next_page_token
            legacy_result = await self._make_request("POST", "search/jql", legacy_payload, api_versions=["3"])
            page = legacy_result.get("issues", []) if legacy_result else []
            if not page:
                break
            legacy_issues.extend(page)
            next_page_token = legacy_result.get("nextPageToken") if legacy_result else None
            if not next_page_token or len(page) < legacy_payload["maxResults"]:
                break

        if legacy_issues:
            hydrated = await self._hydrate_legacy_issue_rows(legacy_issues, max_results)
            if hydrated:
                return {
                    "issues": hydrated,
                    "maxResults": max_results,
                    "truncated": len(hydrated) >= max_results,
                }
        return {"issues": [], "maxResults": max_results, "truncated": False}

    async def _fetch_issue_labels_by_key(self, keys: list[str]) -> dict[str, list[str]]:
        unique_keys = sorted({key.strip() for key in keys if key and key.strip()})
        if not unique_keys:
            return {}

        labels_by_key: dict[str, list[str]] = {}
        for key in unique_keys:
            result = await self._make_request(
                "GET",
                f"issue/{quote(key, safe='')}?fields=labels",
                api_versions=["3", "2"],
            )
            fields = result.get("fields") if isinstance(result, dict) and isinstance(result.get("fields"), dict) else {}
            labels_by_key[key] = [str(label) for label in (fields.get("labels") or []) if label]
        return labels_by_key

    @staticmethod
    def _scope_child_of_epic_key(fields: dict[str, Any]) -> str:
        links = fields.get("issuelinks")
        if not isinstance(links, list):
            return ""
        for link in links:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type") if isinstance(link.get("type"), dict) else {}
            candidates = (
                ("outward", link.get("outwardIssue")),
                ("inward", link.get("inwardIssue")),
            )
            for direction, linked_issue in candidates:
                relation = str(link_type.get(direction) or "").strip().lower()
                if "child of" not in relation or not isinstance(linked_issue, dict):
                    continue
                linked_fields = linked_issue.get("fields") if isinstance(linked_issue.get("fields"), dict) else {}
                linked_type = linked_fields.get("issuetype") if isinstance(linked_fields.get("issuetype"), dict) else {}
                type_name = str(linked_type.get("name") or "").strip().lower()
                if type_name and type_name not in {"epic", "эпик"}:
                    continue
                return str(linked_issue.get("key") or "")
        return ""

    @staticmethod
    def _role_contributors_from_jira_fields(fields: dict[str, Any]) -> dict[str, dict[str, str]]:
        from config import JIRA_BACK_ASSIGNEE_FIELD, JIRA_FRONT_ASSIGNEE_FIELD, JIRA_QA_ASSIGNEE_FIELD

        contributors: dict[str, dict[str, str]] = {}
        for role, field_id in (
            ("front", JIRA_FRONT_ASSIGNEE_FIELD),
            ("back", JIRA_BACK_ASSIGNEE_FIELD),
            ("qa", JIRA_QA_ASSIGNEE_FIELD),
        ):
            if not field_id:
                continue
            names = _jira_user_field_names(fields.get(field_id))
            if names:
                contributors[role] = {"name": names[0], "source": "jira_field"}
        return contributors

    async def _hydrate_legacy_issue_rows(
        self,
        issues: List[Dict[str, Any]],
        max_results: int,
    ) -> List[Dict[str, Any]]:
        """Legacy search/jql may return rows with id but no key — hydrate before parsing."""
        if not issues:
            return []
        if issues[0].get("key"):
            return issues[:max_results]
        detailed: List[Dict[str, Any]] = []
        for issue in issues[:max_results]:
            issue_id = issue.get("id")
            if not issue_id:
                continue
            fields = quote(",".join(self._scope_search_field_ids()), safe=",")
            detail = await self._make_request(
                "GET",
                f"issue/{issue_id}?fields={fields}",
                api_versions=["3", "2"],
            )
            if detail:
                detailed.append(detail)
        return detailed

    def _scope_issue_from_raw(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        issue_key = issue.get("key")
        if not issue_key:
            return {}
        fields = issue.get("fields", {})
        summary = fields.get("summary", issue_key)
        raw_story_points = fields.get(self.story_points_field)
        story_points_source = self.story_points_field if isinstance(raw_story_points, (int, float)) else None
        if not isinstance(raw_story_points, (int, float)):
            for field_id in self._SCOPE_STORY_POINT_FIELDS:
                value = fields.get(field_id)
                if isinstance(value, (int, float)):
                    raw_story_points = value
                    story_points_source = field_id
                    break
        story_points = raw_story_points if isinstance(raw_story_points, (int, float)) else None

        status_field = fields.get("status") or {}
        status_category_field = status_field.get("statusCategory") or {}
        issue_type_field = fields.get("issuetype") or {}
        parent_field = fields.get("parent") or {}
        priority_field = fields.get("priority") or {}
        assignee_field = fields.get("assignee") or {}
        reporter_field = fields.get("reporter") or {}
        team_field = fields.get("customfield_10001") or {}
        plan_status_field = fields.get(_plan_status_field_id()) or {}
        plan_change_reasons = _jira_custom_field_values(fields.get(_plan_change_reason_field_id()))
        final_priority_field = fields.get("customfield_13401") or {}
        severity_field = fields.get("customfield_10584") or {}
        domain_field = fields.get("customfield_13012") or {}
        request_type_field = fields.get("customfield_10020") or {}
        resolution_field = fields.get("resolution") or {}

        def _names(values: Any) -> List[str]:
            if not isinstance(values, list):
                return []
            result: List[str] = []
            for item in values:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("value")
                    if name:
                        result.append(str(name))
                elif item:
                    result.append(str(item))
            return result

        sprint_names = _names(fields.get("customfield_10018"))
        comment_field = fields.get("comment")
        last_comment = self._extract_last_comment(comment_field)
        comments = comment_field.get("comments") if isinstance(comment_field, dict) else []
        role_from_comments = infer_role_contributors_from_comments(comments if isinstance(comments, list) else [])
        role_from_jira_fields = self._role_contributors_from_jira_fields(fields)

        from config import JIRA_SP_BACK_FIELD, JIRA_SP_DEV_FIELD, JIRA_SP_FRONT_FIELD, JIRA_SP_QA_FIELD, JIRA_SP_TEST_FIELD

        linked_epic_key = self._scope_child_of_epic_key(fields)

        return {
            "key": issue_key,
            "summary": summary,
            "url": self.get_issue_url(issue_key),
            "story_points": story_points,
            "story_points_source": story_points_source,
            "story_points_plan": fields.get("customfield_11407"),
            "story_points_fact": fields.get("customfield_11408"),
            "story_points_dev": fields.get(JIRA_SP_DEV_FIELD or "customfield_12978"),
            "story_points_test": fields.get(JIRA_SP_TEST_FIELD or "customfield_12979"),
            "story_points_front": fields.get(JIRA_SP_FRONT_FIELD) if JIRA_SP_FRONT_FIELD else None,
            "story_points_back": fields.get(JIRA_SP_BACK_FIELD) if JIRA_SP_BACK_FIELD else None,
            "story_points_qa": fields.get(JIRA_SP_QA_FIELD) if JIRA_SP_QA_FIELD else None,
            "story_point_estimate": fields.get("customfield_10030"),
            "status": {
                "name": status_field.get("name") or "",
                "category": status_category_field.get("key") or status_category_field.get("name") or "",
            },
            "issue_type": {"name": issue_type_field.get("name") or ""},
            "labels": [str(label) for label in (fields.get("labels") or []) if label],
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "status_changed_at": fields.get("statuscategorychangedate"),
            "due_date": fields.get("duedate") or fields.get(_due_date_field_id()) or fields.get(_due_date_fallback_field_id()),
            "resolution": resolution_field.get("name") or "",
            "resolution_date": fields.get("resolutiondate"),
            "parent_key": parent_field.get("key") or "",
            "epic_key": fields.get("customfield_10013") or "",
            "linked_epic_key": linked_epic_key,
            "priority": priority_field.get("name") or "",
            "assignee": assignee_field.get("displayName") or "",
            "reporter": reporter_field.get("displayName") or "",
            "components": _names(fields.get("components")),
            "fix_versions": _names(fields.get("fixVersions")),
            "versions": _names(fields.get("versions")),
            "sprints": sprint_names,
            "sprint": sprint_names[-1] if sprint_names else "",
            "team": team_field.get("name") or team_field.get("value") or team_field.get("title") or "",
            "team_labels": [str(label) for label in (fields.get("customfield_11905") or []) if label],
            "plan_status": plan_status_field.get("value") or plan_status_field.get("name") or "",
            "plan_change_reason": ", ".join(plan_change_reasons),
            "plan_change_reasons": plan_change_reasons,
            "final_priority": final_priority_field.get("value") or "",
            "severity": severity_field.get("value") or "",
            "domain": domain_field.get("value") or "",
            "request_type": request_type_field.get("requestType", {}).get("name")
            if isinstance(request_type_field.get("requestType"), dict)
            else "",
            "checklist_progress": fields.get("customfield_10100"),
            "role_contributors_from_comments": role_from_comments,
            "role_contributors_from_jira_fields": role_from_jira_fields,
            "_comments": comments if isinstance(comments, list) else [],
            **last_comment,
        }

    def _extract_last_comment(self, comment_field: Any) -> Dict[str, Any]:
        if not isinstance(comment_field, dict):
            return {}
        comments = comment_field.get("comments")
        if not isinstance(comments, list) or not comments:
            return {}
        last = max(comments, key=lambda item: str(item.get("created") or ""))
        body = last.get("body")
        text = truncate_text(adf_to_plain_text(body), 1200) if body else ""
        author = last.get("author") or {}
        return {
            "last_comment": text,
            "last_comment_author": str(author.get("displayName") or ""),
            "last_comment_at": last.get("created"),
        }

    def _scope_subtask_from_raw(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        issue_key = issue.get("key")
        if not issue_key:
            return {}
        fields = issue.get("fields", {})
        comment_field = fields.get("comment")
        comments = comment_field.get("comments") if isinstance(comment_field, dict) else []
        return {
            "key": issue_key,
            "summary": str(fields.get("summary") or issue_key),
            "labels": [str(label) for label in (fields.get("labels") or []) if label],
            "comments": comments if isinstance(comments, list) else [],
        }

    async def _fetch_subtasks_by_parent(self, parent_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        keys = [str(key).strip() for key in parent_keys if str(key).strip()]
        if not keys:
            return grouped

        chunk_size = max(1, int(os.getenv("SCOPE_JIRA_SUBTASK_BATCH_SIZE", "40")))
        for start in range(0, len(keys), chunk_size):
            batch = keys[start : start + chunk_size]
            jql = f"parent in ({','.join(batch)})"
            response = await self.search_scope_issues(jql, max_results=500)
            for issue in response.get("issues", []) if response else []:
                parsed = self._scope_subtask_from_raw(issue)
                if not parsed:
                    continue
                parent = (issue.get("fields") or {}).get("parent") or {}
                parent_key = str(parent.get("key") or "").strip()
                if not parent_key:
                    continue
                grouped.setdefault(parent_key, []).append(parsed)
        return grouped

    def _merge_gitlab_and_comment_workload_items(
        self,
        api_items: list[dict[str, str]],
        comment_items: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        merged = list(api_items)
        seen = {(str(item.get("role") or ""), str(item.get("subtask_key") or "")) for item in api_items}
        for item in comment_items:
            key = (str(item.get("role") or ""), str(item.get("subtask_key") or ""))
            if key in seen:
                continue
            merged.append(item)
            seen.add(key)
        return merged

    async def _enrich_scope_issues_gitlab(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not issues or not gitlab_configured():
            return issues

        keys: list[str] = []
        for issue in issues:
            issue_key = str(issue.get("key") or "").strip()
            if issue_key:
                keys.append(issue_key)
            for subtask in issue.get("_subtasks") or []:
                if isinstance(subtask, dict):
                    subtask_key = str(subtask.get("key") or "").strip()
                    if subtask_key:
                        keys.append(subtask_key)

        client = GitLabHttpClient()
        try:
            evidence_by_key = await client.fetch_evidence_by_keys(keys)
        finally:
            await client.close()

        if not evidence_by_key:
            return issues

        for issue in issues:
            issue_key = str(issue.get("key") or "").strip().upper()
            issue["_gitlab_raw"] = evidence_by_key.get(issue_key) or {"merge_requests": [], "commits": []}
            for subtask in issue.get("_subtasks") or []:
                if not isinstance(subtask, dict):
                    continue
                subtask_key = str(subtask.get("key") or "").strip().upper()
                subtask["_gitlab_raw"] = evidence_by_key.get(subtask_key) or {"merge_requests": [], "commits": []}
        return issues

    def _apply_scope_status_fallback(self, issue: dict[str, Any]) -> dict[str, Any]:
        """Use Jira statuscategorychangedate when changelog enrichment is skipped."""
        enriched = {**issue}
        if not enriched.get("status_entered_at"):
            changed_at = str(enriched.get("status_changed_at") or "").strip()
            if changed_at:
                enriched["status_entered_at"] = changed_at
        return self._finalize_scope_issue_roles(enriched)

    def _finalize_scope_issue_roles(
        self,
        issue: dict[str, Any],
        *,
        histories: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        enriched = {**issue}
        status_field = enriched.get("status")
        if isinstance(status_field, dict):
            status_name = str(status_field.get("name") or "")
        else:
            status_name = str(status_field or "")
        assignee = str(enriched.get("assignee") or "")

        if not enriched.get("developer"):
            developer, developer_source = infer_developer_from_changelog(
                histories or [],
                current_status=status_name,
                current_assignee=assignee,
            )
            enriched["developer"] = developer
            enriched["developer_source"] = developer_source

        qa_name = ""
        qa_source = ""
        if histories:
            qa_name, qa_source = infer_qa_from_changelog(
                histories,
                current_status=status_name,
                current_assignee=assignee,
            )
            if qa_name and qa_source == "fallback":
                if qa_assignee_from_current_status_allowed(status_name):
                    qa_source = "current"
                else:
                    qa_name = ""
                    qa_source = ""
        if not qa_name and assignee and qa_assignee_from_current_status_allowed(status_name):
            qa_name = assignee
            qa_source = "current"

        subtasks = enriched.get("_subtasks") if isinstance(enriched.get("_subtasks"), list) else []
        parent_gitlab_items = build_gitlab_api_workload_items(
            enriched.get("_gitlab_raw") if isinstance(enriched.get("_gitlab_raw"), dict) else {},
            jira_key=str(enriched.get("key") or ""),
        )
        subtask_gitlab_items: list[dict[str, str]] = []
        for subtask in subtasks:
            if not isinstance(subtask, dict):
                continue
            subtask_gitlab_items.extend(
                build_gitlab_api_workload_items(
                    subtask.get("_gitlab_raw") if isinstance(subtask.get("_gitlab_raw"), dict) else {},
                    jira_key=str(enriched.get("key") or ""),
                    subtask_key=str(subtask.get("key") or ""),
                    subtask_summary=str(subtask.get("summary") or ""),
                )
            )

        comment_workload_items = build_subtask_workload_items(subtasks)
        api_workload_items = parent_gitlab_items + subtask_gitlab_items
        workload_items_input = self._merge_gitlab_and_comment_workload_items(api_workload_items, comment_workload_items)
        from_gitlab_api = build_gitlab_api_contributors(parent_gitlab_items)

        merged, workload_items = merge_role_contributors(
            from_comments=enriched.get("role_contributors_from_comments")
            if isinstance(enriched.get("role_contributors_from_comments"), dict)
            else {},
            from_jira_fields=enriched.get("role_contributors_from_jira_fields")
            if isinstance(enriched.get("role_contributors_from_jira_fields"), dict)
            else {},
            from_gitlab_api=from_gitlab_api,
            subtask_workload_items=workload_items_input,
            labels=enriched.get("labels") if isinstance(enriched.get("labels"), list) else [],
            developer=str(enriched.get("developer") or ""),
            developer_source=str(enriched.get("developer_source") or ""),
            issue_comments=enriched.get("_comments") if isinstance(enriched.get("_comments"), list) else [],
            qa_from_changelog=qa_name,
            qa_source=qa_source,
        )
        enriched["role_contributors"] = merged
        enriched["role_contributors_list"] = role_contributors_list(merged)
        enriched["role_workload_items"] = workload_items

        comment_roles = set((enriched.get("role_contributors_from_comments") or {}).keys())
        role_evidence: list[dict[str, str]] = list(api_workload_items)
        for role in ("front", "back", "qa"):
            payload = merged.get(role) if isinstance(merged.get(role), dict) else {}
            reason = unresolved_reason_for_role(
                role=role,
                labels=enriched.get("labels") if isinstance(enriched.get("labels"), list) else [],
                gitlab_items=api_workload_items,
                comment_gitlab_roles=comment_roles,
                has_trusted_name=bool(str(payload.get("name") or "").strip()),
            )
            if reason:
                role_evidence.append(
                    {
                        "role": role,
                        "jira_key": str(enriched.get("key") or ""),
                        "unresolved_reason": reason,
                        "confidence": "unresolved",
                    }
                )
        enriched["role_evidence"] = role_evidence

        from_jira_fields = enriched.get("role_contributors_from_jira_fields")
        if not isinstance(from_jira_fields, dict):
            from_jira_fields = {}
        enriched["jira_role_assignees"] = {
            role: str((from_jira_fields.get(role) or {}).get("name") or "").strip()
            for role in ("front", "back", "qa")
        }

        enriched.pop("_subtasks", None)
        enriched.pop("role_contributors_from_jira_fields", None)
        enriched.pop("_comments", None)
        enriched.pop("_gitlab_raw", None)
        enriched.pop("role_contributors_from_comments", None)
        return enriched

    async def parse_jira_scope_issues(
        self,
        text: str,
        max_results: int = 500,
        *,
        milestone_status_targets: Optional[list[str]] = None,
        enrich_changelog: bool = True,
    ) -> Optional[List[Dict[str, Any]]]:
        """Return scope-dashboard issues by JQL with status/type/created metadata."""
        if not text or not text.strip():
            return None
        jql = text.strip()
        try:
            response = await self.search_scope_issues(jql, max_results=max_results)
            if not response or "issues" not in response:
                return None
            issues = [
                parsed
                for issue in response.get("issues", [])
                if (parsed := self._scope_issue_from_raw(issue))
            ]
            if issues:
                epic_labels = await self._fetch_issue_labels_by_key(
                    [
                        str(issue.get("linked_epic_key") or issue.get("epic_key") or issue.get("parent_key") or "")
                        for issue in issues
                    ]
                )
                for issue in issues:
                    epic_key = str(issue.get("linked_epic_key") or issue.get("epic_key") or issue.get("parent_key") or "")
                    issue["epic_labels"] = epic_labels.get(epic_key, [])
                subtasks_by_parent = await self._fetch_subtasks_by_parent([str(issue.get("key") or "") for issue in issues])
                for issue in issues:
                    issue["_subtasks"] = subtasks_by_parent.get(str(issue.get("key") or ""), [])
                issues = await self._enrich_scope_issues_gitlab(issues)
                if enrich_changelog:
                    issues = await self.enrich_scope_issues_milestones(
                        issues,
                        milestone_status_targets=milestone_status_targets,
                    )
                else:
                    issues = [self._apply_scope_status_fallback(issue) for issue in issues]
            return issues
        except Exception as error:
            logger.warning("Error processing scope Jira request: %s", error)
            return None

    async def _fetch_issue_changelog_histories(self, issue_key: str) -> list[dict[str, Any]]:
        histories: list[dict[str, Any]] = []
        start_at = 0
        page_size = 100
        while True:
            data = await self._make_request(
                "GET",
                f"issue/{issue_key}/changelog?startAt={start_at}&maxResults={page_size}",
                api_versions=["3"],
            )
            if not data:
                break
            page = [item for item in (data.get("values") or []) if isinstance(item, dict)]
            histories.extend(page)
            total = int(data.get("total") or len(histories))
            start_at += len(page)
            if start_at >= total or not page:
                break
        return histories

    async def _enrich_scope_issue_milestones(
        self,
        issue: dict[str, Any],
        *,
        milestone_status_targets: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        issue_key = str(issue.get("key") or "")
        if not issue_key:
            return issue

        status_field = issue.get("status")
        if isinstance(status_field, dict):
            status_name = str(status_field.get("name") or "")
        else:
            status_name = str(status_field or "")

        epic_key = str(issue.get("epic_key") or "")
        histories = await self._fetch_issue_changelog_histories(issue_key)

        enriched = {**issue}
        targets = [str(name) for name in (milestone_status_targets or []) if str(name).strip()]
        entered_at: Optional[str] = None
        if histories:
            if targets:
                entered_at = status_entered_at_for_targets(histories, targets, mode="last")
                if not entered_at and status_name:
                    entered_at = status_entered_at_for_targets(histories, [status_name], mode="last")
            elif status_name:
                entered_at = status_entered_at(histories, status_name)
            if not entered_at and status_name:
                entered_at = status_entered_at(histories, status_name)
        if not entered_at:
            changed_at = str(issue.get("status_changed_at") or "").strip()
            if changed_at and status_name:
                in_targets = status_matches_any_target(status_name, targets) if targets else True
                if in_targets:
                    entered_at = changed_at
        if entered_at:
            enriched["status_entered_at"] = entered_at
        if histories:
            linked_at = epic_linked_at(histories, epic_key) if epic_key else None
            if linked_at:
                enriched["epic_linked_at"] = linked_at
        return self._finalize_scope_issue_roles(enriched, histories=histories if histories else None)

    async def enrich_scope_issues_milestones(
        self,
        issues: list[dict[str, Any]],
        *,
        milestone_status_targets: Optional[list[str]] = None,
        batch_size: Optional[int] = None,
        max_issues: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if not issues:
            return issues

        chunk_size = max(1, batch_size or int(os.getenv("SCOPE_JIRA_CHANGELOG_BATCH_SIZE", "6")))
        enrich_limit = max(0, max_issues or int(os.getenv("SCOPE_JIRA_CHANGELOG_MAX_ISSUES", "40")))
        to_enrich = issues[:enrich_limit] if enrich_limit else []
        tail = issues[enrich_limit:] if enrich_limit else []

        enriched: list[dict[str, Any]] = []
        for start in range(0, len(to_enrich), chunk_size):
            batch = to_enrich[start : start + chunk_size]
            batch_results = await asyncio.gather(
                *(
                    self._enrich_scope_issue_milestones(
                        issue,
                        milestone_status_targets=milestone_status_targets,
                    )
                    for issue in batch
                ),
                return_exceptions=True,
            )
            for issue, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Failed to enrich scope milestones for %s: %s",
                        issue.get("key"),
                        result,
                    )
                    enriched.append(self._apply_scope_status_fallback(issue))
                else:
                    enriched.append(result)
        if tail:
            enriched.extend(self._apply_scope_status_fallback(issue) for issue in tail)
        return enriched

    def get_issue_url(self, issue_key: str) -> str:
        """Get URL for issue."""
        return f"{self.base_url}/browse/{issue_key}"

    async def update_story_points(self, issue_key: str, story_points: int) -> bool:
        """Update story points for issue."""
        payload = {"fields": {self.story_points_field: story_points}}
        result = await self._make_request("PUT", f"issue/{issue_key}", payload, api_versions=["3", "2"])
        return result is not None

    async def update_story_points_fields(self, issue_key: str, fields: Mapping[str, int]) -> Dict[str, bool]:
        """Update arbitrary numeric Jira fields one by one for partial success."""
        results: Dict[str, bool] = {}
        for field_id, value in fields.items():
            if not field_id:
                continue
            payload = {"fields": {field_id: value}}
            result = await self._make_request("PUT", f"issue/{issue_key}", payload, api_versions=["3", "2"])
            results[field_id] = result is not None
        return results

    async def update_due_date(self, issue_key: str, due_date: str) -> bool:
        """Update Jira due date field with an ISO calendar date."""
        field_id = _due_date_field_id()
        payload = {"fields": {field_id: due_date}}
        result = await self._make_request("PUT", f"issue/{issue_key}", payload, api_versions=["3", "2"])
        return result is not None

    async def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        """Fetch Jira fix version metadata (dates, release status, name)."""
        cleaned = str(version_id or "").strip()
        if not cleaned:
            return None
        result = await self._make_request("GET", f"version/{quote(cleaned, safe='')}", api_versions=["3", "2"])
        return result if isinstance(result, dict) else None

    async def get_project_versions(self, project_key: str) -> List[Dict[str, Any]]:
        """List fix versions configured on a Jira project."""
        cleaned = str(project_key or "").strip().upper()
        if not cleaned:
            return []
        result = await self._make_request(
            "GET",
            f"project/{quote(cleaned, safe='')}/versions",
            api_versions=["3", "2"],
        )
        return result if isinstance(result, list) else []

    async def resolve_version(
        self,
        *,
        project_key: str = "",
        version_id: str = "",
        version_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Resolve version metadata by numeric id or project + version name."""
        cleaned_id = str(version_id or "").strip()
        if cleaned_id:
            resolved = await self.get_version(cleaned_id)
            if resolved:
                return resolved

        cleaned_name = str(version_name or "").strip()
        cleaned_project = str(project_key or "").strip().upper()
        if cleaned_project and cleaned_name:
            versions = await self.get_project_versions(cleaned_project)
            target = cleaned_name.casefold()
            for item in versions:
                if not isinstance(item, dict):
                    continue
                if str(item.get("name") or "").strip().casefold() == target:
                    return item
        return None

    async def add_issue_comment(self, issue_key: str, text: str) -> Optional[Dict[str, Any]]:
        """Append a plain-text comment to a Jira issue."""
        cleaned = truncate_text(text, 4000)
        if not issue_key or not cleaned:
            return None
        payload = {"body": self._comment_text_to_adf(cleaned)}
        return await self._make_request("POST", f"issue/{issue_key}/comment", payload, api_versions=["3"])

    async def add_issue_comment_adf(self, issue_key: str, body: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Append an ADF comment to a Jira issue."""
        if not issue_key or not isinstance(body, Mapping) or body.get("type") != "doc":
            return None
        payload = {"body": body}
        return await self._make_request("POST", f"issue/{issue_key}/comment", payload, api_versions=["3"])

    async def update_issue_comment_adf(
        self,
        issue_key: str,
        comment_id: str,
        body: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Replace an existing Jira comment body with ADF."""
        if not issue_key or not comment_id or not isinstance(body, Mapping) or body.get("type") != "doc":
            return None
        payload = {"body": body}
        return await self._make_request(
            "PUT",
            f"issue/{issue_key}/comment/{comment_id}",
            payload,
            api_versions=["3"],
        )

    def _comment_text_to_adf(self, text: str) -> Dict[str, Any]:
        paragraphs = []
        for line in text.splitlines() or [text]:
            paragraphs.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}] if line else [],
                }
            )
        return {"type": "doc", "version": 1, "content": paragraphs}

    async def parse_jira_request(self, text: str, max_results: int = 500) -> Optional[List[Dict[str, Any]]]:
        """Return list of tasks by JQL."""
        if not text:
            return None

        jql = text.strip()
        if not jql:
            return None

        try:
            response = await self.search_issues(jql, max_results=max_results)
            if not response or "issues" not in response:
                logger.info("No Jira issues from search")
                # Fallback: if search didn't work, try to get tasks by keys from JQL
                fallback_issues: List[Dict[str, Any]] = []
                for key in self._key_pattern.findall(text):
                    details = await self._fetch_issue_by_key(key)
                    if details:
                        fallback_issues.append(details)
                return fallback_issues or None

            issues: List[Dict[str, Any]] = []
            for issue in response.get("issues", []):
                issue_key = issue.get("key")
                if not issue_key:
                    continue

                fields = issue.get("fields", {})
                summary = fields.get("summary", issue_key)
                raw_story_points = fields.get(self.story_points_field)
                story_points = raw_story_points if isinstance(raw_story_points, (int, float)) else 0

                issues.append(
                    {
                        "key": issue_key,
                        "summary": summary,
                        "url": self.get_issue_url(issue_key),
                        "story_points": story_points,
                    }
                )

            return issues or None
        except Exception as error:
            logger.warning("Error processing Jira request: %s", error)
            # Fallback: if search didn't work, try to get tasks by keys from JQL
            fallback_issues: List[Dict[str, Any]] = []
            for key in self._key_pattern.findall(text):
                details = await self._fetch_issue_by_key(key)
                if details:
                    fallback_issues.append(details)
            return fallback_issues or None

    async def _fetch_issue_by_key(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Fetch issue details by key."""
        issue = await self._make_request("GET", f"issue/{issue_key}", api_versions=["3", "2"])
        if not issue:
            return None

        fields = issue.get("fields", {})
        summary = fields.get("summary", issue_key)
        raw_story_points = fields.get(self.story_points_field)
        story_points = raw_story_points if isinstance(raw_story_points, (int, float)) else 0

        return {
            "key": issue_key,
            "summary": summary,
            "url": self.get_issue_url(issue_key),
            "story_points": story_points,
        }

    def _issue_context_from_fields(
        self,
        issue_key: str,
        fields: Dict[str, Any],
        rendered_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        summary = str(fields.get("summary") or issue_key)
        raw_description = fields.get("description")
        if isinstance(raw_description, dict):
            description = adf_to_plain_text(raw_description)
            # Keep the raw ADF tree alongside the plain-text projection.
            # The voter UI renders this to preserve original Jira
            # formatting (lists, headings, code, links). The plain text
            # is still what the AI prompt consumes.
            description_adf: Optional[Dict[str, Any]] = raw_description
        else:
            description = str(raw_description or "").strip()
            description_adf = None
            if raw_description is not None and not isinstance(raw_description, str):
                logger.warning(
                    "jira description unexpected type key=%s type=%s",
                    issue_key,
                    type(raw_description).__name__,
                )

        # Jira Cloud can return description as a flat string when the REST
        # client fell back to API v2, OR when ADF exists but the voter UI
        # needs the same look as Jira itself. ``renderedFields.description``
        # is server-rendered HTML — closest match to what you see in Jira.
        description_html: Optional[str] = None
        if rendered_fields and isinstance(rendered_fields, dict):
            raw_html = rendered_fields.get("description")
            if isinstance(raw_html, str) and raw_html.strip():
                description_html = sanitize_jira_html(raw_html) or None

        max_chars = max(500, int(os.getenv("ANTHROPIC_MAX_CONTEXT_CHARS", str(DEFAULT_MAX_CONTEXT_CHARS))))
        description = truncate_text(description, max_chars)

        issue_type_field = fields.get("issuetype") or {}
        issue_type = issue_type_field.get("name") if isinstance(issue_type_field, dict) else None

        labels = [str(item) for item in (fields.get("labels") or []) if item]
        components = [
            str(component.get("name"))
            for component in (fields.get("components") or [])
            if isinstance(component, dict) and component.get("name")
        ]

        raw_story_points = fields.get(self.story_points_field)
        story_points = raw_story_points if isinstance(raw_story_points, (int, float)) else None

        return {
            "key": issue_key,
            "summary": summary,
            "url": self.get_issue_url(issue_key),
            "description": description,
            "description_adf": description_adf,
            "description_html": description_html,
            "issue_type": issue_type,
            "labels": labels[:20],
            "components": components[:20],
            "story_points": story_points,
        }

    def _confluence_configured(self) -> bool:
        return bool(
            self.confluence_base_url
            and self.confluence_username
            and self.confluence_api_token
            and self._confluence_max_pages > 0
        )

    async def _fetch_confluence_page_context(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a Confluence page by id and return sanitized display context."""
        if not self._confluence_configured() or not page_id.isdigit():
            return None

        session = await self._get_session()
        auth = aiohttp.BasicAuth(self.confluence_username, self.confluence_api_token)
        url = f"{self.confluence_base_url}/rest/api/content/{page_id}?expand=body.view,_links"
        try:
            async with session.get(
                url,
                auth=auth,
                headers={"Accept": "application/json"},
            ) as response:
                if response.status in {401, 403, 404}:
                    logger.warning("Confluence page fetch non-200 page_id=%s status=%s", page_id, response.status)
                    return None
                response.raise_for_status()
                data = await response.json()
        except Exception as error:
            logger.warning("Confluence page fetch failed page_id=%s err=%r", page_id, error)
            return None

        if not isinstance(data, dict):
            return None
        title = str(data.get("title") or f"Confluence page {page_id}")
        raw_html = ((data.get("body") or {}).get("view") or {}).get("value")
        safe_html = sanitize_jira_html(raw_html or "")
        text = truncate_text(
            html_to_plain_text(safe_html),
            max(500, int(os.getenv("ANTHROPIC_MAX_CONTEXT_CHARS", str(DEFAULT_MAX_CONTEXT_CHARS)))),
        )
        links = data.get("_links") if isinstance(data.get("_links"), dict) else {}
        webui = links.get("webui") if isinstance(links.get("webui"), str) else ""
        page_url = urljoin(f"{self.confluence_base_url}/", webui.lstrip("/")) if webui else f"{self.confluence_base_url}/pages/{page_id}"
        if not safe_html and not text:
            return None
        return {
            "type": "confluence",
            "id": page_id,
            "title": title,
            "url": page_url,
            "description": text,
            "description_html": safe_html,
        }

    async def _augment_context_with_confluence(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Append linked Confluence page content to Jira issue context."""
        page_ids = extract_confluence_page_ids(
            confluence_base_url=self.confluence_base_url,
            description=context.get("description") or "",
            description_html=context.get("description_html") or "",
            description_adf=context.get("description_adf"),
        )
        if not page_ids:
            context["description_sources"] = [
                {"type": "jira", "url": context.get("url"), "title": context.get("key")}
            ]
            return context

        pages: list[Dict[str, Any]] = []
        for page_id in page_ids[: self._confluence_max_pages]:
            page = await self._fetch_confluence_page_context(page_id)
            if page:
                pages.append(page)
        if not pages:
            context["description_sources"] = [
                {"type": "jira", "url": context.get("url"), "title": context.get("key")}
            ]
            return context

        jira_text = str(context.get("description") or "").strip()
        confluence_text = "\n\n".join(
            f"Confluence: {page['title']}\n{page['description']}"
            for page in pages
            if page.get("description")
        )
        context["description"] = truncate_text(
            "\n\n".join(part for part in [jira_text, confluence_text] if part),
            max(500, int(os.getenv("ANTHROPIC_MAX_CONTEXT_CHARS", str(DEFAULT_MAX_CONTEXT_CHARS)))),
        )

        jira_html = str(context.get("description_html") or "").strip()
        if not jira_html and jira_text:
            jira_html = "".join(f"<p>{html.escape(line)}</p>" for line in jira_text.splitlines() if line.strip())
        confluence_html = "".join(
            f'<hr /><h3>Confluence: <a href="{html.escape(str(page["url"]))}" target="_blank" rel="noreferrer">{html.escape(str(page["title"]))}</a></h3>{page["description_html"]}'
            for page in pages
            if page.get("description_html")
        )
        context["description_html"] = sanitize_jira_html("".join([jira_html, confluence_html]))
        context["description_sources"] = [
            {"type": "jira", "url": context.get("url"), "title": context.get("key")},
            *[
                {"type": "confluence", "url": page.get("url"), "title": page.get("title")}
                for page in pages
            ],
        ]
        return context

    async def fetch_issue_context(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Fetch issue fields used for LLM planning hints."""
        fields_list = quote(
            f"summary,description,labels,components,issuetype,{self.story_points_field}",
            safe=",",
        )
        # API v3 only + renderedFields: v2 returns description as a single
        # plain/wiki string (no ADF, no newlines) which is exactly the
        # "wall of text" voters were seeing. renderedFields is what Jira
        # UI uses to paint headings, lists, and tables.
        issue = await self._make_request(
            "GET",
            f"issue/{issue_key}?expand=renderedFields&fields={fields_list}",
            api_versions=["3"],
        )
        if not issue:
            return None
        fields = issue.get("fields") or {}
        rendered = issue.get("renderedFields")
        context = self._issue_context_from_fields(
            issue_key,
            fields,
            rendered if isinstance(rendered, dict) else None,
        )
        return await self._augment_context_with_confluence(context)
