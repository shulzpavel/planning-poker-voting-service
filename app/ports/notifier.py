"""Notifier interface for sending messages."""

from abc import ABC, abstractmethod
from typing import Any, Optional


class Notifier(ABC):
    """Interface for sending notifications/messages."""

    @abstractmethod
    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Any] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
    ) -> Optional[Any]:
        """Send text message."""
        pass

    @abstractmethod
    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[Any] = None,
        disable_web_page_preview: bool = False,
    ) -> Optional[Any]:
        """Edit existing message."""
        pass

    @abstractmethod
    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Delete message."""
        pass

    @abstractmethod
    async def send_document(
        self,
        chat_id: int,
        document: Any,
        caption: Optional[str] = None,
        reply_markup: Optional[Any] = None,
    ) -> Optional[Any]:
        """Send document."""
        pass
