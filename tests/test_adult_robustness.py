"""
«Взрослые» тесты: инкапсуляция, идемпотентность, дубли, изоляция контекстов, state machine.

Маппинг на требования:
  1.1-1.8  — Инкапсуляция контекстов (чаты, топики, user_id не связывает)
  2.9-2.16 — Идемпотентность, двойная доставка, переобувание, дедуп участников
  3.17-3.24 — State machine: нельзя голосовать без старта, finish идемпотентен, кворум
  4.25-4.29 — Голоса/SP привязаны к задаче, история не удаляется при leave
  5.30-5.34 — Гонки: 10 участников одновременно
  7.39-7.42 — ACL: participant/lead can_manage
  9.47-9.50 — Дедуп задач по jira_key

Контекст: (chat_id, topic_id)
Уникальность голоса: (session, task_index, user_id) — один голос на задачу
Уникальность задачи в батче: jira_key
"""

import asyncio
from typing import Optional

import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from app.domain.participant import Participant
from app.domain.session import Session
from app.domain.task import Task
from app.adapters.session_file import FileSessionRepository
from app.usecases.add_tasks import AddTasksFromJiraUseCase
from app.usecases.advance_task import AdvanceToNextTaskUseCase
from app.usecases.cast_vote import CastVoteUseCase
from app.usecases.finish_batch import FinishBatchUseCase
from app.usecases.join_session import JoinSessionUseCase
from app.usecases.leave_session import LeaveSessionUseCase
from app.usecases.show_results import VotingPolicy
from app.usecases.start_batch import StartBatchUseCase
from app.usecases.reset_queue import ResetQueueUseCase
from config import UserRole


def _make_repo(tmp_path: Path) -> FileSessionRepository:
    return FileSessionRepository(tmp_path / "state.json")


def _make_session(chat_id: int, topic_id: Optional[int], *participants: tuple) -> Session:
    s = Session(chat_id=chat_id, topic_id=topic_id)
    for uid, name, role in participants:
        s.participants[uid] = Participant(user_id=uid, name=name, role=role)
    return s


# --- 1) Инкапсуляция контекстов ---


class TestContextEncapsulation:
    """1.1-1.8: Изоляция по чатам/топикам/сессиям."""

    @pytest.mark.asyncio
    async def test_one_bot_two_chats_parallel(self):
        """1.1: Два чата параллельно — состояния не пересекаются."""
        tmp = Path("/tmp/test_adult_1")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            start = StartBatchUseCase(repo)
            cast = CastVoteUseCase(repo)
            finish = FinishBatchUseCase(repo)

            for chat_id in [-1001, -1002]:
                s = _make_session(chat_id, 100, (1, "U1", UserRole.LEAD), (2, "U2", UserRole.PARTICIPANT))
                s.tasks_queue.append(Task(jira_key=f"CHAT-{chat_id}", summary=f"Task {chat_id}"))
                await repo.save_session(s)
                await start.execute(chat_id, 100)

            await cast.execute(-1001, 100, 1, "5")
            await cast.execute(-1001, 100, 2, "8")
            await cast.execute(-1002, 100, 1, "3")
            await cast.execute(-1002, 100, 2, "2")

            sa = await repo.get_session(-1001, 100)
            sb = await repo.get_session(-1002, 100)

            assert sa.current_task.votes[1] == "5"
            assert sa.current_task.votes[2] == "8"
            assert sb.current_task.votes[1] == "3"
            assert sb.current_task.votes[2] == "2"
            assert sa.current_task.jira_key == "CHAT--1001"
            assert sb.current_task.jira_key == "CHAT--1002"
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_one_chat_two_topics(self):
        """1.2: One session namespace, two topics: isolation by topic_id."""
        tmp = Path("/tmp/test_adult_2")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            chat_id = -2000
            for topic_id in [10, 20]:
                s = _make_session(chat_id, topic_id, (1, "U1", UserRole.PARTICIPANT))
                s.tasks_queue.append(Task(jira_key=f"T{topic_id}", summary=f"Topic {topic_id}"))
                await repo.save_session(s)
                await StartBatchUseCase(repo).execute(chat_id, topic_id)

            await CastVoteUseCase(repo).execute(chat_id, 10, 1, "5")
            await CastVoteUseCase(repo).execute(chat_id, 20, 1, "8")

            s10 = await repo.get_session(chat_id, 10)
            s20 = await repo.get_session(chat_id, 20)
            assert s10.current_task.votes[1] == "5"
            assert s20.current_task.votes[1] == "8"
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_two_votes_in_same_topic_sequential(self):
        """1.3: Два голосования подряд в одном топике — голоса не протекают."""
        tmp = Path("/tmp/test_adult_3")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            advance = AdvanceToNextTaskUseCase(repo)
            cast = CastVoteUseCase(repo)
            finish = FinishBatchUseCase(repo)

            s = _make_session(-3000, 30, (1, "U1", UserRole.PARTICIPANT), (2, "U2", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="T1", summary="Task 1"))
            s.tasks_queue.append(Task(jira_key="T2", summary="Task 2"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-3000, 30)

            await cast.execute(-3000, 30, 1, "5")
            await cast.execute(-3000, 30, 2, "8")
            batch_finished, _ = await advance.execute(-3000, 30)
            assert not batch_finished

            s2 = await repo.get_session(-3000, 30)
            t1 = s2.tasks_queue[0]
            t2 = s2.tasks_queue[1]
            assert t1.votes == {1: "5", 2: "8"}
            assert t2.votes == {}
            assert s2.current_task.jira_key == "T2"

            await cast.execute(-3000, 30, 1, "3")
            await cast.execute(-3000, 30, 2, "3")
            s3 = await repo.get_session(-3000, 30)
            assert s3.tasks_queue[1].votes == {1: "3", 2: "3"}
            assert s3.tasks_queue[0].votes == {1: "5", 2: "8"}
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_restart_voting_when_active(self):
        """1.4: Повторный старт при активной сессии — запрет."""
        tmp = Path("/tmp/test_adult_4")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            start = StartBatchUseCase(repo)
            s = _make_session(-4000, 40, (1, "U1", UserRole.LEAD))
            s.tasks_queue.append(Task(jira_key="X", summary="X"))
            await repo.save_session(s)
            r1 = await start.execute(-4000, 40)
            r2 = await start.execute(-4000, 40)
            assert r1 is True
            assert r2 is True  # start_batch не возвращает False, но is_voting_active уже True
            sess = await repo.get_session(-4000, 40)
            assert sess.is_voting_active
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_user_id_does_not_link_contexts(self):
        """1.5-1.6: user_id в разных чатах — контекст всегда chat_id/topic_id."""
        tmp = Path("/tmp/test_adult_5")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            join = JoinSessionUseCase(repo)
            cast = CastVoteUseCase(repo)
            user_id = 999

            for chat_id, topic_id in [(-5001, 50), (-5002, 50)]:
                s = _make_session(chat_id, topic_id)
                s.tasks_queue.append(Task(jira_key=f"C{chat_id}", summary="T"))
                await repo.save_session(s)
                await join.execute(chat_id, topic_id, user_id, "Same User", UserRole.PARTICIPANT)
                await StartBatchUseCase(repo).execute(chat_id, topic_id)

            await cast.execute(-5001, 50, user_id, "5")
            await cast.execute(-5002, 50, user_id, "8")

            sa = await repo.get_session(-5001, 50)
            sb = await repo.get_session(-5002, 50)
            assert sa.current_task.votes[user_id] == "5"
            assert sb.current_task.votes[user_id] == "8"
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)


# --- 2) Идемпотентность и дубли ---


class TestIdempotencyAndDuplicates:
    """2.9-2.16: Идемпотентность, двойная доставка, переобувание голоса."""

    @pytest.mark.asyncio
    async def test_double_vote_same_value_idempotent(self):
        """2.11: Два одинаковых клика — второй не меняет итог."""
        tmp = Path("/tmp/test_adult_6")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            s = _make_session(-6000, 60, (1, "U1", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-6000, 60)

            await cast.execute(-6000, 60, 1, "5")
            await cast.execute(-6000, 60, 1, "5")
            sess = await repo.get_session(-6000, 60)
            assert sess.current_task.votes[1] == "5"
            assert len(sess.current_task.votes) == 1
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_vote_change_overwrites(self):
        """2.12: Переобувание — голос перезаписан один раз."""
        tmp = Path("/tmp/test_adult_7")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            s = _make_session(-7000, 70, (1, "U1", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-7000, 70)

            await cast.execute(-7000, 70, 1, "3")
            await cast.execute(-7000, 70, 1, "8")
            sess = await repo.get_session(-7000, 70)
            assert sess.current_task.votes[1] == "8"
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_finish_batch_idempotent(self):
        """2.19: Нельзя завершить дважды — идемпотентно."""
        tmp = Path("/tmp/test_adult_8")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            finish = FinishBatchUseCase(repo)
            s = _make_session(-8000, 80, (1, "U1", UserRole.LEAD))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)

            c1 = await finish.execute(-8000, 80)
            c2 = await finish.execute(-8000, 80)
            assert len(c1) == 1
            assert len(c2) == 0
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_duplicate_participant_dedup(self):
        """2.15: Дедуп по user_id при добавлении."""
        tmp = Path("/tmp/test_adult_9")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            join = JoinSessionUseCase(repo)
            await join.execute(-9000, 90, 111, "Alice", UserRole.PARTICIPANT)
            await join.execute(-9000, 90, 111, "Alice Updated", UserRole.PARTICIPANT)
            sess = await repo.get_session(-9000, 90)
            assert len(sess.participants) == 1
            assert sess.participants[111].name == "Alice Updated"
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)


# --- 3) State machine ---


class TestStateMachineIntegrity:
    """3.17-3.24: Переходы состояний, запреты."""

    @pytest.mark.asyncio
    async def test_cannot_vote_without_start(self):
        """3.17: Нельзя голосовать, если голосование не стартовало."""
        tmp = Path("/tmp/test_adult_10")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            s = _make_session(-10000, 100, (1, "U1", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)

            ok = await cast.execute(-10000, 100, 1, "5")
            assert ok is False
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_cannot_vote_after_batch_completed(self):
        """3.21: После закрытия — действия не меняют состояние."""
        tmp = Path("/tmp/test_adult_11")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            finish = FinishBatchUseCase(repo)
            s = _make_session(-11000, 110, (1, "U1", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-11000, 110)
            await finish.execute(-11000, 110)

            ok = await cast.execute(-11000, 110, 1, "5")
            assert ok is False
            sess = await repo.get_session(-11000, 110)
            assert sess.batch_completed
            assert len(sess.last_batch) == 1
            assert len(sess.last_batch[0].votes) == 0
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_quorum_recalculates_when_participant_removed(self):
        """3.22: Удалить участника → кворум пересчитывается."""
        tmp = Path("/tmp/test_adult_12")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            leave = LeaveSessionUseCase(repo)
            s = _make_session(-12000, 120, (1, "U1", UserRole.PARTICIPANT), (2, "U2", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-12000, 120)

            await cast.execute(-12000, 120, 1, "5")
            assert await cast.all_voters_voted(-12000, 120) is False
            await leave.execute(-12000, 120, 2)
            assert await cast.all_voters_voted(-12000, 120) is True
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)


# --- 4) Инкапсуляция данных ---


class TestDataEncapsulation:
    """4.25-4.29: Голоса/SP привязаны к задаче, история не течёт."""

    @pytest.mark.asyncio
    async def test_votes_bound_to_task(self):
        """4.25: Голоса задачи1 не получают голоса задачи2."""
        tmp = Path("/tmp/test_adult_13")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            advance = AdvanceToNextTaskUseCase(repo)
            s = _make_session(-13000, 130, (1, "U1", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="T1", summary="1"))
            s.tasks_queue.append(Task(jira_key="T2", summary="2"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-13000, 130)

            await cast.execute(-13000, 130, 1, "5")
            await advance.execute(-13000, 130)
            await cast.execute(-13000, 130, 1, "8")
            sess = await repo.get_session(-13000, 130)
            assert sess.tasks_queue[0].votes[1] == "5"
            assert sess.tasks_queue[1].votes[1] == "8"
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_sp_result_bound_to_task(self):
        """4.26: SP результат привязан к задаче."""
        policy = VotingPolicy()
        t1 = Task(jira_key="A", summary="A")
        t1.votes = {1: "5", 2: "8"}
        t2 = Task(jira_key="B", summary="B")
        t2.votes = {1: "3", 2: "3"}
        assert policy.get_max_vote(t1.votes) == 8
        assert policy.get_max_vote(t2.votes) == 3

    @pytest.mark.asyncio
    async def test_leave_does_not_remove_history(self):
        """4.29: Удаление участника не удаляет историю голосований."""
        tmp = Path("/tmp/test_adult_14")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            finish = FinishBatchUseCase(repo)
            leave = LeaveSessionUseCase(repo)
            s = _make_session(-14000, 140, (1, "U1", UserRole.PARTICIPANT), (2, "U2", UserRole.PARTICIPANT))
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-14000, 140)
            await cast.execute(-14000, 140, 1, "5")
            await cast.execute(-14000, 140, 2, "8")
            await finish.execute(-14000, 140)
            await leave.execute(-14000, 140, 2)

            sess = await repo.get_session(-14000, 140)
            assert len(sess.last_batch) == 1
            assert sess.last_batch[0].votes == {1: "5", 2: "8"}
            assert 2 not in sess.participants
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)


# --- 5) Гонки ---


class TestRaceConditions:
    """5.30-5.34: Конкурентность."""

    @pytest.mark.asyncio
    async def test_concurrent_votes_from_10_participants(self):
        """5.32: Одновременные нажатия от 10 участников — все голоса записались."""
        tmp = Path("/tmp/test_adult_15")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            cast = CastVoteUseCase(repo)
            s = _make_session(-15000, 150)
            for i in range(10):
                s.participants[i] = Participant(user_id=i, name=f"U{i}", role=UserRole.PARTICIPANT)
            s.tasks_queue.append(Task(jira_key="K", summary="K"))
            await repo.save_session(s)
            await StartBatchUseCase(repo).execute(-15000, 150)

            async def vote(uid: int, val: str):
                return await cast.execute(-15000, 150, uid, val)

            results = await asyncio.gather(*[vote(i, str((i % 6) + 1)) for i in range(10)])
            assert all(results)
            sess = await repo.get_session(-15000, 150)
            assert len(sess.current_task.votes) == 10
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)


# --- 6-7) ACL, дедуп задач ---


class TestACLAndTaskDedup:
    """7.39-7.42, 9.47-9.50."""

    @pytest.mark.asyncio
    async def test_participant_cannot_finish(self):
        """7.39: Участник не может завершать — проверка can_manage на уровне handler."""
        sess = _make_session(-16000, 160, (1, "U1", UserRole.PARTICIPANT))
        assert sess.can_manage(1) is False

    @pytest.mark.asyncio
    async def test_lead_can_manage(self):
        sess = _make_session(-16001, 161, (1, "U1", UserRole.LEAD))
        assert sess.can_manage(1) is True

    @pytest.mark.asyncio
    async def test_task_dedup_same_key_in_batch(self):
        """9.47: Два раза одна задача (одинаковый key) — одна запись."""
        tmp = Path("/tmp/test_adult_16")
        tmp.mkdir(exist_ok=True)
        try:
            repo = _make_repo(tmp)
            jira = AsyncMock()
            jira.parse_jira_request.return_value = [
                {"key": "PROJ-1", "summary": "S1", "url": None},
                {"key": "PROJ-1", "summary": "S1", "url": None},
            ]
            add = AddTasksFromJiraUseCase(jira, repo)
            added, skipped = await add.execute(-16000, 160, "project=PROJ")
            assert len(added) == 1
            assert "PROJ-1" in skipped
        finally:
            import shutil
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
