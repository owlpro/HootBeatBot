import asyncio
import importlib.util
import os
from pathlib import Path

import pytest


def load_bootstrap_module():
    script = Path(__file__).parents[1] / "scripts" / "bootstrap_telegram_web.py"
    spec = importlib.util.spec_from_file_location("bootstrap_telegram_web", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secure_profile_directory_enforces_private_permissions(tmp_path: Path) -> None:
    module = load_bootstrap_module()
    profile = tmp_path / "telegram-profile"
    module.secure_profile_directory(profile)
    os.chmod(profile, 0o755)  # noqa: S103 - deliberately simulate insecure permissions
    module.secure_profile_directory(profile)
    assert os.stat(profile).st_mode & 0o777 == 0o700


def test_login_parser_never_accepts_phone_code_or_password() -> None:
    module = load_bootstrap_module()
    options = {action.dest for action in module.parser()._actions}
    assert not ({"phone", "code", "password", "two_factor"} & options)


@pytest.mark.asyncio
async def test_headless_bootstrap_writes_private_login_screenshot(
    monkeypatch, tmp_path: Path
) -> None:
    module = load_bootstrap_module()
    screenshot = tmp_path / "private" / "login.png"
    closed = False

    waited = False
    screenshot_calls = 0

    class Browser:
        async def open(self, url: str) -> None:
            assert url == "https://web.telegram.org/k/"

        async def is_logged_in(self) -> bool:
            return False

        async def screenshot(self, path: Path) -> None:
            nonlocal screenshot_calls
            screenshot_calls += 1
            path.write_bytes(f"login-{screenshot_calls}".encode())

        async def wait_for_login(self) -> None:
            nonlocal waited
            waited = True
            await asyncio.sleep(0.02)

        async def close(self) -> None:
            nonlocal closed
            closed = True

    async def launch(profile: Path, *, headless: bool):
        assert profile == tmp_path / "profile"
        assert headless
        return Browser()

    monkeypatch.setattr(module, "launch_browser", launch)
    result = await module.bootstrap(
        profile_path=tmp_path / "profile",
        web_url="https://web.telegram.org/k/",
        headed=False,
        screenshot_path=screenshot,
        check_login=False,
    )
    assert result == 0
    assert screenshot.exists()
    assert os.stat(screenshot.parent).st_mode & 0o777 == 0o700
    assert os.stat(screenshot).st_mode & 0o777 == 0o600
    assert waited
    assert closed


@pytest.mark.asyncio
async def test_check_login_returns_status_and_always_closes(monkeypatch, tmp_path: Path) -> None:
    module = load_bootstrap_module()
    closed = False

    class Browser:
        async def open(self, url: str) -> None:
            del url

        async def is_logged_in(self) -> bool:
            return True

        async def screenshot(self, path: Path) -> None:
            del path

        async def wait_for_login(self) -> None:
            raise AssertionError

        async def close(self) -> None:
            nonlocal closed
            closed = True

    async def launch(profile: Path, *, headless: bool):
        del profile, headless
        return Browser()

    monkeypatch.setattr(module, "launch_browser", launch)
    result = await module.bootstrap(
        profile_path=tmp_path / "profile",
        web_url="https://web.telegram.org/k/",
        headed=False,
        screenshot_path=None,
        check_login=True,
    )
    assert result == 0
    assert closed
