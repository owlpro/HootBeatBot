"""aiogram destination adapter with explicit forum-topic routing."""

from __future__ import annotations

from html import escape
from typing import Any, Protocol

from aiogram.types import FSInputFile

from music_bridge.domain import DownloadedMedia, MediaKind


class BotLike(Protocol):
    async def send_audio(self, **kwargs: Any) -> Any: ...

    async def send_document(self, **kwargs: Any) -> Any: ...


class AiogramPublisher:
    def __init__(self, bot: BotLike, source_username: str) -> None:
        self._bot = bot
        self._source = source_username

    def _caption(self, media: DownloadedMedia, genre: str) -> str:
        title = escape(media.source.title or media.source.file_name or "Unknown track")
        performer = escape(media.source.performer or "Unknown artist")
        return (
            f"<b>{title}</b> — {performer}\nGenre: {escape(genre)}\nSource: {escape(self._source)}"
        )

    async def publish(
        self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
    ) -> int:
        common = {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "caption": self._caption(media, genre),
            "parse_mode": "HTML",
        }
        upload = FSInputFile(media.path, filename=media.source.file_name or media.path.name)
        if media.source.kind is MediaKind.AUDIO:
            message = await self._bot.send_audio(
                audio=upload,
                title=media.source.title,
                performer=media.source.performer,
                **common,
            )
        else:
            message = await self._bot.send_document(document=upload, **common)
        return int(message.message_id)
