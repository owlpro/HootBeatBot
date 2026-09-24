import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from music_bridge.scheduler import (
    ActiveJobRegistry,
    canonical_occurrence,
    canonical_rotation_occurrence,
    register_topic_jobs,
    rotation_topic_for_occurrence,
)
from music_bridge.settings import RotationSchedule, TopicConfig


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


def test_rotation_registers_one_hourly_job_and_removes_daily_topic_jobs() -> None:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    configured = [topic("rock"), topic("jazz")]
    register_topic_jobs(scheduler, configured, runner)

    register_topic_jobs(
        scheduler,
        configured,
        runner,
        rotation_schedule=RotationSchedule(enabled=True),
    )

    jobs = scheduler.get_jobs()
    assert [job.id for job in jobs] == ["topic-rotation"]
    trigger: Any = jobs[0].trigger
    assert str(trigger.fields[5]) == "*"
    assert str(trigger.fields[6]) == "5,35"
    assert str(trigger.timezone) == "Asia/Tehran"
    assert jobs[0].coalesce is True
    assert jobs[0].max_instances == 1
    assert 0 < jobs[0].misfire_grace_time <= 3600


def test_disabling_rotation_restores_daily_jobs_and_removes_rotation_job() -> None:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    configured = [topic("rock"), topic("jazz")]
    register_topic_jobs(
        scheduler,
        configured,
        runner,
        rotation_schedule=RotationSchedule(enabled=True),
    )

    register_topic_jobs(scheduler, configured, runner, rotation_schedule=RotationSchedule())

    assert {job.id for job in scheduler.get_jobs()} == {"topic:rock", "topic:jazz"}


def test_rotation_has_48_exact_fair_slots_without_adjacent_repeats() -> None:
    schedule = RotationSchedule(enabled=True)
    configured = [topic("rock"), topic("jazz"), topic("pop")]
    zone = ZoneInfo(schedule.timezone)
    slots = [
        datetime.combine(date(2026, 9, 23), datetime.min.time(), zone)
        + timedelta(hours=hour, minutes=minute)
        for hour in range(24)
        for minute in schedule.minutes
    ]

    selected = [rotation_topic_for_occurrence(configured, schedule, slot).name for slot in slots]

    assert len(selected) == 48
    assert all(left != right for left, right in zip(selected, selected[1:], strict=False))
    assert {name: selected.count(name) for name in set(selected)} == {
        "rock": 16,
        "jazz": 16,
        "pop": 16,
    }


def test_rotation_uses_absolute_slot_number_across_dates() -> None:
    schedule = RotationSchedule(enabled=True)
    configured = [topic("rock"), topic("jazz"), topic("pop"), topic("folk"), topic("dance")]
    zone = ZoneInfo(schedule.timezone)
    first = datetime(2026, 9, 23, 0, 5, tzinfo=zone)
    next_day = first + timedelta(days=1)

    first_name = rotation_topic_for_occurrence(configured, schedule, first).name
    next_name = rotation_topic_for_occurrence(configured, schedule, next_day).name

    assert first_name != next_name  # 48 slots/day leaves an extra rotation of three.


def test_rotation_canonical_occurrence_is_exact_latest_slot() -> None:
    schedule = RotationSchedule(enabled=True)
    now = datetime(2026, 9, 23, 10, 34, 59, 999, tzinfo=ZoneInfo("Asia/Tehran"))

    occurrence = canonical_rotation_occurrence(schedule, now)

    assert occurrence == datetime(2026, 9, 23, 10, 5, tzinfo=ZoneInfo("Asia/Tehran")).astimezone(
        UTC
    )


def test_rotation_job_passes_selected_topic_and_exact_slot() -> None:
    observed: list[tuple[str, datetime]] = []

    async def observe(current: TopicConfig, scheduled_for: datetime) -> str:
        observed.append((current.name, scheduled_for))
        return "ok"

    schedule = RotationSchedule(enabled=True)
    configured = [topic("rock"), topic("jazz")]
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Tehran"))
    register_topic_jobs(scheduler, configured, observe, rotation_schedule=schedule)

    job = scheduler.get_job("topic-rotation")
    assert job is not None
    asyncio.run(job.func())

    name, occurrence = observed[0]
    assert name == rotation_topic_for_occurrence(configured, schedule, occurrence).name
    local = occurrence.astimezone(ZoneInfo(schedule.timezone))
    assert local.minute in schedule.minutes
    assert local.second == local.microsecond == 0


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
