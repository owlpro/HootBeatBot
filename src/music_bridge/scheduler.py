"""Daily APScheduler registration in each topic's explicit timezone."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from music_bridge.service import safe_exception_category
from music_bridge.settings import TopicConfig

TopicRunner = Callable[[TopicConfig, datetime], Awaitable[str]]
logger = logging.getLogger(__name__)
MISFIRE_GRACE_SECONDS = 3600


class ActiveJobRegistry:
    """Track scheduler-owned tasks so shutdown can await their cancellation cleanup."""

    def __init__(self) -> None:
        self._accepting = True
        self._tasks: set[asyncio.Task[Any]] = set()

    async def run(self, job: Callable[[], Awaitable[str]]) -> str | None:
        if not self._accepting:
            return None
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always supplies one here
            raise RuntimeError("registered job has no asyncio task")
        self._tasks.add(task)
        try:
            return await job()
        finally:
            self._tasks.discard(task)

    def stop_accepting(self) -> None:
        self._accepting = False

    async def cancel_and_wait(self) -> None:
        current = asyncio.current_task()
        active = [task for task in self._tasks if task is not current and not task.done()]
        for task in active:
            if task.cancelling() == 0:
                task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


def canonical_occurrence(topic: TopicConfig, now: datetime | None = None) -> datetime:
    """Return the latest configured local occurrence not later than ``now``."""
    zone = ZoneInfo(topic.timezone)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    occurrence = local_now.replace(hour=topic.hour, minute=topic.minute, second=0, microsecond=0)
    if occurrence > local_now:
        occurrence -= timedelta(days=1)
    return occurrence.astimezone(UTC)


def register_topic_jobs(
    scheduler: AsyncIOScheduler,
    topics: Iterable[TopicConfig],
    runner: TopicRunner,
    active_jobs: ActiveJobRegistry | None = None,
) -> None:
    """Reconcile deterministic daily jobs; missed runs coalesce and never overlap."""
    topic_list = list(topics)
    registry = active_jobs or ActiveJobRegistry()
    desired = {f"topic:{topic.name}" for topic in topic_list if topic.enabled}
    for job in scheduler.get_jobs():
        if job.id.startswith("topic:") and job.id not in desired:
            scheduler.remove_job(job.id)

    for topic in topic_list:
        if not topic.enabled:
            continue

        async def run(current: TopicConfig = topic) -> None:
            try:
                occurrence = canonical_occurrence(current)
                await registry.run(lambda: runner(current, occurrence))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("scheduled topic job failed (%s)", safe_exception_category(exc))

        job_id = f"topic:{topic.name}"
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)
        scheduler.add_job(
            run,
            CronTrigger(
                hour=topic.hour,
                minute=topic.minute,
                timezone=ZoneInfo(topic.timezone),
            ),
            id=f"topic:{topic.name}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )


def create_scheduler() -> Any:
    return AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
