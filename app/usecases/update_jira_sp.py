"""Use case for updating Jira story points."""

import asyncio
import os
from typing import Dict, List, Optional, Tuple

from app.domain.estimation import get_mode_config, is_split_mode
from app.domain.session import Session
from app.domain.task import Task
from app.ports.jira_client import JiraClient
from app.ports.session_repository import SessionRepository
from app.usecases.show_results import VotingPolicy


class UpdateJiraStoryPointsUseCase:
    """Use case for updating story points in Jira."""

    def __init__(self, jira_client: JiraClient, session_repo: SessionRepository):
        self.jira_client = jira_client
        self.session_repo = session_repo
        self.policy = VotingPolicy()

    async def execute(
        self,
        chat_id: int,
        topic_id: Optional[int],
        skip_errors: bool = False,
    ) -> Tuple[int, List[str], List[str]]:
        """Update story points in Jira for last batch tasks.
        
        Returns:
            Tuple of (updated_count, failed_keys, skipped_reasons)
        """
        session = await self.session_repo.get_session(chat_id, topic_id)
        
        if not session.last_batch:
            return 0, [], []
        
        updated = 0
        failed: List[str] = []
        skipped: List[str] = []
        pending_updates: list[tuple[Task, str, int]] = []
        pending_track_updates: list[tuple[Task, str, Dict[str, int], Dict[str, str]]] = []
        
        for task in session.last_batch:
            if not task.jira_key:
                label = task.summary or task.task_id or "Задача"
                skipped.append(f"{label}: нет ключа Jira")
                continue

            if is_split_mode(session.estimation_mode) and task.story_points_by_track:
                tracks: Dict[str, int] = {}
                track_labels: Dict[str, str] = {}
                for track_key, value in task.story_points_by_track.items():
                    track_label = self._track_label(session.estimation_mode, track_key)
                    tracks[track_key] = int(value)
                    track_labels[track_key] = track_label
                if tracks:
                    pending_track_updates.append((task, task.jira_key, tracks, track_labels))
                elif not skip_errors:
                    failed.append(task.jira_key)
                continue

            story_points = self._story_points_for_jira(task)
            if story_points is None:
                if skip_errors:
                    label = task.jira_key
                    if not task.votes:
                        skipped.append(f"{label}: нет голосов и нет финальной оценки")
                    else:
                        skipped.append(f"{label}: нет финальной оценки SP")
                    continue
                failed.append(task.jira_key)
                continue

            if story_points == 0:
                if skip_errors:
                    skipped.append(f"{task.jira_key}: нет валидных голосов")
                    continue
                failed.append(task.jira_key)
                continue
            
            pending_updates.append((task, task.jira_key, story_points))

        if skip_errors:
            concurrency = max(1, int(os.getenv("JIRA_UPDATE_CONCURRENCY", "5")))
            semaphore = asyncio.Semaphore(concurrency)

            async def update_one(jira_key: str, story_points: int):
                async with semaphore:
                    try:
                        return jira_key, await self.jira_client.update_story_points(jira_key, story_points)
                    except Exception as exc:  # noqa: BLE001 — skip_errors must not abort the batch
                        return jira_key, exc

            results = await asyncio.gather(
                *(update_one(jira_key, story_points) for _, jira_key, story_points in pending_updates),
                return_exceptions=False,
            )
            result_by_key = dict(results)
            for task, jira_key, story_points in pending_updates:
                outcome = result_by_key.get(jira_key)
                if outcome is True:
                    task.story_points = story_points
                    updated += 1
                elif isinstance(outcome, Exception):
                    failed.append(jira_key)
                    skipped.append(f"{jira_key}: {outcome}")
                else:
                    failed.append(jira_key)

            async def update_tracks(
                jira_key: str,
                tracks: Dict[str, int],
            ):
                async with semaphore:
                    try:
                        results, skipped_tracks = await self.jira_client.update_story_points_tracks(
                            jira_key, tracks
                        )
                        return jira_key, results, skipped_tracks
                    except Exception as exc:  # noqa: BLE001
                        return jira_key, exc, []

            track_results = await asyncio.gather(
                *(update_tracks(jira_key, tracks) for _, jira_key, tracks, _ in pending_track_updates),
                return_exceptions=False,
            )
            track_result_by_key = {
                jira_key: (results, skipped_tracks) for jira_key, results, skipped_tracks in track_results
            }
            for task, jira_key, tracks, track_labels in pending_track_updates:
                results, skipped_tracks = track_result_by_key.get(jira_key, ({}, []))
                if isinstance(results, Exception):
                    failed.append(jira_key)
                    skipped.append(f"{jira_key}: {results}")
                    continue
                for track_key in skipped_tracks:
                    label = track_labels.get(track_key, track_key)
                    skipped.append(
                        f"{jira_key} {label}: поле Jira не настроено в jira-service (трек {track_key})"
                    )
                for track_key, ok in results.items():
                    if track_key in skipped_tracks:
                        continue
                    label = track_labels.get(track_key, track_key)
                    if ok:
                        updated += 1
                    else:
                        failed.append(f"{jira_key} {label}: запись в Jira отклонена")
        else:
            for task, jira_key, story_points in pending_updates:
                if await self.jira_client.update_story_points(jira_key, story_points):
                    task.story_points = story_points
                    updated += 1
                else:
                    failed.append(jira_key)
                    break
            for task, jira_key, tracks, track_labels in pending_track_updates:
                results, skipped_tracks = await self.jira_client.update_story_points_tracks(jira_key, tracks)
                for track_key in skipped_tracks:
                    label = track_labels.get(track_key, track_key)
                    skipped.append(
                        f"{jira_key} {label}: поле Jira не настроено в jira-service (трек {track_key})"
                    )
                for track_key, ok in results.items():
                    if track_key in skipped_tracks:
                        continue
                    label = track_labels.get(track_key, track_key)
                    if ok:
                        updated += 1
                    else:
                        failed.append(f"{jira_key} {label}: запись в Jira отклонена")
                        break

        if updated:
            await self.session_repo.save_session(session)

        return updated, failed, skipped

    def _story_points_for_jira(self, task: Task) -> Optional[int]:
        """Prefer manager final SP; fall back to max numeric vote when unset."""
        if task.story_points is not None and task.story_points > 0:
            return int(task.story_points)
        if not task.votes:
            return None
        max_vote = self.policy.get_max_vote(task.votes)
        return max_vote if max_vote > 0 else None

    def _track_label(self, mode: str, track_key: str) -> str:
        config = get_mode_config(mode)
        for track in config.tracks:
            if track.key == track_key:
                return track.label
        return track_key
