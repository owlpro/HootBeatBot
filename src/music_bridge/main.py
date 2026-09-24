"""Command-line lifecycle and production dependency wiring."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aiogram import Bot, Dispatcher

from music_bridge.db import Database
from music_bridge.destination.aiogram_publisher import AiogramPublisher, BotLike
from music_bridge.domain import DestinationPublisher, MusicSource
from music_bridge.moderation import ForumModerator
from music_bridge.repositories import Repository
from music_bridge.scheduler import ActiveJobRegistry, create_scheduler, register_topic_jobs
from music_bridge.service import BridgeOperationalError, BridgeService, safe_exception_category
from music_bridge.settings import GroupConfig, RotationSchedule, Settings, TopicConfig, load_config
from music_bridge.source.soundcloud_catalog import (
    CatalogCache,
    CatalogMusicSource,
    PublicChromiumSoundCloudCollector,
    SoundCloudCatalogManager,
)
from music_bridge.source.soundcloud_source import (
    AsyncCommandRunner,
    SoundCloudMusicSource,
    canonical_track_url,
)

logger = logging.getLogger(__name__)


def _enabled_catalog_queries(topics: Sequence[TopicConfig]) -> list[str]:
    queries = (
        query
        for topic in topics
        if topic.enabled
        for query in topic.render_catalog_queries()
        if canonical_track_url(query) is None
    )
    return list(dict.fromkeys(queries))


class PollingLifecycle:
    """Own aiogram polling and drain handlers before the Bot session closes."""

    def __init__(self, dispatcher: Any, bot: Any) -> None:
        self._dispatcher = dispatcher
        self._bot = bot
        self._task: asyncio.Task[Any] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("polling already started")
        self._task = asyncio.create_task(
            self._dispatcher.start_polling(self._bot, handle_signals=False)
        )

    async def wait_for_shutdown(self, shutdown_event: asyncio.Event) -> None:
        """Return for shutdown, or classify any unexpected polling termination."""
        if self._task is None:
            raise RuntimeError("polling not started")
        polling_task = self._task

        async def wait_for_polling() -> Any:
            return await asyncio.shield(polling_task)

        shutdown_wait = asyncio.create_task(shutdown_event.wait())
        polling_wait = asyncio.create_task(wait_for_polling())
        try:
            done, _pending = await asyncio.wait(
                {shutdown_wait, polling_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if polling_wait in done:
                try:
                    await polling_wait
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    raise BridgeOperationalError("polling_failed") from None
                raise BridgeOperationalError("polling_stopped_unexpectedly")
        finally:
            for waiter in (shutdown_wait, polling_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(shutdown_wait, polling_wait, return_exceptions=True)

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            await self._dispatcher.stop_polling()
        await self._task


async def start_moderation(
    bot: Any, groups: list[GroupConfig], *, enabled: bool
) -> PollingLifecycle | None:
    """Start moderation polling only when the administrative feature is enabled."""
    if not enabled:
        return None
    dispatcher = Dispatcher()
    me = await bot.get_me()
    moderator = ForumModerator(bot, groups, bot_id=me.id)
    dispatcher.message.register(moderator.handle)
    polling = PollingLifecycle(dispatcher, bot)
    polling.start()
    return polling


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Telegram topic daily music bridge")
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--check-config", action="store_true")
    modes.add_argument("--run-once", metavar="TOPIC_NAME")
    return result


def install_signal_handlers(loop: Any, shutdown_event: asyncio.Event) -> None:
    """Request orderly shutdown for both standard service termination signals."""
    callback: Callable[[], None] = shutdown_event.set
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, callback)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(callback))


async def _build_and_run(
    settings: Settings,
    topics: list[TopicConfig],
    once: str | None,
    groups: list[GroupConfig] | None = None,
    moderation_enabled: bool = False,
    rotation_schedule: RotationSchedule | None = None,
) -> None:
    Path("var/tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
    database = Database(settings.database_url)
    bot: Bot | None = None
    polling: PollingLifecycle | None = None
    scheduler: Any | None = None
    catalog_task: asyncio.Task[None] | None = None
    active_jobs = ActiveJobRegistry()
    cleanup_error: BaseException | None = None
    try:
        await database.create_schema()
        repository = Repository(database.session_factory)
        await repository.recover_nonterminal()
        bot = Bot(settings.destination_bot_token.get_secret_value())
        direct_source = SoundCloudMusicSource(
            AsyncCommandRunner(),
            download_root=Path("var/tmp/source"),
            timeout_seconds=settings.source_response_timeout_seconds,
            max_media_bytes=settings.max_media_bytes,
            min_duration_seconds=settings.soundcloud_min_duration_seconds,
            search_limit=settings.soundcloud_search_limit,
        )
        catalog_cache = CatalogCache(settings.soundcloud_catalog_path)
        selected = next((topic for topic in topics if topic.name == once), None)
        if once is not None and selected is None:
            raise ValueError(f"unknown topic: {once}")
        if selected is None:
            catalog_queries = _enabled_catalog_queries(topics)
        else:
            catalog_queries = list(
                dict.fromkeys(
                    query
                    for query in selected.render_catalog_queries()
                    if canonical_track_url(query) is None
                )
            )
        catalog_manager = SoundCloudCatalogManager(
            catalog_cache,
            PublicChromiumSoundCloudCollector(
                chromium_executable=settings.chromium_executable_path,
                page_timeout_seconds=settings.soundcloud_catalog_refresh_timeout_seconds,
                result_limit=settings.soundcloud_search_limit,
            ),
            catalog_queries,
            timeout_seconds=settings.soundcloud_catalog_refresh_timeout_seconds,
        )
        if catalog_queries:
            await catalog_manager.refresh_or_load()
            used_source_ids = await repository.get_used_source_message_ids()
            capacity = catalog_cache.require_capacity(catalog_queries, used_source_ids)
            logger.info("catalog usable capacity by query: %s", capacity)
        source = CatalogMusicSource(
            catalog_cache,
            direct_source,
            candidate_attempts=settings.soundcloud_candidate_attempts,
        )
        service = BridgeService(
            repository,
            cast(MusicSource, source),
            cast(
                DestinationPublisher,
                AiogramPublisher(cast(BotLike, bot)),
            ),
            settings.max_media_bytes,
            Path("var/tmp"),
            source_attempts=settings.soundcloud_candidate_attempts,
        )
        if once is not None:
            assert selected is not None
            occurrence = datetime.now(UTC).replace(second=0, microsecond=0)
            print(await service.run(selected, occurrence))
            return
        shutdown_event = asyncio.Event()
        install_signal_handlers(asyncio.get_running_loop(), shutdown_event)
        if catalog_queries:
            catalog_task = asyncio.create_task(
                catalog_manager.run_periodic(
                    settings.soundcloud_catalog_refresh_interval_seconds,
                    shutdown_event,
                )
            )
        polling = await start_moderation(bot, groups or [], enabled=moderation_enabled)
        scheduler = create_scheduler()
        register_topic_jobs(
            scheduler,
            topics,
            service.run,
            active_jobs,
            rotation_schedule=rotation_schedule,
        )
        scheduler.start()
        if polling is None:
            await shutdown_event.wait()
        else:
            await polling.wait_for_shutdown(shutdown_event)
    finally:
        had_active_exception = sys.exc_info()[0] is not None
        active_jobs.stop_accepting()
        if catalog_task is not None and not catalog_task.done():
            catalog_task.cancel()
        if catalog_task is not None:
            await asyncio.gather(catalog_task, return_exceptions=True)
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            await active_jobs.cancel_and_wait()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if polling is not None:
            try:
                await polling.stop()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if bot is not None:
            try:
                await bot.session.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            await database.dispose()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None and not had_active_exception:
            raise cleanup_error


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings()  # type: ignore[call-arg]  # populated from environment
    config = load_config(settings.topics_config_path)
    topics = config.topics
    if args.check_config:
        print(f"configuration valid: {len(topics)} topic(s)")
        return 0
    asyncio.run(
        _build_and_run(
            settings,
            topics,
            args.run_once,
            config.groups,
            config.moderation_enabled,
            config.rotation_schedule,
        )
    )
    return 0


def cli() -> None:
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {safe_exception_category(exc)}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    cli()
