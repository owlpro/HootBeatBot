"""Framework-independent domain types and ports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class MediaKind(StrEnum):
    AUDIO = "audio"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class SourceRequest:
    query: str


@dataclass(frozen=True, slots=True)
class SourceMedia:
    source_chat_id: int
    source_message_id: int
    kind: MediaKind
    file_name: str | None
    mime_type: str | None
    size: int | None
    title: str | None = None
    performer: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    source: SourceMedia
    path: Path
    size: int
    sha256: str


class MusicSource(Protocol):
    async def request_track(self, request: SourceRequest) -> SourceMedia: ...

    def iter_download(self, media: SourceMedia) -> AsyncIterator[bytes]: ...


class DestinationPublisher(Protocol):
    async def publish(
        self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
    ) -> int: ...
