"""Telegram Web source adapter with an injectable browser-driver boundary.

Telegram Web has no supported automation DOM API. ``TelegramWebSelectors`` keeps
all fallbacks in one place; every selector must be verified against the staging
account after Telegram Web updates. None is claimed as live-verified here.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from music_bridge.domain import MediaKind, SourceMedia, SourceRequest


class SourceTimeout(TimeoutError):
    """No qualifying post-request media appeared before the deadline."""


class SourceSelectorError(RuntimeError):
    """Telegram Web no longer matches the staging selector strategy."""


class SourceAuthenticationRequired(RuntimeError):
    """The persistent browser profile is not logged in."""


class SourceDownloadError(RuntimeError):
    """The browser download failed without leaking DOM or account details."""


@dataclass(frozen=True, slots=True)
class WebMessage:
    message_id: str
    source_chat_id: int
    kind: MediaKind
    file_name: str | None
    mime_type: str | None
    size: int | None
    title: str | None = None
    performer: str | None = None


class TelegramWebDriver(Protocol):
    async def open_chat(self, web_url: str, username: str) -> None: ...

    async def snapshot_message_ids(self) -> set[str]: ...

    async def send_query(self, query: str) -> str: ...

    async def wait_for_new_media(
        self, outbound_message_id: str, excluded: set[str], timeout_seconds: float
    ) -> WebMessage: ...

    async def download_to(self, message_id: str, destination: Path, max_bytes: int) -> None: ...


class TelegramWebSelectors:
    """Centralized role/ARIA/data-attribute fallbacks; staging verification required."""

    LOGIN_MARKERS = (
        "canvas[aria-label*='QR']",
        "button[aria-label*='phone']",
        "input[name='phone_number']",
    )
    CHAT_LIST = ("[data-testid='chat-list']", "[role='list'][aria-label*='Chat']", ".chatlist")
    SEARCH = (
        "[data-testid='chat-search'] input",
        "input[aria-label*='Search']",
        "[role='searchbox']",
        "[contenteditable='true'][aria-label*='Search']",
    )
    SEARCH_RESULTS = (
        "[data-testid='search-results']",
        "[role='listbox'][aria-label*='Search']",
        ".search-results",
    )
    SEARCH_RESULT = ("[data-username]", "[role='option']", "[data-peer-id]")
    USERNAME = ("[data-username]", "[data-testid='username']", ".username")
    CHAT_HEADER = (
        "[data-testid='chat-header']",
        "header[aria-label*='Chat']",
        ".chat-info",
    )
    COMPOSER = (
        "[data-testid='message-input']",
        "[contenteditable='true'][aria-label*='Message']",
        "[contenteditable='true'][data-placeholder*='Message']",
    )
    MESSAGE = ("[data-message-id]", "[data-mid]", "[role='listitem'][id^='message']")
    MEDIA = (
        "[data-testid='audio-message']",
        "audio",
        "[data-testid='document-message']",
        "[aria-label*='audio']",
        "[aria-label*='file']",
    )
    DOWNLOAD = (
        "button[data-testid='download']",
        "button[aria-label*='Download']",
        "[role='button'][aria-label*='download']",
    )
    OUTGOING = ("[data-is-outgoing='true']", "[data-out='true']", ".is-outgoing", ".own")
    REPLY_ID_ATTRIBUTES = ("data-reply-to-message-id", "data-reply-to", "data-reply-mid")


class PlaywrightTelegramWebDriver:
    """Thin Playwright implementation; tests inject ``TelegramWebDriver`` fakes."""

    def __init__(self, playwright: Any, context: Any, page: Any) -> None:
        self._playwright = playwright
        self._context = context
        self._page = page
        self._chat_id = 0
        self._observed_message_ids: set[str] = set()

    @classmethod
    async def launch(
        cls, profile_path: Path, *, headless: bool = True
    ) -> PlaywrightTelegramWebDriver:
        from playwright.async_api import async_playwright

        profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_path.chmod(0o700)
        manager = async_playwright()
        playwright = await manager.start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_path),
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                accept_downloads=True,
            )
        except BaseException:
            await playwright.stop()
            raise
        page = context.pages[0] if context.pages else await context.new_page()
        return cls(playwright, context, page)

    async def close(self) -> None:
        try:
            await self._context.close()
        finally:
            await self._playwright.stop()

    async def _first_visible(self, selectors: tuple[str, ...], *, within: Any | None = None) -> Any:
        root = within or self._page
        for selector in selectors:
            locator = root.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        raise SourceSelectorError("telegram_web_selector_mismatch")

    async def _is_login_page(self) -> bool:
        for selector in TelegramWebSelectors.LOGIN_MARKERS:
            if await self._page.locator(selector).count():
                return True
        return False

    async def check_login(self, web_url: str) -> bool:
        await self._page.goto(web_url, wait_until="domcontentloaded")
        if await self._is_login_page():
            return False
        return any(
            [
                await self._page.locator(selector).count() > 0
                for selector in TelegramWebSelectors.CHAT_LIST
            ]
        )

    @staticmethod
    def _canonical_username(value: str) -> str:
        return value.strip().removeprefix("@").casefold()

    async def _username_from(self, locator: Any) -> str | None:
        value = await locator.get_attribute("data-username")
        if value:
            return self._canonical_username(value)
        for selector in TelegramWebSelectors.USERNAME:
            nested = locator.locator(selector).first
            if not await nested.count():
                continue
            value = await nested.get_attribute("data-username") or await nested.text_content()
            if value:
                return self._canonical_username(value)
        return None

    async def open_chat(self, web_url: str, username: str) -> None:
        await self._page.goto(web_url, wait_until="domcontentloaded")
        if await self._is_login_page():
            raise SourceAuthenticationRequired("telegram_web_login_required")
        search = await self._first_visible(TelegramWebSelectors.SEARCH)
        await search.fill(username)
        wanted = self._canonical_username(username)
        result = None
        for container_selector in TelegramWebSelectors.SEARCH_RESULTS:
            container = self._page.locator(container_selector).first
            if not await container.count() or not await container.is_visible():
                continue
            for result_selector in TelegramWebSelectors.SEARCH_RESULT:
                rows = container.locator(result_selector)
                for index in range(await rows.count()):
                    candidate = rows.nth(index)
                    if await self._username_from(candidate) == wanted:
                        result = candidate
                        break
                if result is not None:
                    break
            if result is not None:
                break
        if result is None:
            raise SourceSelectorError("telegram_web_chat_not_found")
        await result.click()
        header = await self._first_visible(TelegramWebSelectors.CHAT_HEADER)
        if await self._username_from(header) != wanted:
            raise SourceSelectorError("telegram_web_chat_identity_mismatch")
        digest = hashlib.sha256(wanted.encode()).digest()
        self._chat_id = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

    async def snapshot_message_ids(self) -> set[str]:
        result: set[str] = set()
        for selector in TelegramWebSelectors.MESSAGE:
            rows = self._page.locator(selector)
            for index in range(await rows.count()):
                row = rows.nth(index)
                value = (
                    await row.get_attribute("data-message-id")
                    or await row.get_attribute("data-mid")
                    or await row.get_attribute("id")
                )
                if value:
                    result.add(value)
        return result

    @staticmethod
    async def _message_id(row: Any) -> str | None:
        for attribute in ("data-message-id", "data-mid", "id"):
            value = await row.get_attribute(attribute)
            if isinstance(value, str) and value:
                return value
        return None

    async def _ordered_message_rows(self) -> list[tuple[str, Any]]:
        result: list[tuple[str, Any]] = []
        rows = self._page.locator(", ".join(TelegramWebSelectors.MESSAGE))
        for index in range(await rows.count()):
            row = rows.nth(index)
            message_id = await self._message_id(row)
            if message_id:
                result.append((message_id, row))
        return result

    async def send_query(self, query: str) -> str:
        baseline = await self.snapshot_message_ids()
        composer = await self._first_visible(TelegramWebSelectors.COMPOSER)
        await composer.fill(query)
        await composer.press("Enter")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            for message_id, row in reversed(await self._ordered_message_rows()):
                if message_id in baseline:
                    continue
                text = (await row.text_content() or "").strip()
                outgoing = False
                for selector in TelegramWebSelectors.OUTGOING:
                    if await row.locator(selector).count():
                        outgoing = True
                        break
                outgoing = outgoing or (await row.get_attribute("data-is-outgoing")) == "true"
                if outgoing and text == query:
                    self._observed_message_ids.add(message_id)
                    return message_id
            await asyncio.sleep(0.05)
        raise SourceSelectorError("telegram_web_outbound_message_not_found")

    async def _media_from_row(self, row: Any, message_id: str) -> WebMessage | None:
        has_media = False
        for selector in TelegramWebSelectors.MEDIA:
            if await row.locator(selector).count():
                has_media = True
                break
        if not has_media:
            return None
        mime = await row.get_attribute("data-mime-type")
        kind = MediaKind.AUDIO if (mime or "").startswith("audio/") else MediaKind.DOCUMENT
        size_raw = await row.get_attribute("data-file-size")
        return WebMessage(
            message_id=message_id,
            source_chat_id=self._chat_id,
            kind=kind,
            file_name=await row.get_attribute("data-file-name"),
            mime_type=mime,
            size=int(size_raw) if size_raw and size_raw.isdigit() else None,
            title=await row.get_attribute("data-title"),
            performer=await row.get_attribute("data-performer"),
        )

    async def wait_for_new_media(
        self, outbound_message_id: str, excluded: set[str], timeout_seconds: float
    ) -> WebMessage:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            rows = await self._ordered_message_rows()
            ids = [message_id for message_id, _row in rows]
            if outbound_message_id not in ids:
                await asyncio.sleep(0.05)
                continue
            outbound_index = ids.index(outbound_message_id)
            self._observed_message_ids.update(ids[: outbound_index + 1])
            fallback: WebMessage | None = None
            for message_id, row in rows[outbound_index + 1 :]:
                if message_id in excluded or message_id in self._observed_message_ids:
                    continue
                media = await self._media_from_row(row, message_id)
                if media is None:
                    continue
                reply_id = None
                for attribute in TelegramWebSelectors.REPLY_ID_ATTRIBUTES:
                    reply_id = await row.get_attribute(attribute)
                    if reply_id:
                        break
                if reply_id == outbound_message_id:
                    self._observed_message_ids.add(message_id)
                    return media
                if reply_id is not None:
                    self._observed_message_ids.add(message_id)
                elif fallback is None:
                    fallback = media
            if fallback is not None:
                self._observed_message_ids.add(fallback.message_id)
                return fallback
            await asyncio.sleep(0.5)
        raise SourceTimeout("source_response_timeout")

    async def download_to(self, message_id: str, destination: Path, max_bytes: int) -> None:
        row = self._page.locator(
            f'[data-message-id="{message_id}"], [data-mid="{message_id}"], #{message_id}'
        ).first
        if not await row.count():
            raise SourceSelectorError("telegram_web_message_disappeared")
        button = await self._first_visible(TelegramWebSelectors.DOWNLOAD, within=row)
        staging = destination.parent / ".playwright-download"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o700)
        download = None
        path_task: asyncio.Task[Path] | None = None
        complete = False
        try:
            session = await self._context.new_cdp_session(self._page)
            await session.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": str(staging),
                    "eventsEnabled": True,
                },
            )
            async with self._page.expect_download() as pending:
                await button.click()
            download = await pending.value
            path_task = asyncio.create_task(download.path())
            while not path_task.done():
                size = sum(path.stat().st_size for path in staging.iterdir() if path.is_file())
                if size > max_bytes:
                    await download.cancel()
                    await asyncio.gather(path_task, return_exceptions=True)
                    raise SourceDownloadError("source_media_too_large")
                await asyncio.sleep(0.01)
            completed = await path_task
            if completed.stat().st_size > max_bytes:
                await download.cancel()
                raise SourceDownloadError("source_media_too_large")
            shutil.copyfile(completed, destination)
            complete = True
        except SourceDownloadError:
            raise
        except Exception as exc:
            raise SourceDownloadError("source_download_failed") from exc
        finally:
            if path_task is not None and not path_task.done():
                if download is not None:
                    await download.cancel()
                path_task.cancel()
                await asyncio.gather(path_task, return_exceptions=True)
            shutil.rmtree(staging, ignore_errors=True)
            if destination.exists() and not complete:
                destination.unlink()


class PlaywrightMusicSource:
    """Serialize each request through download and reject consumed source messages."""

    def __init__(
        self,
        driver: TelegramWebDriver,
        web_url: str,
        source_username: str,
        *,
        timeout_seconds: float,
        max_media_bytes: int,
        download_root: Path | None = None,
        chunk_size: int = 512 * 1024,
    ) -> None:
        self._driver = driver
        self._web_url = web_url
        self._source_username = source_username
        self._timeout = timeout_seconds
        self._max_bytes = max_media_bytes
        self._download_root = download_root
        self._chunk_size = chunk_size
        self._lock = asyncio.Lock()
        self._message_ids: dict[int, str] = {}
        self._lease_owners: dict[int, asyncio.Task[Any]] = {}
        self._consumed_message_ids: set[str] = set()

    @staticmethod
    def _numeric_id(value: str) -> int:
        if value.isdigit():
            return int(value)
        return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") & ((1 << 63) - 1)

    async def request_track(self, request: SourceRequest) -> SourceMedia:
        await self._lock.acquire()
        try:
            await self._driver.open_chat(self._web_url, self._source_username)
            baseline = await self._driver.snapshot_message_ids() | self._consumed_message_ids
            outbound_id = await self._driver.send_query(request.query)
            candidate = await self._driver.wait_for_new_media(outbound_id, baseline, self._timeout)
            self._consumed_message_ids.add(candidate.message_id)
            if candidate.message_id in baseline:
                raise SourceTimeout("source_returned_no_new_media")
            if candidate.size is not None and candidate.size > self._max_bytes:
                raise SourceDownloadError("source_media_too_large")
            if candidate.kind is MediaKind.DOCUMENT and not (candidate.mime_type or "").startswith(
                "audio/"
            ):
                raise SourceDownloadError("source_media_not_audio")
            numeric_id = self._numeric_id(candidate.message_id)
            self._message_ids[numeric_id] = candidate.message_id
            media = SourceMedia(
                source_chat_id=candidate.source_chat_id,
                source_message_id=numeric_id,
                kind=candidate.kind,
                file_name=candidate.file_name,
                mime_type=candidate.mime_type,
                size=candidate.size,
                title=candidate.title,
                performer=candidate.performer,
            )
            owner = asyncio.current_task()
            if owner is not None:
                self._lease_owners[numeric_id] = owner

                def release_abandoned(task: asyncio.Task[Any]) -> None:
                    self._release_abandoned(numeric_id, task)

                owner.add_done_callback(release_abandoned)
            return media
        except BaseException:
            self._lock.release()
            raise

    def _release_abandoned(self, media_id: int, owner: asyncio.Task[Any]) -> None:
        if self._lease_owners.get(media_id) is not owner:
            return
        self._lease_owners.pop(media_id, None)
        self._message_ids.pop(media_id, None)
        if self._lock.locked():
            self._lock.release()

    async def iter_download(self, media: SourceMedia) -> AsyncIterator[bytes]:
        message_id = self._message_ids.pop(media.source_message_id)
        self._lease_owners.pop(media.source_message_id, None)
        root = self._download_root
        if root is not None:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(tempfile.mkdtemp(prefix="telegram-web-", dir=root))
        directory.chmod(0o700)
        destination = directory / "download"
        try:
            try:
                await self._driver.download_to(message_id, destination, self._max_bytes)
            except SourceDownloadError:
                raise
            except Exception as exc:
                raise SourceDownloadError("source_download_failed") from exc
            with destination.open("rb") as source:
                while chunk := source.read(self._chunk_size):
                    yield chunk
        finally:
            shutil.rmtree(directory, ignore_errors=True)
            if self._lock.locked():
                self._lock.release()
