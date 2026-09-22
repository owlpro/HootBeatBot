#!/usr/bin/env python3
"""Securely bootstrap/check a persistent Telegram Web browser profile.

No phone number, login code, password, or 2FA value is accepted by this CLI.
The user completes Telegram's own login UI or scans the saved QR screenshot.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

DEFAULT_WEB_URL = "https://web.telegram.org/k/"
DEFAULT_PROFILE = "/data/telegram-web-profile"


class BootstrapBrowser(Protocol):
    async def open(self, url: str) -> None: ...

    async def is_logged_in(self) -> bool: ...

    async def screenshot(self, path: Path) -> None: ...

    async def wait_for_login(self) -> None: ...

    async def close(self) -> None: ...


class PlaywrightBootstrapBrowser:
    CHAT_LIST = "[data-testid='chat-list'], [role='list'][aria-label*='Chat'], .chatlist"

    def __init__(self, playwright: Any, context: Any, page: Any) -> None:
        self._playwright = playwright
        self._context = context
        self._page = page

    async def open(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def is_logged_in(self) -> bool:
        return await self._page.locator(self.CHAT_LIST).count() > 0

    async def screenshot(self, path: Path) -> None:
        await self._page.screenshot(path=str(path), full_page=False)

    async def wait_for_login(self) -> None:
        await self._page.locator(self.CHAT_LIST).first.wait_for(state="visible", timeout=0)

    async def close(self) -> None:
        try:
            await self._context.close()
        finally:
            await self._playwright.stop()


async def launch_browser(profile: Path, *, headless: bool) -> BootstrapBrowser:
    from playwright.async_api import async_playwright

    manager = async_playwright()
    playwright = await manager.start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except BaseException:
        await playwright.stop()
        raise
    page = context.pages[0] if context.pages else await context.new_page()
    return PlaywrightBootstrapBrowser(playwright, context, page)


def secure_profile_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bootstrap a persistent Telegram Web login")
    result.add_argument(
        "--profile-path",
        type=Path,
        default=Path(os.environ.get("TELEGRAM_WEB_PROFILE_PATH", DEFAULT_PROFILE)),
    )
    result.add_argument("--web-url", default=os.environ.get("TELEGRAM_WEB_URL", DEFAULT_WEB_URL))
    result.add_argument("--headed", action="store_true", help="show Chromium (requires DISPLAY)")
    result.add_argument(
        "--headless-qr-screenshot",
        type=Path,
        default=(
            Path(os.environ["TELEGRAM_LOGIN_SCREENSHOT_PATH"])
            if os.environ.get("TELEGRAM_LOGIN_SCREENSHOT_PATH")
            else None
        ),
        metavar="PATH",
    )
    result.add_argument("--check-login", action="store_true")
    return result


async def bootstrap(
    *,
    profile_path: Path,
    web_url: str,
    headed: bool,
    screenshot_path: Path | None,
    check_login: bool,
) -> int:
    os.umask(0o077)
    secure_profile_directory(profile_path)
    browser = await launch_browser(profile_path, headless=not headed)
    try:
        await browser.open(web_url)
        logged_in = await browser.is_logged_in()
        if check_login:
            print("Telegram Web login: valid" if logged_in else "Telegram Web login: required")
            return 0 if logged_in else 1
        if logged_in:
            print("Telegram Web profile is already logged in")
            return 0
        if headed:
            print("Complete login in the Telegram Web window; credentials stay in Telegram's UI.")
            await browser.wait_for_login()
            print("Telegram Web login saved")
            return 0
        if screenshot_path is None:
            raise RuntimeError("headless login requires --headless-qr-screenshot PATH")
        screenshot_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        screenshot_path.parent.chmod(0o700)
        await browser.screenshot(screenshot_path)
        screenshot_path.chmod(0o600)
        print(
            f"Login screenshot written securely to {screenshot_path}; "
            "scan it, then run --check-login"
        )
        return 1
    finally:
        await browser.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.headed and not os.environ.get("DISPLAY"):
        parser().error("--headed requires DISPLAY")
    headed = bool(
        args.headed
        or (
            not args.check_login
            and args.headless_qr_screenshot is None
            and os.environ.get("DISPLAY")
        )
    )
    return asyncio.run(
        bootstrap(
            profile_path=args.profile_path,
            web_url=args.web_url,
            headed=headed,
            screenshot_path=args.headless_qr_screenshot,
            check_login=args.check_login,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
