"""Idempotent source-download-dedupe-publish orchestration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from music_bridge.domain import DestinationPublisher, DownloadedMedia, MusicSource, SourceRequest
from music_bridge.repositories import Repository
from music_bridge.settings import TopicConfig
from music_bridge.tempfiles import delivery_workspace, download_bounded

logger = logging.getLogger(__name__)


class BridgeOperationalError(RuntimeError):
    """A safe, classified failure suitable for operator-facing logs."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def safe_exception_category(exc: BaseException) -> str:
    """Return a non-secret operator-facing classification for an exception."""
    if isinstance(exc, BridgeOperationalError):
        return exc.category
    return type(exc).__name__


class BridgeService:
    def __init__(
        self,
        repository: Repository,
        source: MusicSource,
        publisher: DestinationPublisher,
        max_media_bytes: int,
        workspace_root: Path,
    ) -> None:
        self._repository = repository
        self._source = source
        self._publisher = publisher
        self._max_bytes = max_media_bytes
        self._workspace_root = workspace_root

    async def _record_failure(
        self,
        delivery_id: int,
        *,
        stage: str,
        detail: str,
        message_id: int | None = None,
    ) -> None:
        """Best-effort status recording without replacing the original exception."""
        try:
            if stage in {"publishing", "published"}:
                await self._repository.mark_ambiguous(delivery_id, detail, message_id)
            else:
                await self._repository.mark_failed(delivery_id, detail)
        except Exception as record_error:
            logger.error(
                "failed to persist classified delivery failure (%s)",
                type(record_error).__name__,
            )

    async def run(self, topic: TopicConfig, scheduled_for: datetime) -> str:
        delivery = await self._repository.reserve_delivery(topic.name, scheduled_for)
        if delivery is None:
            return "already_reserved"
        stage = "source"
        message_id: int | None = None
        try:
            source_media = await self._source.request_track(SourceRequest(topic.render_request()))
            stage = "download"
            async with delivery_workspace(self._workspace_root) as workspace:
                result = await download_bounded(
                    source_media,
                    self._source.iter_download(source_media),
                    workspace,
                    self._max_bytes,
                )
                track_id = await self._repository.claim_track_for_publish(
                    delivery.id,
                    source_chat_id=source_media.source_chat_id,
                    source_message_id=source_media.source_message_id,
                    content_sha256=result.sha256,
                    title=source_media.title,
                    performer=source_media.performer,
                    file_name=source_media.file_name,
                    mime_type=source_media.mime_type,
                    file_size=result.size,
                )
                if track_id is None:
                    await self._repository.mark_duplicate(delivery.id)
                    return "skipped_duplicate"
                downloaded = DownloadedMedia(
                    source=source_media,
                    path=result.path,
                    size=result.size,
                    sha256=result.sha256,
                )
                stage = "publishing"
                message_id = await self._publisher.publish(
                    topic.chat_id,
                    topic.message_thread_id,
                    downloaded,
                    topic.genre,
                )
                stage = "published"
                await self._repository.mark_success(delivery.id, track_id, message_id)
                return "succeeded"
        except asyncio.CancelledError:
            detail = (
                "cancelled_during_publish" if stage in {"publishing", "published"} else "cancelled"
            )
            await self._record_failure(
                delivery.id, stage=stage, detail=detail, message_id=message_id
            )
            raise
        except Exception:
            if stage == "publishing":
                detail = "publish_error"
            elif stage == "published":
                detail = "post_publish_persistence_error"
            elif stage == "source":
                detail = "source_error"
            else:
                detail = "download_or_persistence_error"
            await self._record_failure(
                delivery.id, stage=stage, detail=detail, message_id=message_id
            )
            raise BridgeOperationalError(detail) from None
