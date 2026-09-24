import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from music_bridge.domain import MediaKind, SourceMedia, SourceRequest
from music_bridge.source.soundcloud_catalog import (
    CatalogCache,
    CatalogCandidate,
    CatalogMusicSource,
    SoundCloudCatalogManager,
    normalize_search_cards,
)
from music_bridge.source.soundcloud_source import (
    SoundCloudDownloadError,
    SoundCloudValidationError,
    source_message_id_for_url,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def candidate(
    slug: str,
    *,
    query: str = "rock",
    age_seconds: int = 86_400,
    plays: int = 1_000,
    likes: int = 10,
) -> CatalogCandidate:
    return CatalogCandidate(
        query=query,
        url=f"https://soundcloud.com/artist/{slug}",
        title=slug,
        uploader="artist",
        age_seconds=age_seconds,
        plays=plays,
        likes=likes,
        reposts=1,
        comments=2,
        collected_at=NOW,
    )


def test_normalize_search_cards_validates_deduplicates_and_parses_metrics() -> None:
    cards = [
        {
            "url": "/artist/track?utm_source=x",
            "title": " Track ",
            "uploader": " Artist ",
            "age": "2 days ago",
            "plays": "1.2K plays",
            "likes": "34",
            "reposts": "5",
            "comments": "6",
        },
        {"url": "https://soundcloud.com/artist/track", "title": "duplicate"},
        {"url": "https://evil.example/artist/track", "title": "unsafe"},
        {"url": "https://soundcloud.com/artist", "title": "profile"},
    ]

    parsed = normalize_search_cards("rock", cards, NOW)

    assert parsed == [
        CatalogCandidate(
            query="rock",
            url="https://soundcloud.com/artist/track",
            title="Track",
            uploader="Artist",
            age_seconds=172_800,
            plays=1_200,
            likes=34,
            reposts=5,
            comments=6,
            collected_at=NOW,
        )
    ]


def test_cache_is_atomic_and_loads_previous_cache_if_refresh_fails(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    cache = CatalogCache(path)
    original = candidate("cached")
    cache.save([original])

    class BrokenCollector:
        async def collect(self, queries: list[str]) -> list[CatalogCandidate]:
            del queries
            raise TimeoutError("public search unavailable")

    manager = SoundCloudCatalogManager(cache, BrokenCollector(), ["rock"], timeout_seconds=1)

    assert asyncio.run(manager.refresh_or_load()) == [original]
    assert not list(tmp_path.glob("*.tmp"))


def test_refresh_failure_warns_with_redacted_category_and_cache_age(tmp_path: Path, caplog) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    cache.save([candidate("cached")])

    class BrokenCollector:
        async def collect(self, queries: list[str]) -> list[CatalogCandidate]:
            del queries
            raise RuntimeError("secret URL https://example.invalid/token")

    manager = SoundCloudCatalogManager(cache, BrokenCollector(), ["rock"], timeout_seconds=1)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(manager.refresh_or_load())

    assert "catalog_refresh_failed_using_cache" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "cache_age_seconds=" in caplog.text
    assert "secret" not in caplog.text


def test_partial_refresh_preserves_candidates_for_missing_queries(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    old_rock = candidate("old-rock", query="rock")
    old_pop = candidate("old-pop", query="pop")
    cache.save([old_rock, old_pop])
    new_rock = candidate("new-rock", query="rock")

    class PartialCollector:
        async def collect(self, queries: list[str]) -> list[CatalogCandidate]:
            assert queries == ["rock", "pop"]
            return [new_rock]

    manager = SoundCloudCatalogManager(
        cache, PartialCollector(), ["rock", "pop"], timeout_seconds=1
    )

    assert asyncio.run(manager.refresh_or_load()) == [new_rock, old_pop]
    assert cache.load() == [new_rock, old_pop]


def test_capacity_counts_every_query_and_excludes_used_candidates(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    rock = candidate("rock", query="rock")
    pop = candidate("pop", query="pop")
    cache.save([rock, pop])

    assert cache.capacity_counts(
        ["rock", "pop"], frozenset({source_message_id_for_url(rock.url)})
    ) == {"rock": 0, "pop": 1}
    with pytest.raises(SoundCloudDownloadError, match="catalog_query_capacity_missing"):
        cache.require_capacity(["rock", "pop"], frozenset({source_message_id_for_url(rock.url)}))


def test_ranking_combines_popularity_and_recency(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    cache.save(
        [
            candidate("stale", age_seconds=365 * 86_400, plays=20_000, likes=100),
            candidate("fresh", age_seconds=86_400, plays=15_000, likes=90),
            candidate("viral", age_seconds=30 * 86_400, plays=2_000_000, likes=50_000),
        ]
    )

    selected = cache.select("rock", frozenset(), limit=3)

    assert [item.title for item in selected] == ["viral", "fresh", "stale"]


def test_cache_selection_excludes_used_and_rejected_urls(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    used, rejected, available = candidate("used"), candidate("rejected"), candidate("available")
    cache.save([used, rejected, available])
    cache.reject(rejected.url, "download_failed")

    selected = cache.select("rock", frozenset({source_message_id_for_url(used.url)}), limit=5)

    assert selected == [available]


class FakeDirectSource:
    def __init__(self, bad_urls: set[str]) -> None:
        self.bad_urls = bad_urls
        self.requests: list[str] = []
        self.request_objects: list[SourceRequest] = []
        self.discarded: list[SourceMedia] = []

    async def request_track(self, request: SourceRequest) -> SourceMedia:
        self.requests.append(request.query)
        self.request_objects.append(request)
        if request.query in self.bad_urls:
            raise SoundCloudValidationError("duration_mismatch")
        return SourceMedia(
            -1,
            source_message_id_for_url(request.query),
            MediaKind.AUDIO,
            "track.mp3",
            "audio/mpeg",
            999,
        )

    async def _chunks(self):
        yield b"audio"

    def iter_download(self, media: SourceMedia):
        del media
        return self._chunks()

    async def discard(self, media: SourceMedia) -> None:
        self.discarded.append(media)


@pytest.mark.asyncio
async def test_catalog_source_tries_bounded_fallback_and_records_rejection(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    first, second, third = (
        candidate("first", plays=30_000),
        candidate("second", plays=20_000),
        candidate("third", plays=10_000),
    )
    cache.save([first, second, third])
    direct = FakeDirectSource({first.url, second.url})
    source = CatalogMusicSource(cache, direct, candidate_attempts=2)

    with pytest.raises(SoundCloudDownloadError, match="catalog_candidates_exhausted"):
        await source.request_track(SourceRequest("rock"))

    assert direct.requests == [first.url, second.url]
    assert cache.select("rock", frozenset(), limit=5) == [third]


@pytest.mark.asyncio
async def test_transient_download_failure_is_not_permanently_rejected(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    first = candidate("first")
    cache.save([first])

    class TransientDirect(FakeDirectSource):
        async def request_track(self, request: SourceRequest) -> SourceMedia:
            self.requests.append(request.query)
            raise SoundCloudDownloadError("soundcloud_command_failed")

    direct = TransientDirect(set())
    source = CatalogMusicSource(cache, direct, candidate_attempts=1)

    with pytest.raises(SoundCloudDownloadError):
        await source.request_track(SourceRequest("rock"))

    assert cache.select("rock", frozenset(), limit=5) == [first]


@pytest.mark.asyncio
async def test_already_prepared_candidate_is_not_permanently_rejected(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    first = candidate("first")
    cache.save([first])

    class ConcurrentDirect(FakeDirectSource):
        async def request_track(self, request: SourceRequest) -> SourceMedia:
            self.requests.append(request.query)
            raise SoundCloudValidationError("media_already_prepared")

    source = CatalogMusicSource(cache, ConcurrentDirect(set()), candidate_attempts=1)

    with pytest.raises(SoundCloudDownloadError, match="catalog_candidates_exhausted"):
        await source.request_track(SourceRequest("rock"))

    assert cache.select("rock", frozenset(), limit=5) == [first]


@pytest.mark.asyncio
async def test_multi_query_topic_selects_from_one_ranked_pool(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    persian = candidate("persian", query="Persian remix", plays=10_000)
    international = candidate("international", query="international remix", plays=20_000)
    cache.save([persian, international])
    direct = FakeDirectSource(set())
    source = CatalogMusicSource(cache, direct, candidate_attempts=2)

    media = await source.request_track(
        SourceRequest("Persian remix", catalog_queries=("Persian remix", "international remix"))
    )

    assert media.source_message_id == source_message_id_for_url(international.url)


@pytest.mark.asyncio
async def test_catalog_source_preserves_direct_url_run_once(tmp_path: Path) -> None:
    direct = FakeDirectSource(set())
    source = CatalogMusicSource(
        CatalogCache(tmp_path / "missing.json"), direct, candidate_attempts=2
    )
    url = "https://soundcloud.com/artist/direct"

    media = await source.request_track(SourceRequest(url))

    assert media.source_message_id == source_message_id_for_url(url)
    assert direct.requests == [url]

    await source.discard(media)
    assert direct.discarded == [media]


@pytest.mark.asyncio
async def test_catalog_source_canonicalizes_direct_url_before_delegating(tmp_path: Path) -> None:
    direct = FakeDirectSource(set())
    source = CatalogMusicSource(
        CatalogCache(tmp_path / "missing.json"), direct, candidate_attempts=2
    )
    exclusions = frozenset({11, 22})

    await source.request_track(
        SourceRequest(
            " https://www.soundcloud.com/artist/direct/?utm_source=clipboard#fragment ",
            exclusions,
        )
    )

    assert direct.request_objects == [
        SourceRequest("https://soundcloud.com/artist/direct", exclusions)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "https://example.com/not-soundcloud",
        "https://soundcloud.com/artist",
    ],
)
async def test_url_like_invalid_query_fails_closed_with_multi_query_pool(
    tmp_path: Path, query: str
) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    pooled = candidate("pooled", query="valid pool query")
    cache.save([pooled])
    direct = FakeDirectSource(set())
    source = CatalogMusicSource(cache, direct, candidate_attempts=1)

    with pytest.raises(SoundCloudValidationError, match="unsupported_url"):
        await source.request_track(
            SourceRequest(query, catalog_queries=(query, "valid pool query"))
        )

    assert direct.requests == []


@pytest.mark.asyncio
async def test_catalog_source_treats_colon_search_text_as_catalog_query(tmp_path: Path) -> None:
    cache = CatalogCache(tmp_path / "catalog.json")
    result = candidate("house", query="genre: house")
    cache.save([result])
    direct = FakeDirectSource(set())
    source = CatalogMusicSource(cache, direct, candidate_attempts=1)

    media = await source.request_track(SourceRequest("genre: house"))

    assert media.source_message_id == source_message_id_for_url(result.url)
    assert direct.requests == [result.url]
