"""Public Chromium SoundCloud discovery with an atomic local catalog cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote_plus

from music_bridge.domain import MusicSource, SourceMedia, SourceRequest
from music_bridge.source.soundcloud_source import (
    SoundCloudDownloadError,
    SoundCloudValidationError,
    canonical_track_url,
    is_url_like,
    source_message_id_for_url,
)

_CACHE_VERSION = 1
_METRIC_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([KMB]?)", re.IGNORECASE)
_AGE_RE = re.compile(
    r"(?:about\s+)?(?:an?|one|([0-9]+))\s+"
    r"(minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_AGE_SECONDS = {
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
    "year": 31_536_000,
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogCandidate:
    query: str
    url: str
    title: str | None
    uploader: str | None
    age_seconds: int | None
    plays: int
    likes: int
    reposts: int
    comments: int
    collected_at: datetime


class CatalogCollector(Protocol):
    async def collect(self, queries: list[str]) -> list[CatalogCandidate]: ...


def _metric(value: object) -> int:
    text = str(value or "").replace(",", "").strip()
    match = _METRIC_RE.search(text)
    if match is None:
        return 0
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    return max(0, int(number * multiplier))


def _relative_age_seconds(value: object) -> int | None:
    text = str(value or "").strip()
    match = _AGE_RE.search(text)
    if match is None:
        return None
    amount = int(match.group(1) or 1)
    return amount * _AGE_SECONDS[match.group(2).casefold()]


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def normalize_search_cards(
    query: str, cards: Sequence[Mapping[str, object]], collected_at: datetime | None = None
) -> list[CatalogCandidate]:
    """Convert browser-extracted result cards into safe deduplicated catalog rows."""
    timestamp = (collected_at or datetime.now(UTC)).astimezone(UTC)
    seen: set[str] = set()
    candidates: list[CatalogCandidate] = []
    for card in cards:
        raw_url = card.get("url")
        if not isinstance(raw_url, str):
            continue
        if raw_url.startswith("/"):
            raw_url = f"https://soundcloud.com{raw_url}"
        url = canonical_track_url(raw_url)
        if url is None or url in seen:
            continue
        seen.add(url)
        candidates.append(
            CatalogCandidate(
                query=query,
                url=url,
                title=_optional_text(card.get("title")),
                uploader=_optional_text(card.get("uploader")),
                age_seconds=_relative_age_seconds(card.get("age")),
                plays=_metric(card.get("plays")),
                likes=_metric(card.get("likes")),
                reposts=_metric(card.get("reposts")),
                comments=_metric(card.get("comments")),
                collected_at=timestamp,
            )
        )
    return candidates


def _rank(candidate: CatalogCandidate) -> float:
    popularity = (
        candidate.plays + candidate.likes * 40 + candidate.reposts * 200 + candidate.comments * 20
    )
    age_days = (candidate.age_seconds or 180 * 86_400) / 86_400
    return popularity / (1.0 + age_days / 14.0)


class CatalogCache:
    """Versioned JSON cache written by replace(2) in its destination directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _payload(self) -> dict[str, object]:
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SoundCloudValidationError("catalog_cache_unavailable") from None
        if not isinstance(raw, dict) or raw.get("version") != _CACHE_VERSION:
            raise SoundCloudValidationError("invalid_catalog_cache")
        if not isinstance(raw.get("candidates"), list) or not isinstance(raw.get("rejected"), dict):
            raise SoundCloudValidationError("invalid_catalog_cache")
        return cast(dict[str, object], raw)

    @staticmethod
    def _decode_candidate(raw: object) -> CatalogCandidate:
        if not isinstance(raw, dict):
            raise SoundCloudValidationError("invalid_catalog_cache")
        try:
            url = canonical_track_url(str(raw["url"]))
            if url is None:
                raise ValueError
            collected_at = datetime.fromisoformat(str(raw["collected_at"])).astimezone(UTC)
            return CatalogCandidate(
                query=str(raw["query"]),
                url=url,
                title=_optional_text(raw.get("title")),
                uploader=_optional_text(raw.get("uploader")),
                age_seconds=(
                    int(raw["age_seconds"]) if raw.get("age_seconds") is not None else None
                ),
                plays=max(0, int(raw["plays"])),
                likes=max(0, int(raw["likes"])),
                reposts=max(0, int(raw["reposts"])),
                comments=max(0, int(raw["comments"])),
                collected_at=collected_at,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise SoundCloudValidationError("invalid_catalog_cache") from None

    def load(self) -> list[CatalogCandidate]:
        return [
            self._decode_candidate(item)
            for item in cast(list[object], self._payload()["candidates"])
        ]

    def age_seconds(self) -> int:
        payload = self._payload()
        try:
            generated_at = datetime.fromisoformat(str(payload["generated_at"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            raise SoundCloudValidationError("invalid_catalog_cache") from None
        return max(0, int((datetime.now(UTC) - generated_at).total_seconds()))

    def _atomic_write(self, payload: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    def save(self, candidates: Sequence[CatalogCandidate]) -> None:
        rejected: dict[str, object] = {}
        if self.path.exists():
            try:
                rejected = cast(dict[str, object], self._payload()["rejected"])
            except SoundCloudValidationError:
                pass
        encoded: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            if canonical_track_url(candidate.url) != candidate.url:
                continue
            identity = (candidate.query, candidate.url)
            if identity in seen:
                continue
            seen.add(identity)
            item = asdict(candidate)
            item["collected_at"] = candidate.collected_at.astimezone(UTC).isoformat()
            encoded.append(item)
        self._atomic_write(
            {
                "version": _CACHE_VERSION,
                "generated_at": datetime.now(UTC).isoformat(),
                "candidates": encoded,
                "rejected": rejected,
            }
        )

    def reject(self, url: str, category: str) -> None:
        payload = self._payload()
        rejected = cast(dict[str, object], payload["rejected"])
        rejected[url] = {"category": category, "at": datetime.now(UTC).isoformat()}
        self._atomic_write(payload)

    def select(
        self, query: str, excluded_source_ids: frozenset[int], *, limit: int
    ) -> list[CatalogCandidate]:
        payload = self._payload()
        rejected = cast(dict[str, object], payload["rejected"])
        available = [
            self._decode_candidate(item) for item in cast(list[object], payload["candidates"])
        ]
        available = [
            candidate
            for candidate in available
            if candidate.query == query
            and candidate.url not in rejected
            and source_message_id_for_url(candidate.url) not in excluded_source_ids
        ]
        return sorted(available, key=lambda item: (_rank(item), item.url), reverse=True)[:limit]

    def select_pool(
        self, queries: Sequence[str], excluded_source_ids: frozenset[int], *, limit: int
    ) -> list[CatalogCandidate]:
        query_set = set(queries)
        payload = self._payload()
        rejected = cast(dict[str, object], payload["rejected"])
        available = [
            self._decode_candidate(item) for item in cast(list[object], payload["candidates"])
        ]
        available = [
            candidate
            for candidate in available
            if candidate.query in query_set
            and candidate.url not in rejected
            and source_message_id_for_url(candidate.url) not in excluded_source_ids
        ]
        return sorted(available, key=lambda item: (_rank(item), item.url), reverse=True)[:limit]

    def capacity_counts(
        self, queries: Sequence[str], excluded_source_ids: frozenset[int]
    ) -> dict[str, int]:
        candidates = self.select_pool(queries, excluded_source_ids, limit=2**31 - 1)
        counts = Counter(candidate.query for candidate in candidates)
        return {query: counts[query] for query in dict.fromkeys(queries)}

    def require_capacity(
        self, queries: Sequence[str], excluded_source_ids: frozenset[int]
    ) -> dict[str, int]:
        counts = self.capacity_counts(queries, excluded_source_ids)
        if any(count < 1 for count in counts.values()):
            raise SoundCloudDownloadError("catalog_query_capacity_missing")
        return counts


class PublicChromiumSoundCloudCollector:
    """Search public SoundCloud pages in an ephemeral unauthenticated browser."""

    def __init__(
        self,
        *,
        chromium_executable: Path,
        page_timeout_seconds: int,
        result_limit: int = 50,
    ) -> None:
        self._chromium_executable = chromium_executable
        self._page_timeout_ms = page_timeout_seconds * 1_000
        self._result_limit = result_limit

    async def collect(self, queries: list[str]) -> list[CatalogCandidate]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise SoundCloudDownloadError("playwright_unavailable") from None

        collected: list[CatalogCandidate] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=str(self._chromium_executable),
                headless=True,
                chromium_sandbox=True,
                args=["--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(storage_state=None)
                page = await context.new_page()
                page.set_default_timeout(self._page_timeout_ms)
                for query in queries:
                    await page.goto(
                        f"https://soundcloud.com/search/sounds?q={quote_plus(query)}",
                        wait_until="domcontentloaded",
                        timeout=self._page_timeout_ms,
                    )
                    await page.locator("li.searchList__item").first.wait_for(
                        timeout=self._page_timeout_ms
                    )
                    card_locator = page.locator("li.searchList__item")
                    previous_count = 0
                    unchanged = 0
                    for _ in range(10):
                        count = await card_locator.count()
                        if count >= self._result_limit or unchanged >= 2:
                            break
                        unchanged = unchanged + 1 if count == previous_count else 0
                        previous_count = count
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(300)
                    raw_cards = await card_locator.evaluate_all(
                        """(cards, limit) => cards.slice(0, limit).map(card => {
                          const text = selector =>
                            card.querySelector(selector)?.textContent?.trim() || '';
                          const title = card.querySelector('a.soundTitle__title');
                          const metric = name => {
                            const nodes = [...card.querySelectorAll('.sc-ministats-item')];
                            const node = nodes.find(item =>
                              ((item.getAttribute('title') || '') + ' ' + item.textContent)
                                .toLowerCase().includes(name));
                            return node?.getAttribute('title') || node?.textContent?.trim() || '';
                          };
                          return {
                            url: title?.href || '', title: title?.textContent?.trim() || '',
                            uploader: text('a.soundTitle__username'),
                            age: text('.soundTitle__uploadTime'), plays: metric('play'),
                            likes: metric('like'), reposts: metric('repost'),
                            comments: metric('comment')
                          };
                        })""",
                        self._result_limit,
                    )
                    if not isinstance(raw_cards, list):
                        continue
                    mappings = [item for item in raw_cards if isinstance(item, dict)]
                    collected.extend(normalize_search_cards(query, mappings))
                await context.close()
            finally:
                await browser.close()
        return collected


class SoundCloudCatalogManager:
    def __init__(
        self,
        cache: CatalogCache,
        collector: CatalogCollector,
        queries: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> None:
        self._cache = cache
        self._collector = collector
        self._queries = list(dict.fromkeys(queries))
        self._timeout = timeout_seconds

    async def refresh_or_load(self) -> list[CatalogCandidate]:
        try:
            candidates = await asyncio.wait_for(
                self._collector.collect(self._queries), timeout=self._timeout
            )
            if not candidates:
                raise SoundCloudValidationError("empty_catalog_refresh")
            refreshed_queries = {candidate.query for candidate in candidates}
            retained: list[CatalogCandidate] = []
            try:
                retained = [
                    candidate
                    for candidate in self._cache.load()
                    if candidate.query in self._queries and candidate.query not in refreshed_queries
                ]
            except SoundCloudValidationError:
                pass
            merged = [
                candidate for candidate in candidates if candidate.query in self._queries
            ] + retained
            if not merged:
                raise SoundCloudValidationError("empty_catalog_refresh")
            self._cache.save(merged)
            return merged
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cached = self._cache.load()
            logger.warning(
                "catalog_refresh_failed_using_cache (%s, cache_age_seconds=%d)",
                type(exc).__name__,
                self._cache.age_seconds(),
            )
            return cached

    async def run_periodic(self, interval_seconds: int, shutdown: asyncio.Event) -> None:
        while True:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                try:
                    await self.refresh_or_load()
                except Exception as exc:
                    logger.warning("catalog refresh failed (%s)", type(exc).__name__)


class CatalogMusicSource:
    """Select unused catalog URLs and fall back across failed direct downloads."""

    def __init__(
        self, cache: CatalogCache, direct: MusicSource, *, candidate_attempts: int
    ) -> None:
        self._cache = cache
        self._direct = direct
        self._candidate_attempts = candidate_attempts

    async def request_track(self, request: SourceRequest) -> SourceMedia:
        value = request.query.strip()
        canonical = canonical_track_url(value)
        if canonical is not None:
            return await self._direct.request_track(
                SourceRequest(
                    canonical,
                    request.excluded_source_message_ids,
                    request.catalog_queries,
                )
            )
        if is_url_like(value):
            raise SoundCloudValidationError("unsupported_url")
        candidates = self._cache.select_pool(
            request.catalog_queries or (request.query,),
            request.excluded_source_message_ids,
            limit=self._candidate_attempts,
        )
        for candidate in candidates:
            try:
                return await self._direct.request_track(
                    SourceRequest(candidate.url, request.excluded_source_message_ids)
                )
            except asyncio.CancelledError:
                raise
            except SoundCloudValidationError as exc:
                if str(exc) != "media_already_prepared":
                    self._cache.reject(candidate.url, str(exc))
            except SoundCloudDownloadError:
                continue
        raise SoundCloudDownloadError("catalog_candidates_exhausted")

    def iter_download(self, media: SourceMedia) -> AsyncIterator[bytes]:
        return self._direct.iter_download(media)

    async def discard(self, media: SourceMedia) -> None:
        await self._direct.discard(media)
