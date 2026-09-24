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
    def __init__(self, payloads: list[bytes] | None = None) -> None:
        self.requests: list[SourceRequest] = []
        self.discarded: list[SourceMedia] = []
        self.payloads = payloads or [b"abc"]
        self.current = b""

    async def request_track(self, request: SourceRequest) -> SourceMedia:
        self.requests.append(request)
        self.current = self.payloads[min(len(self.requests) - 1, len(self.payloads) - 1)]
        return SourceMedia(1, len(self.requests), MediaKind.AUDIO, "song.mp3", "audio/mpeg", 3)

    async def _chunks(self) -> AsyncIterator[bytes]:
        yield self.current

    def iter_download(self, media: SourceMedia) -> AsyncIterator[bytes]:
        del media
        return self._chunks()

    async def discard(self, media: SourceMedia) -> None:
        self.discarded.append(media)


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


def direct_topic() -> TopicConfig:
    return TopicConfig(
        name="rock_direct",
        chat_id=-1001234567890,
        message_thread_id=88,
        genre="https://soundcloud.com/artist/full-rock-track",
        request_template="{genre}",
        hour=9,
        minute=0,
    )


def multi_query_direct_topic() -> TopicConfig:
    return TopicConfig(
        name="rock_direct_multi",
        chat_id=-1001234567890,
        message_thread_id=88,
        genre="rock",
        request_template="{genre}",
        catalog_queries=[
            "https://soundcloud.com/artist/full-rock-track",
            "rock fallback search",
        ],
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
async def test_service_excludes_all_previously_used_source_ids(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    await repo.add_track(
        source_chat_id=-1,
        source_message_id=73,
        content_sha256="d" * 64,
        title=None,
        performer=None,
        file_name="old.mp3",
        mime_type="audio/mpeg",
        file_size=3,
    )
    source = FakeSource()
    service = BridgeService(repo, source, FakePublisher(), 100, tmp_path / "work")

    assert await service.run(topic(), datetime(2026, 9, 26, 5, 30, tzinfo=UTC)) == "succeeded"

    assert source.requests == [SourceRequest("give rock", frozenset({73}))]
    assert isinstance(source.requests[0].excluded_source_message_ids, frozenset)
    await database.dispose()


@pytest.mark.asyncio
async def test_workspace_creation_failure_discards_prepared_source_media(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    source = FakeSource()
    workspace_root = tmp_path / "not-a-directory"
    workspace_root.write_text("occupied")
    service = BridgeService(
        Repository(database.session_factory), source, FakePublisher(), 100, workspace_root
    )

    with pytest.raises(BridgeOperationalError, match="download_or_persistence_error"):
        await service.run(topic(), datetime(2026, 9, 28, 5, 30, tzinfo=UTC))

    assert source.discarded == [SourceMedia(1, 1, MediaKind.AUDIO, "song.mp3", "audio/mpeg", 3)]
    await database.dispose()


@pytest.mark.asyncio
async def test_content_duplicate_falls_back_to_next_candidate_without_double_delivery(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    publisher = FakePublisher()
    first_service = BridgeService(
        repo, FakeSource([b"same"]), publisher, 100, tmp_path / "work", source_attempts=3
    )
    assert await first_service.run(topic(), datetime(2026, 9, 26, 5, 30, tzinfo=UTC)) == "succeeded"

    source = FakeSource([b"same", b"different"])
    service = BridgeService(repo, source, publisher, 100, tmp_path / "work", source_attempts=3)
    result = await service.run(topic(), datetime(2026, 9, 27, 5, 30, tzinfo=UTC))

    assert result == "succeeded"
    assert len(source.requests) == 2
    assert source.requests[1].excluded_source_message_ids.issuperset({1})
    assert len(publisher.calls) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_direct_url_duplicate_does_not_retry_or_become_source_error(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    publisher = FakePublisher()
    first_source = FakeSource([b"same"])
    first_service = BridgeService(
        repo, first_source, publisher, 100, tmp_path / "work", source_attempts=3
    )
    assert (
        await first_service.run(direct_topic(), datetime(2026, 9, 26, 5, 30, tzinfo=UTC))
        == "succeeded"
    )

    duplicate_source = FakeSource([b"same", b"different"])
    duplicate_service = BridgeService(
        repo, duplicate_source, publisher, 100, tmp_path / "work", source_attempts=3
    )
    result = await duplicate_service.run(direct_topic(), datetime(2026, 9, 27, 5, 30, tzinfo=UTC))

    assert result == "skipped_duplicate"
    assert len(duplicate_source.requests) == 1
    saved = await repo.get_delivery(2)
    assert saved.status == "skipped_duplicate"
    assert saved.error_detail is None
    await database.dispose()


@pytest.mark.asyncio
async def test_multi_query_starting_with_direct_url_duplicate_does_not_retry(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    publisher = FakePublisher()
    first_service = BridgeService(
        repo, FakeSource([b"same"]), publisher, 100, tmp_path / "work", source_attempts=3
    )
    assert (
        await first_service.run(multi_query_direct_topic(), datetime(2026, 9, 26, 6, 0, tzinfo=UTC))
        == "succeeded"
    )

    duplicate_source = FakeSource([b"same", b"different"])
    duplicate_service = BridgeService(
        repo, duplicate_source, publisher, 100, tmp_path / "work", source_attempts=3
    )
    result = await duplicate_service.run(
        multi_query_direct_topic(), datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
    )

    assert result == "skipped_duplicate"
    assert len(duplicate_source.requests) == 1
    saved = await repo.get_delivery(2)
    assert saved.status == "skipped_duplicate"
    assert saved.error_detail is None
    await database.dispose()


@pytest.mark.asyncio
async def test_discard_failure_does_not_mask_publish_cancellation(tmp_path: Path) -> None:
    class DiscardFailingSource(FakeSource):
        async def discard(self, media: SourceMedia) -> None:
            del media
            raise RuntimeError("discard failed")

    class CancelledPublisher(FakePublisher):
        async def publish(
            self, chat_id: int, thread_id: int, media: DownloadedMedia, genre: str
        ) -> int:
            del chat_id, thread_id, media, genre
            raise asyncio.CancelledError

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    service = BridgeService(
        repo,
        DiscardFailingSource(),
        CancelledPublisher(),
        100,
        tmp_path / "work",
    )

    with pytest.raises(asyncio.CancelledError):
        await service.run(topic(), datetime(2026, 9, 28, 5, 30, tzinfo=UTC))

    saved = await repo.get_delivery(1)
    assert saved.status == "ambiguous"
    assert saved.error_detail == "cancelled_during_publish"
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
