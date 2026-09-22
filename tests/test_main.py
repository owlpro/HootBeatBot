import asyncio
import signal
from pathlib import Path

import pytest

from music_bridge.main import PollingLifecycle, _build_and_run, cli, install_signal_handlers, main
from music_bridge.service import BridgeOperationalError
from music_bridge.settings import Settings


def test_check_config_loads_topics_without_connecting(monkeypatch, tmp_path: Path, capsys) -> None:
    topics = tmp_path / "topics.yml"
    topics.write_text(
        "topics:\n"
        "  - name: rock\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: rock\n"
        "    hour: 9\n"
        "    minute: 0\n"
    )
    monkeypatch.setenv("TELEGRAM_WEB_PROFILE_PATH", str(tmp_path / "telegram-profile"))
    monkeypatch.setenv("DESTINATION_BOT_TOKEN", "123456:not-real")
    monkeypatch.setenv("TOPICS_CONFIG_PATH", str(topics))
    assert main(["--check-config"]) == 0
    assert capsys.readouterr().out == "configuration valid: 1 topic(s)\n"


def test_signal_handlers_set_shutdown_event() -> None:
    callbacks: dict[signal.Signals, object] = {}

    class Loop:
        def add_signal_handler(self, sig, callback) -> None:
            callbacks[sig] = callback

    event = asyncio.Event()
    install_signal_handlers(Loop(), event)
    assert set(callbacks) == {signal.SIGINT, signal.SIGTERM}
    callbacks[signal.SIGTERM]()
    assert event.is_set()


@pytest.mark.asyncio
async def test_partial_startup_failure_disposes_database(monkeypatch, tmp_path: Path) -> None:
    disposed = False

    class BrokenDatabase:
        def __init__(self, url: str) -> None:
            del url

        async def create_schema(self) -> None:
            raise RuntimeError("schema failed")

        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    settings = Settings(
        telegram_web_profile_path=tmp_path / "telegram-profile",
        destination_bot_token=":".join(("1", "token")),
        topics_config_path=tmp_path / "topics.yml",
        _env_file=None,
    )
    monkeypatch.setattr("music_bridge.main.Database", BrokenDatabase)
    with pytest.raises(RuntimeError, match="schema failed"):
        await _build_and_run(settings, [], None)
    assert disposed


@pytest.mark.asyncio
async def test_polling_shutdown_stops_intake_then_waits_for_handlers() -> None:
    events: list[str] = []
    handler_release = asyncio.Event()

    class Dispatcher:
        async def start_polling(self, bot, *, handle_signals: bool) -> None:
            del bot
            assert handle_signals is False
            events.append("polling-started")
            await handler_release.wait()
            events.append("handlers-drained")

        async def stop_polling(self) -> None:
            events.append("polling-stopped")
            handler_release.set()

    lifecycle = PollingLifecycle(Dispatcher(), object())
    lifecycle.start()
    await asyncio.sleep(0)
    await lifecycle.stop()
    assert events == ["polling-started", "polling-stopped", "handlers-drained"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_polling_exit_wakes_main_with_safe_classified_failure(raises: bool) -> None:
    class Dispatcher:
        async def start_polling(self, bot, *, handle_signals: bool) -> None:
            del bot, handle_signals
            if raises:
                raise RuntimeError("secret polling details")

        async def stop_polling(self) -> None:
            raise AssertionError("completed polling must not be stopped again")

    lifecycle = PollingLifecycle(Dispatcher(), object())
    lifecycle.start()
    with pytest.raises(BridgeOperationalError) as caught:
        await asyncio.wait_for(lifecycle.wait_for_shutdown(asyncio.Event()), timeout=1)
    assert caught.value.category == ("polling_failed" if raises else "polling_stopped_unexpectedly")


@pytest.mark.asyncio
async def test_polling_failure_stops_scheduler_and_closes_dependencies(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class Database:
        session_factory = object()

        def __init__(self, url: str) -> None:
            del url

        async def create_schema(self) -> None:
            events.append("schema")

        async def dispose(self) -> None:
            events.append("database-closed")

    class Repository:
        def __init__(self, factory: object) -> None:
            del factory

        async def recover_nonterminal(self) -> None:
            return None

    class WebDriver:
        @classmethod
        async def launch(cls, profile_path: Path, *, headless: bool):
            del profile_path, headless
            return cls()

        async def check_login(self, web_url: str) -> bool:
            del web_url
            return True

        async def close(self) -> None:
            events.append("browser-closed")

    class Session:
        async def close(self) -> None:
            events.append("bot-closed")

    class Bot:
        session = Session()

        def __init__(self, token: str) -> None:
            del token

        async def get_me(self):
            return type("Me", (), {"id": 7})()

    class Messages:
        def register(self, handler: object) -> None:
            del handler

    class Dispatcher:
        message = Messages()

        async def start_polling(self, bot: object, *, handle_signals: bool) -> None:
            del bot, handle_signals
            raise RuntimeError("private polling error")

    class Scheduler:
        def start(self) -> None:
            events.append("scheduler-started")

        def shutdown(self, *, wait: bool) -> None:
            assert wait is False
            events.append("scheduler-stopped")

    scheduler = Scheduler()
    monkeypatch.setattr("music_bridge.main.Database", Database)
    monkeypatch.setattr("music_bridge.main.Repository", Repository)
    monkeypatch.setattr("music_bridge.main.PlaywrightTelegramWebDriver", WebDriver)
    monkeypatch.setattr("music_bridge.main.Bot", Bot)
    monkeypatch.setattr("music_bridge.main.Dispatcher", Dispatcher)
    monkeypatch.setattr("music_bridge.main.create_scheduler", lambda: scheduler)
    monkeypatch.setattr("music_bridge.main.register_topic_jobs", lambda *args: None)
    monkeypatch.setattr("music_bridge.main.install_signal_handlers", lambda *args: None)
    settings = Settings(
        telegram_web_profile_path=tmp_path / "profile",
        destination_bot_token=":".join(("1", "token")),
        topics_config_path=tmp_path / "topics.yml",
        _env_file=None,
    )

    with pytest.raises(BridgeOperationalError, match="polling_failed"):
        await _build_and_run(settings, [], None)

    assert events == [
        "schema",
        "scheduler-started",
        "scheduler-stopped",
        "bot-closed",
        "browser-closed",
        "database-closed",
    ]


def test_cli_does_not_print_exception_messages(monkeypatch, capsys) -> None:
    def fail() -> int:
        raise RuntimeError("https://api.telegram.org/botSECRET private payload")

    monkeypatch.setattr("music_bridge.main.main", fail)
    with pytest.raises(SystemExit) as caught:
        cli()

    assert caught.value.code == 2
    stderr = capsys.readouterr().err
    assert stderr == "error: RuntimeError\n"
