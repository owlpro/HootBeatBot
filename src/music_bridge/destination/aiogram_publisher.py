"""aiogram destination adapter with explicit forum-topic routing."""

from __future__ import annotations

from typing import Any, Protocol

from aiogram.types import FSInputFile

from music_bridge.domain import DownloadedMedia, MediaKind


class BotLike(Protocol):
    async def send_audio(self, **kwargs: Any) -> Any: ...

    async def send_document(self, **kwargs: Any) -> Any: ...


class AiogramPublisher:
    def __init__(self, bot: BotLike) -> None:
        self._bot = bot

    async def publish(
        self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
    ) -> int:
        del genre
        common = {
            "chat_id": chat_id,
        }
        if thread_id != 1:
            common["message_thread_id"] = thread_id
        upload = FSInputFile(media.path, filename=media.source.file_name or media.path.name)
        if media.source.kind is MediaKind.AUDIO:
            audio_metadata: dict[str, Any] = {
                "title": media.source.title,
                "performer": media.source.performer,
            }
            if media.source.duration_seconds is not None:
                audio_metadata["duration"] = media.source.duration_seconds
            message = await self._bot.send_audio(
                audio=upload,
                **audio_metadata,
                **common,
            )
        else:
            message = await self._bot.send_document(document=upload, **common)
        return int(message.message_id)
