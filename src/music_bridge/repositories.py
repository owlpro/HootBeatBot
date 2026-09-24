"""Transactional delivery and track repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from music_bridge.models import DeliveryRow, TrackRow

ERROR_CATEGORIES = {
    "cancelled",
    "cancelled_during_publish",
    "download_or_persistence_error",
    "internal_error",
    "post_publish_persistence_error",
    "publish_error",
    "source_error",
    "startup_recovery",
}


def _classified_detail(detail: str) -> str:
    return detail if detail in ERROR_CATEGORIES else "internal_error"


class Repository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def reserve_delivery(
        self, topic_name: str, scheduled_for: datetime
    ) -> DeliveryRow | None:
        async with self._sessions() as session:
            row = DeliveryRow(topic_name=topic_name, scheduled_for=scheduled_for, status="queued")
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(row)
            return row

    async def get_delivery(self, delivery_id: int) -> DeliveryRow:
        async with self._sessions() as session:
            row = await session.get(DeliveryRow, delivery_id)
            if row is None:
                raise LookupError(delivery_id)
            return row

    async def has_track_hash(self, sha256: str) -> bool:
        async with self._sessions() as session:
            result = await session.scalar(
                select(TrackRow.id).where(TrackRow.content_sha256 == sha256)
            )
            return result is not None

    async def get_used_source_message_ids(self) -> frozenset[int]:
        async with self._sessions() as session:
            result = await session.scalars(select(TrackRow.source_message_id))
            return frozenset(result.all())

    async def add_track(
        self,
        *,
        source_chat_id: int,
        source_message_id: int,
        content_sha256: str,
        title: str | None,
        performer: str | None,
        file_name: str | None,
        mime_type: str | None,
        file_size: int,
    ) -> int:
        async with self._sessions() as session:
            row = TrackRow(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                content_sha256=content_sha256,
                title=title,
                performer=performer,
                file_name=file_name,
                mime_type=mime_type,
                file_size=file_size,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def claim_track_for_publish(
        self,
        delivery_id: int,
        *,
        source_chat_id: int,
        source_message_id: int,
        content_sha256: str,
        title: str | None,
        performer: str | None,
        file_name: str | None,
        mime_type: str | None,
        file_size: int,
    ) -> int | None:
        """Atomically claim a hash and move its delivery to publishing."""
        async with self._sessions() as session:
            delivery = await session.get(DeliveryRow, delivery_id)
            if delivery is None:
                raise LookupError(delivery_id)
            track = TrackRow(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                content_sha256=content_sha256,
                title=title,
                performer=performer,
                file_name=file_name,
                mime_type=mime_type,
                file_size=file_size,
            )
            session.add(track)
            try:
                await session.flush()
                delivery.track_id = track.id
                delivery.status = "publishing"
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return track.id

    async def recover_nonterminal(self) -> None:
        """Close deliveries whose prior process ended without a terminal result."""
        async with self._sessions() as session:
            await session.execute(
                update(DeliveryRow)
                .where(DeliveryRow.status == "queued")
                .values(status="interrupted", error_detail="startup_recovery")
            )
            await session.execute(
                update(DeliveryRow)
                .where(DeliveryRow.status == "publishing")
                .values(status="ambiguous", error_detail="startup_recovery")
            )
            await session.commit()

    async def _set_status(self, delivery_id: int, status: str, **values: object) -> None:
        async with self._sessions() as session:
            row = await session.get(DeliveryRow, delivery_id)
            if row is None:
                raise LookupError(delivery_id)
            row.status = status
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()

    async def mark_success(self, delivery_id: int, track_id: int, message_id: int) -> None:
        await self._set_status(
            delivery_id,
            "succeeded",
            track_id=track_id,
            destination_message_id=message_id,
        )

    async def mark_duplicate(self, delivery_id: int) -> None:
        await self._set_status(delivery_id, "skipped_duplicate")

    async def mark_failed(self, delivery_id: int, detail: str) -> None:
        await self._set_status(
            delivery_id, "permanently_failed", error_detail=_classified_detail(detail)
        )

    async def mark_ambiguous(
        self, delivery_id: int, detail: str, message_id: int | None = None
    ) -> None:
        await self._set_status(
            delivery_id,
            "ambiguous",
            error_detail=_classified_detail(detail),
            destination_message_id=message_id,
        )

    async def mark_cancelled(self, delivery_id: int) -> None:
        await self._set_status(delivery_id, "interrupted", error_detail="cancelled")
