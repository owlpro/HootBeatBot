from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from music_bridge.destination.aiogram_publisher import AiogramPublisher
from music_bridge.domain import DownloadedMedia, MediaKind, SourceMedia


class FakeBot:
    def __init__(self) -> None:
        self.audio: dict[str, Any] | None = None
        self.document: dict[str, Any] | None = None

    async def send_audio(self, **kwargs: Any) -> Any:
        self.audio = kwargs
        return SimpleNamespace(message_id=42)

    async def send_document(self, **kwargs: Any) -> Any:
        self.document = kwargs
        return SimpleNamespace(message_id=43)


def downloaded(tmp_path: Path, kind: MediaKind = MediaKind.AUDIO) -> DownloadedMedia:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"abc")
    return DownloadedMedia(
        source=SourceMedia(1, 2, kind, "song.mp3", "audio/mpeg", 3, "<Title>", "A & B"),
        path=path,
        size=3,
        sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_publisher_routes_audio_to_exact_topic_and_escapes_caption(tmp_path: Path) -> None:
    bot = FakeBot()
    publisher = AiogramPublisher(bot, "@source")
    message_id = await publisher.publish(-1001234567890, 99, downloaded(tmp_path), "R&B")
    assert message_id == 42
    assert bot.audio is not None
    assert bot.audio["chat_id"] == -1001234567890
    assert bot.audio["message_thread_id"] == 99
    assert bot.audio["title"] == "<Title>"
    assert bot.audio["performer"] == "A & B"
    assert "R&amp;B" in bot.audio["caption"]
    assert "&lt;Title&gt;" in bot.audio["caption"]
    assert bot.audio["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_publisher_uses_document_for_document_media(tmp_path: Path) -> None:
    bot = FakeBot()
    publisher = AiogramPublisher(bot, "@source")
    message_id = await publisher.publish(
        -1001234567890, 99, downloaded(tmp_path, MediaKind.DOCUMENT), "jazz"
    )
    assert message_id == 43
    assert bot.document is not None
    assert bot.audio is None
