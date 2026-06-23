"""Jira client interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional


class JiraClient(ABC):
    """Interface for Jira API client."""

    @abstractmethod
    async def search_issues(self, jql: str, max_results: int = 100) -> Optional[Dict[str, Any]]:
        """Execute search for issues using arbitrary JQL."""
        pass

    @abstractmethod
    def get_issue_url(self, issue_key: str) -> str:
        """Get URL for issue."""
        pass

    @abstractmethod
    async def update_story_points(self, issue_key: str, story_points: int) -> bool:
        """Update story points for issue."""
        pass

    async def update_story_points_fields(self, issue_key: str, fields: Mapping[str, int]) -> Dict[str, bool]:
        """Update one or more Jira story-point fields for issue."""
        results: Dict[str, bool] = {}
        for field_id, value in fields.items():
            if field_id:
                results[field_id] = await self.update_story_points(issue_key, value)
        return results

    async def update_story_points_tracks(
        self, issue_key: str, tracks: Mapping[str, int]
    ) -> tuple[Dict[str, bool], List[str]]:
        """Update SP by semantic track keys via jira-service. Returns (results, skipped_tracks)."""
        raise NotImplementedError("update_story_points_tracks requires JiraServiceHttpClient")

    @abstractmethod
    async def parse_jira_request(self, text: str, max_results: int = 500) -> Optional[List[Dict[str, Any]]]:
        """Return list of tasks by JQL or issue keys."""
        pass
