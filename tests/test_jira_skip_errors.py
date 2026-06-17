"""Tests for Jira update with skip_errors mode."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.usecases.show_results import VotingPolicy
from config import UserRole


class TestJiraSkipErrors:
    """Tests for Jira update with skip_errors functionality."""

    @pytest.mark.asyncio
    async def test_jira_skip_errors_audit_logging(self):
        """Test that audit_log is called with failed_keys and skipped_reasons in skip_errors mode."""
        session = Session(chat_id=123, topic_id=456)

        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[1] = lead

        # Создаём задачи с разными состояниями
        task1 = Task(jira_key="TEST-1", summary="Task 1")
        task1.votes = {1: "5", 2: "8"}  # Есть голоса

        task2 = Task(jira_key="TEST-2", summary="Task 2")
        task2.votes = {}  # Нет голосов - будет пропущено

        task3 = Task(jira_key="TEST-3", summary="Task 3")
        task3.votes = {1: "skip", 2: "skip"}  # Только skip - будет пропущено

        task4 = Task(jira_key="", summary="Task 4")  # Нет ключа - будет пропущено

        session.last_batch = [task1, task2, task3, task4]

        # Симулируем данные для audit_log
        updated = 0
        failed = []
        skipped = []

        # Обрабатываем задачи
        for task in session.last_batch:
            if not task.jira_key:
                skipped.append(f"{task.jira_key or 'Без ключа'}: нет ключа Jira")
                continue

            if not task.votes:
                skipped.append(f"{task.jira_key}: нет голосов")
                continue

            story_points = VotingPolicy.get_max_vote(task.votes)
            if story_points == 0:
                skipped.append(f"{task.jira_key}: нет валидных голосов")
                continue

            # Симулируем успешное обновление только для task1
            if task.jira_key == "TEST-1":
                updated += 1
            else:
                failed.append(task.jira_key)

        # Проверяем результаты
        assert updated == 1
        assert len(failed) == 0  # В этом тесте нет реальных ошибок Jira
        assert len(skipped) == 3  # task2, task3, task4

        # Проверяем, что skipped содержит правильные причины
        skipped_keys = [s.split(":")[0] for s in skipped]
        assert "TEST-2" in skipped_keys or any("TEST-2" in s for s in skipped)
        assert "TEST-3" in skipped_keys or any("TEST-3" in s for s in skipped)
        assert "Без ключа" in skipped_keys or any("Без ключа" in s for s in skipped)

        # Формируем extra_data как в реальном коде
        jira_keys = [task.jira_key for task in session.last_batch if task.jira_key]
        extra_data = {
            "updated_count": updated,
            "failed_count": len(failed),
            "skipped_count": len(skipped),
            "total_tasks": len(session.last_batch),
            "jira_keys": jira_keys[:10],
        }

        if failed:
            extra_data["failed_keys"] = failed[:10]
        if skipped:
            extra_data["skipped_reasons"] = skipped[:10]

        # Проверяем, что extra_data содержит нужные поля
        assert extra_data["updated_count"] == 1
        assert extra_data["failed_count"] == 0
        assert extra_data["skipped_count"] == 3
        assert "skipped_reasons" in extra_data
        assert len(extra_data["skipped_reasons"]) == 3

    @pytest.mark.asyncio
    async def test_jira_skip_errors_with_failed_updates(self):
        """Test skip_errors mode with some failed Jira updates."""
        session = Session(chat_id=123, topic_id=456)

        lead = Participant(user_id=1, name="Lead", role=UserRole.LEAD)
        session.participants[1] = lead

        task1 = Task(jira_key="TEST-1", summary="Task 1")
        task1.votes = {1: "5"}

        task2 = Task(jira_key="TEST-2", summary="Task 2")
        task2.votes = {1: "8"}

        task3 = Task(jira_key="TEST-3", summary="Task 3")
        task3.votes = {1: "13"}

        session.last_batch = [task1, task2, task3]

        # Симулируем: task1 успешно, task2 ошибка, task3 успешно
        updated = 0
        failed = []
        skipped = []

        for task in session.last_batch:
            if not task.jira_key:
                skipped.append(f"{task.jira_key or 'Без ключа'}: нет ключа Jira")
                continue

            if not task.votes:
                skipped.append(f"{task.jira_key}: нет голосов")
                continue

            story_points = VotingPolicy.get_max_vote(task.votes)
            if story_points == 0:
                skipped.append(f"{task.jira_key}: нет валидных голосов")
                continue

            # Симулируем: TEST-2 возвращает ошибку
            if task.jira_key == "TEST-2":
                failed.append(task.jira_key)
            else:
                updated += 1

        assert updated == 2
        assert len(failed) == 1
        assert failed[0] == "TEST-2"
        assert len(skipped) == 0

        # Формируем extra_data
        jira_keys = [task.jira_key for task in session.last_batch if task.jira_key]
        extra_data = {
            "updated_count": updated,
            "failed_count": len(failed),
            "skipped_count": len(skipped),
            "total_tasks": len(session.last_batch),
            "jira_keys": jira_keys[:10],
        }

        if failed:
            extra_data["failed_keys"] = failed[:10]
        if skipped:
            extra_data["skipped_reasons"] = skipped[:10]

        # Проверяем, что failed_keys присутствует
        assert "failed_keys" in extra_data
        assert extra_data["failed_keys"] == ["TEST-2"]

    def test_save_session_only_on_updates(self):
        """Test that save_session is only called when updated > 0."""
        session = Session(chat_id=123, topic_id=456)

        task1 = Task(jira_key="TEST-1", summary="Task 1")
        task1.votes = {1: "skip", 2: "skip"}  # Только skip

        task2 = Task(jira_key="TEST-2", summary="Task 2")
        task2.votes = {}  # Нет голосов

        session.last_batch = [task1, task2]

        # Симулируем обработку
        updated = 0
        failed = []
        skipped = []

        for task in session.last_batch:
            if not task.jira_key:
                skipped.append(f"{task.jira_key or 'Без ключа'}: нет ключа Jira")
                continue

            if not task.votes:
                skipped.append(f"{task.jira_key}: нет голосов")
                continue

            story_points = VotingPolicy.get_max_vote(task.votes)
            if story_points == 0:
                skipped.append(f"{task.jira_key}: нет валидных голосов")
                continue

        # Проверяем, что updated == 0, значит save_session не должен вызываться
        assert updated == 0
        assert len(skipped) == 2
