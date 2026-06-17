"""Use case for adding tasks from Jira."""

from typing import List, Optional, Set, Tuple

from app.domain.session import Session
from app.domain.task import Task
from app.ports.jira_client import JiraClient
from app.ports.session_repository import SessionRepository


class AddTasksFromJiraUseCase:
    """Use case for adding tasks from Jira to session."""

    def __init__(self, jira_client: JiraClient, session_repo: SessionRepository):
        self.jira_client = jira_client
        self.session_repo = session_repo

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
        jql: str,
        max_results: int = 500,
    ) -> Tuple[List[Task], List[str]]:
        """Add tasks from Jira query to session."""
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        jira_issues = await self.jira_client.parse_jira_request(jql, max_results=max_results)
        if not jira_issues:
            return [], []

        existing_keys: Set[str] = set()
        for task in session.tasks_queue:
            if task.jira_key:
                existing_keys.add(task.jira_key)
        for task in session.last_batch:
            if task.jira_key:
                existing_keys.add(task.jira_key)
        for task in session.history:
            if task.jira_key:
                existing_keys.add(task.jira_key)

        added: List[Task] = []
        skipped: List[str] = []

        for issue in jira_issues:
            jira_key = issue.get("key")
            if not jira_key:
                continue
            if jira_key in existing_keys:
                skipped.append(jira_key)
                continue

            task = Task(
                jira_key=jira_key,
                summary=issue.get("summary") or jira_key,
                url=issue.get("url"),
                story_points=issue.get("story_points"),
                jql=jql,
                source="jira",
            )
            session.tasks_queue.append(task)
            existing_keys.add(jira_key)
            added.append(task)

        if added:
            session.batch_completed = False
            session.bump_tasks_version()
        await self.session_repo.save_session(session)
        return added, skipped
