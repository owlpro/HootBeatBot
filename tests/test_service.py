import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from music_bridge.db import Database
from music_bridge.domain import DownloadedMedia, MediaKind, SourceMedia, SourceRequest
from music_bridge.repositories import Repository
from music_bridge.scheduler import ActiveJobRegistry
from music_bridge.service import BridgeOperationalError, BridgeService
from music_bridge.settings import TopicConfig


class FakeSource:
    def __init__(self) -> None:
        self.requests: list[SourceRequest] = []

    async def request_track(self, request: SourceRequest) -> SourceMedia:
        self.requests.append(request)
        return SourceMedia(1, len(self.requests), MediaKind.AUDIO, "song.mp3", "audio/mpeg", 3)

    async def _chunks(self) -> AsyncIterator[bytes]:
        yield b"abc"

    def iter_download(self, media: SourceMedia) -> AsyncIterator[bytes]:
        del media
        return self._chunks()


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, DownloadedMedia, str]] = []

    async def publish(
        self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
    ) -> int:
        self.calls.append((chat_id, thread_id, media, genre))
        return 500 + len(self.calls)


class FailingPublisher(FakePublisher):
    async def publish(
        self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
    ) -> int:
        del chat_id, thread_id, media, genre
        raise RuntimeError("https://api.telegram.org/botSECRET private payload")


def topic() -> TopicConfig:
    return TopicConfig(
        name="rock",
        chat_id=-1001234567890,
        message_thread_id=88,
        genre="rock",
        request_template="give {genre}",
        hour=9,
        minute=0,
    )


@pytest.mark.asyncio
async def test_service_is_occurrence_idempotent_and_skips_duplicate_content(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    source, publisher = FakeSource(), FakePublisher()
    service = BridgeService(
        Repository(database.session_factory), source, publisher, 100, tmp_path / "work"
    )
    occurrence = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)

    first = await service.run(topic(), occurrence)
    repeated = await service.run(topic(), occurrence)
    duplicate = await service.run(topic(), occurrence + timedelta(days=1))

    assert first == "succeeded"
    assert repeated == "already_reserved"
    assert duplicate == "skipped_duplicate"
    assert len(source.requests) == 2
    assert len(publisher.calls) == 1
    assert publisher.calls[0][0:2] == (-1001234567890, 88)
    assert list((tmp_path / "work").glob("music-bridge-*")) == []
    await database.dispose()


@pytest.mark.asyncio
async def test_publish_failure_is_ambiguous_and_redacted(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    service = BridgeService(repo, FakeSource(), FailingPublisher(), 100, tmp_path / "work")
    occurrence = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)
    with pytest.raises(BridgeOperationalError, match="publish_error") as caught:
        await service.run(topic(), occurrence)
    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None
    saved = await repo.get_delivery(1)
    assert saved.status == "ambiguous"
    assert saved.error_detail == "publish_error"
    await database.dispose()


@pytest.mark.asyncio
async def test_recording_failure_does_not_expose_original_exception(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    service = BridgeService(repo, FakeSource(), FailingPublisher(), 100, tmp_path / "work")

    async def cannot_record(*args, **kwargs) -> None:
        del args, kwargs
        raise OSError("database unavailable")

    monkeypatch.setattr(repo, "mark_ambiguous", cannot_record)
    with pytest.raises(BridgeOperationalError, match="publish_error") as caught:
        await service.run(topic(), datetime(2026, 9, 23, 5, 30, tzinfo=UTC))
    assert "SECRET" not in str(caught.value)
    await database.dispose()


@pytest.mark.asyncio
async def test_cancelled_publish_is_recorded_without_swallowing_cancellation(
    tmp_path: Path,
) -> None:
    class CancelledPublisher(FakePublisher):
        async def publish(
            self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
        ) -> int:
            del chat_id, thread_id, media, genre
            raise asyncio.CancelledError

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    service = BridgeService(repo, FakeSource(), CancelledPublisher(), 100, tmp_path / "work")
    with pytest.raises(asyncio.CancelledError):
        await service.run(topic(), datetime(2026, 9, 24, 5, 30, tzinfo=UTC))
    saved = await repo.get_delivery(1)
    assert saved.status == "ambiguous"
    assert saved.error_detail == "cancelled_during_publish"
    await database.dispose()


@pytest.mark.asyncio
async def test_job_shutdown_persists_cancelled_delivery_before_database_disposal(
    tmp_path: Path, monkeypatch
) -> None:
    publish_started = asyncio.Event()
    events: list[str] = []

    class BlockingPublisher(FakePublisher):
        async def publish(
            self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
        ) -> int:
            del chat_id, thread_id, media, genre
            publish_started.set()
            await asyncio.Event().wait()
            return 0

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    service = BridgeService(repo, FakeSource(), BlockingPublisher(), 100, tmp_path / "work")
    registry = ActiveJobRegistry()
    original_mark_ambiguous = repo.mark_ambiguous
    original_dispose = database.dispose

    async def mark_ambiguous(*args, **kwargs) -> None:
        await original_mark_ambiguous(*args, **kwargs)
        events.append("status_persisted")

    async def dispose() -> None:
        events.append("database_disposed")
        await original_dispose()

    monkeypatch.setattr(repo, "mark_ambiguous", mark_ambiguous)
    monkeypatch.setattr(database, "dispose", dispose)
    job = asyncio.create_task(
        registry.run(lambda: service.run(topic(), datetime(2026, 9, 24, 5, 30, tzinfo=UTC)))
    )
    await publish_started.wait()

    registry.stop_accepting()
    await registry.cancel_and_wait()
    await database.dispose()

    assert job.cancelled()
    assert events == ["status_persisted", "database_disposed"]


@pytest.mark.asyncio
async def test_database_failure_after_publish_retains_message_id_as_ambiguous(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    service = BridgeService(repo, FakeSource(), FakePublisher(), 100, tmp_path / "work")

    async def fail_success(*args, **kwargs) -> None:
        del args, kwargs
        raise OSError("database unavailable")

    monkeypatch.setattr(repo, "mark_success", fail_success)
    with pytest.raises(BridgeOperationalError, match="post_publish_persistence_error"):
        await service.run(topic(), datetime(2026, 9, 25, 5, 30, tzinfo=UTC))
    saved = await repo.get_delivery(1)
    assert saved.status == "ambiguous"
    assert saved.error_detail == "post_publish_persistence_error"
    assert saved.destination_message_id == 501
    await database.dispose()
