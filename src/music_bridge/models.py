"""SQLite-compatible persistence models for the single-process MVP."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TrackRow(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str | None]
    performer: Mapped[str | None]
    file_name: Mapped[str | None]
    mime_type: Mapped[str | None]
    file_size: Mapped[int]
    __table_args__ = (UniqueConstraint("source_chat_id", "source_message_id"),)


class DeliveryRow(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_name: Mapped[str] = mapped_column(String(100))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"))
    destination_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_detail: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (
        UniqueConstraint("topic_name", "scheduled_for", name="uq_delivery_occurrence"),
        Index("ix_deliveries_status", "status"),
    )
