import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from music_bridge.scheduler import ActiveJobRegistry, canonical_occurrence, register_topic_jobs
from music_bridge.settings import TopicConfig


async def runner(topic: TopicConfig, scheduled_for: datetime) -> str:
    del topic, scheduled_for
    return "ok"


def topic(name: str, enabled: bool = True, hour: int = 9) -> TopicConfig:
    return TopicConfig(
        name=name,
        chat_id=-1001234567890,
        message_thread_id=10 if name == "rock" else 11,
        genre=name,
        hour=hour,
        minute=15,
        enabled=enabled,
    )


def test_scheduler_registers_only_enabled_tehran_jobs_and_replaces() -> None:
    scheduler: AsyncIOScheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    register_topic_jobs(scheduler, [topic("rock"), topic("jazz", False)], runner)
    jobs = scheduler.get_jobs()
    assert [job.id for job in jobs] == ["topic:rock"]
    trigger: Any = jobs[0].trigger
    assert str(trigger.timezone) == "Asia/Tehran"
    assert str(trigger.fields[5]) == "9"
    register_topic_jobs(scheduler, [topic("rock", hour=10)], runner)
    assert len(scheduler.get_jobs()) == 1
    assert str(scheduler.get_job("topic:rock").trigger.fields[5]) == "10"


def test_canonical_occurrence_uses_configured_local_wall_clock() -> None:
    configured = topic("rock", hour=9)
    now = datetime(2026, 9, 22, 18, 44, 37, 123, tzinfo=UTC)
    occurrence = canonical_occurrence(configured, now)
    local = occurrence.astimezone(ZoneInfo("Asia/Tehran"))
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 9, 22, 9, 15)
    assert local.second == local.microsecond == 0


def test_canonical_occurrence_uses_previous_day_before_scheduled_time() -> None:
    configured = topic("rock", hour=23)
    execution = datetime(2026, 9, 21, 20, 40, tzinfo=UTC)  # 00:10 Tehran on Sep 22

    local = canonical_occurrence(configured, execution).astimezone(ZoneInfo("Asia/Tehran"))

    assert (local.year, local.month, local.day, local.hour, local.minute) == (
        2026,
        9,
        21,
        23,
        15,
    )


def test_registered_job_has_bounded_misfire_grace_and_no_overlap() -> None:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    register_topic_jobs(scheduler, [topic("rock")], runner)
    job = scheduler.get_job("topic:rock")
    assert job is not None
    assert 0 < job.misfire_grace_time <= 3600
    assert job.max_instances == 1


def test_registered_job_passes_canonical_occurrence() -> None:
    observed: list[datetime] = []

    async def observe(_topic: TopicConfig, scheduled_for: datetime) -> str:
        observed.append(scheduled_for)
        return "ok"

    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    configured = topic("rock", hour=9)
    register_topic_jobs(scheduler, [configured], observe)
    job = scheduler.get_job("topic:rock")
    assert job is not None
    asyncio.run(job.func())
    local = observed[0].astimezone(ZoneInfo(configured.timezone))
    assert (local.hour, local.minute, local.second, local.microsecond) == (9, 15, 0, 0)


def test_scheduled_job_logs_only_failure_class(caplog) -> None:
    async def fail(_topic: TopicConfig, _scheduled_for: datetime) -> str:
        raise RuntimeError("https://api.telegram.org/botSECRET private payload")

    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    register_topic_jobs(scheduler, [topic("rock")], fail)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scheduler.get_job("topic:rock").func())

    assert "RuntimeError" in caplog.text
    assert "SECRET" not in caplog.text
    assert "https://" not in caplog.text


def test_active_job_shutdown_waits_for_persistence_before_disposal() -> None:
    async def exercise() -> list[str]:
        registry = ActiveJobRegistry()
        started = asyncio.Event()
        events: list[str] = []

        async def job() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
                return "unexpected"
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                events.append("status_persisted")
                raise

        task = asyncio.create_task(registry.run(job))
        await started.wait()
        registry.stop_accepting()
        await registry.cancel_and_wait()
        events.append("database_disposed")
        assert task.cancelled()
        return events

    assert asyncio.run(exercise()) == ["status_persisted", "database_disposed"]
