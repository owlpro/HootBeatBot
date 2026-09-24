from datetime import UTC, datetime
from pathlib import Path

import pytest

from music_bridge.db import Database
from music_bridge.repositories import Repository


@pytest.mark.asyncio
async def test_delivery_occurrence_is_reserved_once(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    occurrence = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)
    first = await repo.reserve_delivery("rock", occurrence)
    second = await repo.reserve_delivery("rock", occurrence)
    assert first is not None
    assert second is None
    await database.dispose()


@pytest.mark.asyncio
async def test_track_hash_deduplication_and_success_recording(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    occurrence = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)
    delivery = await repo.reserve_delivery("jazz", occurrence)
    assert delivery is not None
    assert not await repo.has_track_hash("a" * 64)
    track_id = await repo.add_track(
        source_chat_id=10,
        source_message_id=20,
        content_sha256="a" * 64,
        title="A",
        performer="B",
        file_name="a.mp3",
        mime_type="audio/mpeg",
        file_size=3,
    )
    assert await repo.has_track_hash("a" * 64)
    await repo.mark_success(delivery.id, track_id, 777)
    saved = await repo.get_delivery(delivery.id)
    assert saved.status == "succeeded"
    assert saved.destination_message_id == 777
    await database.dispose()


@pytest.mark.asyncio
async def test_repository_returns_used_source_message_ids(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    for source_id, digest in [(71, "a" * 64), (72, "b" * 64)]:
        await repo.add_track(
            source_chat_id=-1,
            source_message_id=source_id,
            content_sha256=digest,
            title=None,
            performer=None,
            file_name="track.mp3",
            mime_type="audio/mpeg",
            file_size=3,
        )

    assert await repo.get_used_source_message_ids() == frozenset({71, 72})
    await database.dispose()


@pytest.mark.asyncio
async def test_hash_claim_is_atomic_and_associates_delivery_before_publish(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    when = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)
    first = await repo.reserve_delivery("rock", when)
    second = await repo.reserve_delivery("jazz", when)
    assert first is not None and second is not None
    kwargs = dict(
        source_chat_id=10,
        source_message_id=20,
        content_sha256="b" * 64,
        title=None,
        performer=None,
        file_name="x.mp3",
        mime_type="audio/mpeg",
        file_size=3,
    )
    first_track = await repo.claim_track_for_publish(first.id, **kwargs)
    second_track = await repo.claim_track_for_publish(second.id, **kwargs)
    assert first_track is not None
    assert second_track is None
    saved = await repo.get_delivery(first.id)
    assert saved.track_id == first_track
    assert saved.status == "publishing"
    await database.dispose()


@pytest.mark.asyncio
async def test_startup_recovery_marks_nonterminal_deliveries(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    when = datetime(2026, 9, 22, 5, 30, tzinfo=UTC)
    queued = await repo.reserve_delivery("rock", when)
    publishing = await repo.reserve_delivery("jazz", when)
    assert queued is not None and publishing is not None
    await repo.claim_track_for_publish(
        publishing.id,
        source_chat_id=1,
        source_message_id=2,
        content_sha256="c" * 64,
        title=None,
        performer=None,
        file_name=None,
        mime_type="audio/mpeg",
        file_size=3,
    )
    await repo.recover_nonterminal()
    assert (await repo.get_delivery(queued.id)).status == "interrupted"
    assert (await repo.get_delivery(publishing.id)).status == "ambiguous"
    await database.dispose()


@pytest.mark.asyncio
async def test_failure_details_are_classified_not_raw_exception_text(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_schema()
    repo = Repository(database.session_factory)
    delivery = await repo.reserve_delivery("rock", datetime(2026, 9, 25, tzinfo=UTC))
    assert delivery is not None
    await repo.mark_failed(delivery.id, "https://secret.invalid token=raw")
    saved = await repo.get_delivery(delivery.id)
    assert saved.error_detail == "internal_error"
    await database.dispose()
