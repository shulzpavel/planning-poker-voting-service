"""Coalesced background sync for the CMS read model."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.domain.session import Session, SessionFactory

logger = logging.getLogger(__name__)


def session_identity(chat_id: int, topic_id: Optional[int]) -> str:
    topic_part = "none" if topic_id is None else str(topic_id)
    return f"{chat_id}:{topic_part}"


def snapshot_session(session: Session) -> Session:
    return SessionFactory.from_dict(SessionFactory.to_dict(session), session.chat_id, session.topic_id)


class CmsSyncScheduler:
    """Debounce and coalesce expensive full-session CMS syncs.

    Voting writes stay on the critical path; the read model catches up in the
    background and keeps only the latest session snapshot per session key.
    """

    def __init__(self, cms_store, debounce_seconds: float = 0.15):
        self.cms_store = cms_store
        self.debounce_seconds = debounce_seconds
        self._pending: dict[str, Session] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False

    def schedule(self, session: Session) -> None:
        if self._closed:
            return
        key = session_identity(session.chat_id, session.topic_id)
        self._pending[key] = snapshot_session(session)
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(self._run(key))

    async def _run(self, key: str) -> None:
        # Re-check pending after popping the worker slot so that any schedule()
        # call racing with our exit creates a new worker instead of losing data.
        try:
            await asyncio.sleep(self.debounce_seconds)
            while True:
                session = self._pending.pop(key, None)
                if session is None:
                    return
                try:
                    await self.cms_store.sync_session(session)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CMS background sync failed: key=%s error=%s", key, exc)
        finally:
            self._tasks.pop(key, None)
            # If schedule() raced with us between the pop above and now, the
            # entry is back in _pending but we already removed ourselves from
            # _tasks. Reschedule so no snapshot is lost.
            if not self._closed and key in self._pending:
                self._tasks[key] = asyncio.create_task(self._run(key))

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in list(self._tasks.values()) if not task.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
