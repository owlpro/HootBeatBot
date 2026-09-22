import asyncio
from pathlib import Path

import pytest

from music_bridge.domain import MediaKind, SourceRequest
from music_bridge.source.playwright_source import (
    PlaywrightMusicSource,
    PlaywrightTelegramWebDriver,
    SourceDownloadError,
    SourceSelectorError,
    SourceTimeout,
    WebMessage,
)


class FakeDriver:
    def __init__(self, messages: list[WebMessage] | None = None) -> None:
        self.messages = messages or []
        self.events: list[object] = []
        self.active = 0
        self.max_active = 0

    async def open_chat(self, web_url: str, username: str) -> None:
        self.events.append(("open", web_url, username))

    async def snapshot_message_ids(self) -> set[str]:
        self.events.append("snapshot")
        return {"old"}

    async def send_query(self, query: str) -> str:
        self.events.append(("send", query))
        return f"out-{query}"

    async def wait_for_new_media(
        self, outbound_message_id: str, excluded: set[str], timeout_seconds: float
    ) -> WebMessage:
        self.events.append(("wait", outbound_message_id, excluded, timeout_seconds))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if not self.messages:
            raise SourceTimeout("source_response_timeout")
        return self.messages.pop(0)

    async def download_to(self, message_id: str, destination: Path, max_bytes: int) -> None:
        self.events.append(("download", message_id))
        assert max_bytes == 1_000_000
        destination.write_bytes(b"abc" * 200_000)


def web_message(message_id: str = "new", **changes: object) -> WebMessage:
    values: dict[str, object] = {
        "message_id": message_id,
        "source_chat_id": 55,
        "kind": MediaKind.AUDIO,
        "file_name": "song.mp3",
        "mime_type": "audio/mpeg",
        "size": 600_000,
        "title": "Song",
        "performer": "Artist",
    }
    values.update(changes)
    return WebMessage(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_source_snapshots_before_send_and_accepts_only_new_media() -> None:
    driver = FakeDriver([web_message()])
    source = PlaywrightMusicSource(
        driver,
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=12,
        max_media_bytes=1_000_000,
    )

    result = await source.request_track(SourceRequest("random rock"))

    assert result.source_message_id > 0
    assert driver.events == [
        ("open", "https://web.telegram.org/k/", "@melobot"),
        "snapshot",
        ("send", "random rock"),
        ("wait", "out-random rock", {"old"}, 12),
    ]


@pytest.mark.asyncio
async def test_source_rejects_baseline_message_returned_by_driver() -> None:
    source = PlaywrightMusicSource(
        FakeDriver([web_message("old")]),
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
    )
    with pytest.raises(SourceTimeout, match="new_media"):
        await source.request_track(SourceRequest("rock"))


@pytest.mark.asyncio
async def test_source_serializes_requests(tmp_path: Path) -> None:
    driver = FakeDriver([web_message("new-1"), web_message("new-2")])
    source = PlaywrightMusicSource(
        driver,
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
        download_root=tmp_path,
    )

    async def fetch(query: str) -> None:
        media = await source.request_track(SourceRequest(query))
        _ = [chunk async for chunk in source.iter_download(media)]

    await asyncio.gather(fetch("one"), fetch("two"))
    assert driver.max_active == 1


@pytest.mark.asyncio
async def test_source_holds_session_lock_until_download_finishes(tmp_path: Path) -> None:
    first_download_started = asyncio.Event()
    release_first_download = asyncio.Event()

    class BlockingDriver(FakeDriver):
        async def download_to(self, message_id: str, destination: Path, max_bytes: int) -> None:
            assert max_bytes == 1_000_000
            if message_id == "new-1":
                first_download_started.set()
                await release_first_download.wait()
            destination.write_bytes(b"audio")

    driver = BlockingDriver([web_message("new-1"), web_message("new-2")])
    source = PlaywrightMusicSource(
        driver,
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
        download_root=tmp_path,
    )

    async def fetch(query: str) -> bytes:
        media = await source.request_track(SourceRequest(query))
        return b"".join([chunk async for chunk in source.iter_download(media)])

    first = asyncio.create_task(fetch("one"))
    await first_download_started.wait()
    second = asyncio.create_task(fetch("two"))
    await asyncio.sleep(0)
    assert [
        event for event in driver.events if isinstance(event, tuple) and event[0] == "open"
    ] == [("open", "https://web.telegram.org/k/", "@melobot")]
    release_first_download.set()
    assert await asyncio.gather(first, second) == [b"audio", b"audio"]


@pytest.mark.asyncio
async def test_request_failure_releases_source_session_lock(tmp_path: Path) -> None:
    class FailsOnceDriver(FakeDriver):
        attempts = 0

        async def wait_for_new_media(
            self, outbound_message_id: str, excluded: set[str], timeout_seconds: float
        ) -> WebMessage:
            self.attempts += 1
            if self.attempts == 1:
                raise SourceTimeout("source_response_timeout")
            return await super().wait_for_new_media(outbound_message_id, excluded, timeout_seconds)

    driver = FailsOnceDriver([web_message("new")])
    source = PlaywrightMusicSource(
        driver,
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
        download_root=tmp_path,
    )
    with pytest.raises(SourceTimeout):
        await source.request_track(SourceRequest("first"))
    media = await asyncio.wait_for(source.request_track(SourceRequest("second")), timeout=1)
    assert b"".join([chunk async for chunk in source.iter_download(media)])


@pytest.mark.asyncio
async def test_abandoned_media_handle_releases_source_session(tmp_path: Path) -> None:
    driver = FakeDriver([web_message("abandoned"), web_message("next")])
    source = PlaywrightMusicSource(
        driver,
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
        download_root=tmp_path,
    )

    async def abandon() -> None:
        await source.request_track(SourceRequest("first"))

    await asyncio.create_task(abandon())
    await asyncio.sleep(0)
    media = await asyncio.wait_for(source.request_track(SourceRequest("second")), timeout=1)
    assert b"".join([chunk async for chunk in source.iter_download(media)])


@pytest.mark.asyncio
async def test_download_streams_from_disk_without_reading_whole_file(tmp_path: Path) -> None:
    driver = FakeDriver([web_message()])
    source = PlaywrightMusicSource(
        driver,
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
        download_root=tmp_path,
        chunk_size=256_000,
    )
    media = await source.request_track(SourceRequest("rock"))
    chunks = [chunk async for chunk in source.iter_download(media)]
    assert list(map(len, chunks)) == [256_000, 256_000, 88_000]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_download_failure_is_safely_classified(tmp_path: Path) -> None:
    class BrokenDriver(FakeDriver):
        async def download_to(self, message_id: str, destination: Path, max_bytes: int) -> None:
            del message_id, destination, max_bytes
            raise RuntimeError("secret DOM details")

    source = PlaywrightMusicSource(
        BrokenDriver([web_message()]),
        "https://web.telegram.org/k/",
        "@melobot",
        timeout_seconds=1,
        max_media_bytes=1_000_000,
        download_root=tmp_path,
    )
    media = await source.request_track(SourceRequest("rock"))
    with pytest.raises(SourceDownloadError, match="source_download_failed"):
        _ = [chunk async for chunk in source.iter_download(media)]


@pytest.mark.asyncio
async def test_driver_cancels_oversized_streaming_download_and_removes_partials(
    tmp_path: Path,
) -> None:
    class FakeDownload:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()
            self.writer: asyncio.Task[None] | None = None
            self.path_value: Path | None = None
            self.writes = 0

        async def path(self) -> Path:
            assert self.writer is not None
            await self.writer
            assert self.path_value is not None
            return self.path_value

        async def cancel(self) -> None:
            self.cancelled.set()

    download = FakeDownload()

    class Session:
        async def send(self, method: str, params: dict[str, object]) -> None:
            assert method == "Browser.setDownloadBehavior"
            directory = Path(str(params["downloadPath"]))
            download.path_value = directory / "stream.part"

            async def write() -> None:
                assert download.path_value is not None
                with download.path_value.open("wb") as output:
                    for _ in range(100):
                        if download.cancelled.is_set():
                            return
                        output.write(b"x" * 128)
                        download.writes += 1
                        output.flush()
                        await asyncio.sleep(0.005)

            download.writer = asyncio.create_task(write())

    class Context:
        async def new_cdp_session(self, page: object) -> Session:
            del page
            return Session()

    class Pending:
        async def __aenter__(self) -> "Pending":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        @property
        async def value(self) -> FakeDownload:
            return download

    class Locator:
        @property
        def first(self) -> "Locator":
            return self

        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

        def locator(self, selector: str) -> "Locator":
            del selector
            return self

        async def click(self) -> None:
            return None

    class Page:
        def locator(self, selector: str) -> Locator:
            del selector
            return Locator()

        def expect_download(self) -> Pending:
            return Pending()

    driver = PlaywrightTelegramWebDriver(object(), Context(), Page())
    destination = tmp_path / "complete"
    with pytest.raises(SourceDownloadError, match="source_media_too_large"):
        await driver.download_to("42", destination, max_bytes=512)
    assert download.cancelled.is_set()
    assert download.writes < 100
    assert not destination.exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_open_chat_uses_exact_scoped_username_and_verifies_header() -> None:
    class Locator:
        def __init__(self, page: "Page", kind: str, index: int = 0) -> None:
            self.page, self.kind, self.index = page, kind, index

        @property
        def first(self) -> "Locator":
            return self

        def nth(self, index: int) -> "Locator":
            return Locator(self.page, "result", index)

        async def count(self) -> int:
            if self.kind == "results":
                return 2
            return 0 if self.kind == "empty" else 1

        async def is_visible(self) -> bool:
            return True

        async def fill(self, value: str) -> None:
            self.page.search_value = value

        async def get_attribute(self, name: str) -> str | None:
            if name != "data-username":
                return None
            if self.kind == "result":
                return ["@melobot_fan", "melobot"][self.index]
            if self.kind == "header":
                return self.page.header_username
            return None

        def locator(self, selector: str) -> "Locator":
            if self.kind == "container":
                return Locator(self.page, "results")
            return Locator(self.page, "empty")

        async def click(self) -> None:
            assert self.kind == "result"
            self.page.clicked = self.index

    class Page:
        search_value = ""
        clicked: int | None = None
        header_username = "@MeLoBoT"

        async def goto(self, url: str, *, wait_until: str) -> None:
            del url, wait_until

        def locator(self, selector: str) -> Locator:
            if selector in TelegramWebSelectors.LOGIN_MARKERS:
                return Locator(self, "empty")
            if selector in TelegramWebSelectors.SEARCH:
                return Locator(self, "search")
            if selector in TelegramWebSelectors.SEARCH_RESULTS:
                return Locator(self, "container")
            if selector in TelegramWebSelectors.CHAT_HEADER:
                return Locator(self, "header")
            return Locator(self, "empty")

    from music_bridge.source.playwright_source import TelegramWebSelectors

    page = Page()
    driver = PlaywrightTelegramWebDriver(object(), object(), page)
    await driver.open_chat("https://web.telegram.org/k/", "@melobot")
    assert page.search_value == "@melobot"
    assert page.clicked == 1

    page.header_username = "@not_melobot"
    with pytest.raises(SourceSelectorError, match="identity_mismatch"):
        await driver.open_chat("https://web.telegram.org/k/", "@melobot")


@pytest.mark.asyncio
async def test_driver_returns_exact_outbound_query_message_id() -> None:
    class Row:
        async def text_content(self) -> str:
            return "random rock"

        async def get_attribute(self, name: str) -> str | None:
            return "true" if name == "data-is-outgoing" else None

        def locator(self, selector: str) -> "Row":
            del selector
            return self

        async def count(self) -> int:
            return 0

    class Composer:
        async def fill(self, value: str) -> None:
            assert value == "random rock"

        async def press(self, key: str) -> None:
            assert key == "Enter"

    class Driver(PlaywrightTelegramWebDriver):
        async def snapshot_message_ids(self) -> set[str]:
            return {"before"}

        async def _first_visible(self, selectors, *, within=None):
            del selectors, within
            return Composer()

        async def _ordered_message_rows(self):
            return [("before", Row()), ("outbound-42", Row())]

    driver = Driver(object(), object(), object())
    assert await driver.send_query("random rock") == "outbound-42"


@pytest.mark.asyncio
async def test_driver_reads_mixed_message_selectors_in_dom_order() -> None:
    class Row:
        def __init__(self, message_id: str) -> None:
            self.message_id = message_id

        async def get_attribute(self, name: str) -> str | None:
            if name in {"data-message-id", "id"}:
                return self.message_id
            return None

    class Rows:
        def __init__(self, rows: list[Row]) -> None:
            self.rows = rows

        async def count(self) -> int:
            return len(self.rows)

        def nth(self, index: int) -> Row:
            return self.rows[index]

    class Page:
        def locator(self, selector: str) -> Rows:
            if ", " in selector:
                return Rows([Row("old"), Row("outbound")])
            if selector == TelegramWebSelectors.MESSAGE[0]:
                return Rows([Row("outbound")])
            if selector == TelegramWebSelectors.MESSAGE[2]:
                return Rows([Row("old")])
            return Rows([])

    from music_bridge.source.playwright_source import TelegramWebSelectors

    rows = await PlaywrightTelegramWebDriver(object(), object(), Page())._ordered_message_rows()
    assert [message_id for message_id, _row in rows] == ["old", "outbound"]


@pytest.mark.asyncio
async def test_driver_skips_media_observed_by_cancelled_request() -> None:
    scanned = asyncio.Event()

    class Row:
        def __init__(self, reply_id: str | None = None) -> None:
            self.reply_id = reply_id

        async def get_attribute(self, name: str) -> str | None:
            if name == "data-reply-to-message-id":
                return self.reply_id
            values = {
                "data-mime-type": "audio/mpeg",
                "data-file-name": "song.mp3",
                "data-file-size": "5",
            }
            return values.get(name)

        def locator(self, selector: str) -> "MediaMarker":
            return MediaMarker(selector)

    class MediaMarker:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def count(self) -> int:
            return int(self.selector == "audio")

    class Driver(PlaywrightTelegramWebDriver):
        scan = 0

        async def _ordered_message_rows(self):
            self.scan += 1
            scanned.set()
            if self.scan == 1:
                return [("out-1", Row()), ("stale", Row("older-query"))]
            return [
                ("out-1", Row()),
                ("out-2", Row()),
                ("stale", Row()),
                ("fresh", Row("out-2")),
            ]

    driver = Driver(object(), object(), object())
    first = asyncio.create_task(driver.wait_for_new_media("out-1", set(), 10))
    await scanned.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    result = await driver.wait_for_new_media("out-2", set(), 1)
    assert result.message_id == "fresh"


@pytest.mark.asyncio
async def test_driver_skips_media_observed_by_timed_out_request(monkeypatch) -> None:
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        del delay
        await original_sleep(0)

    monkeypatch.setattr("music_bridge.source.playwright_source.asyncio.sleep", fast_sleep)

    class Row:
        def __init__(self, reply_id: str | None = None) -> None:
            self.reply_id = reply_id

        async def get_attribute(self, name: str) -> str | None:
            if name == "data-reply-to-message-id":
                return self.reply_id
            return {"data-mime-type": "audio/mpeg", "data-file-size": "5"}.get(name)

        def locator(self, selector: str) -> "Marker":
            return Marker(selector)

    class Marker:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def count(self) -> int:
            return int(self.selector == "audio")

    class Driver(PlaywrightTelegramWebDriver):
        phase = 1

        async def _ordered_message_rows(self):
            if self.phase == 1:
                return [("out-1", Row()), ("stale", Row("older-query"))]
            return [("out-2", Row()), ("stale", Row()), ("fresh", Row("out-2"))]

    driver = Driver(object(), object(), object())
    with pytest.raises(SourceTimeout):
        await driver.wait_for_new_media("out-1", set(), 0.001)
    driver.phase = 2
    assert (await driver.wait_for_new_media("out-2", set(), 1)).message_id == "fresh"


@pytest.mark.asyncio
async def test_driver_prefers_explicit_reply_over_unassociated_media() -> None:
    class Row:
        def __init__(self, reply_id: str | None = None) -> None:
            self.reply_id = reply_id

        async def get_attribute(self, name: str) -> str | None:
            if name == "data-reply-to-message-id":
                return self.reply_id
            return {"data-mime-type": "audio/mpeg", "data-file-size": "5"}.get(name)

        def locator(self, selector: str) -> "Marker":
            return Marker(selector)

    class Marker:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def count(self) -> int:
            return int(self.selector == "audio")

    class Driver(PlaywrightTelegramWebDriver):
        async def _ordered_message_rows(self):
            return [
                ("outbound", Row()),
                ("unassociated", Row()),
                ("reply", Row("outbound")),
            ]

    result = await Driver(object(), object(), object()).wait_for_new_media("outbound", set(), 1)
    assert result.message_id == "reply"


@pytest.mark.asyncio
async def test_driver_requires_candidate_to_follow_outbound_message() -> None:
    class Row:
        async def get_attribute(self, name: str) -> str | None:
            values = {
                "data-mime-type": "audio/mpeg",
                "data-file-name": "song.mp3",
                "data-file-size": "5",
            }
            return values.get(name)

        def locator(self, selector: str) -> "Marker":
            return Marker(selector)

    class Marker:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def count(self) -> int:
            return int(self.selector == "audio")

    class Driver(PlaywrightTelegramWebDriver):
        async def _ordered_message_rows(self):
            return [("delayed-old", Row()), ("outbound", Row()), ("fresh", Row())]

    result = await Driver(object(), object(), object()).wait_for_new_media("outbound", set(), 1)
    assert result.message_id == "fresh"
