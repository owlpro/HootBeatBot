from pathlib import Path

import pytest
from pydantic import ValidationError

from music_bridge.settings import RotationSchedule, Settings, TopicConfig, load_config, load_topics


def valid_settings(tmp_path: Path) -> dict[str, object]:
    return {
        "destination_bot_token": "123456:secret-token",
        "topics_config_path": tmp_path / "topics.yml",
    }


def test_settings_require_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, topics_config_path=tmp_path / "topics.yml")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination_bot_token", ""),
        ("destination_bot_token", "   "),
    ],
)
def test_settings_reject_blank_secrets(tmp_path: Path, field: str, value: str) -> None:
    values = valid_settings(tmp_path)
    values[field] = value
    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_settings_hide_secrets(tmp_path: Path) -> None:
    settings = Settings(**valid_settings(tmp_path), _env_file=None)
    assert "secret-token" not in repr(settings)
    assert not hasattr(settings, "telegram_web_profile_path")
    assert settings.soundcloud_search_limit == 50


def test_settings_reject_removed_personal_telegram_profile_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra"):
        Settings(
            **valid_settings(tmp_path),
            telegram_web_profile_path=tmp_path / "personal-profile",
            _env_file=None,
        )


def test_topic_rejects_non_supergroup_chat_id() -> None:
    with pytest.raises(ValidationError):
        TopicConfig(name="rock", chat_id=123, message_thread_id=10, genre="rock", hour=9, minute=0)


def test_topic_defaults_to_tehran_and_renders_request() -> None:
    topic = TopicConfig(
        name="rock",
        chat_id=-1001234567890,
        message_thread_id=10,
        genre="alternative rock",
        request_template="random {genre}",
        hour=9,
        minute=5,
    )
    assert topic.timezone == "Asia/Tehran"
    assert topic.render_request() == "random alternative rock"
    assert topic.render_catalog_query() == "random alternative rock"


def test_topic_can_use_explicit_catalog_query_without_changing_direct_template() -> None:
    topic = TopicConfig(
        name="rap_fa",
        chat_id=-1001234567890,
        message_thread_id=10,
        genre="Persian rap",
        request_template="{genre}",
        catalog_query="popular Persian rap 2026",
        hour=9,
        minute=5,
    )

    assert topic.render_request() == "Persian rap"
    assert topic.render_catalog_query() == "popular Persian rap 2026"


def test_topic_supports_strict_multi_query_catalog_pool() -> None:
    topic = TopicConfig(
        name="remix",
        chat_id=-1001234567890,
        message_thread_id=10,
        genre="remix",
        catalog_queries=["popular Persian remix", "popular international remix"],
        hour=9,
        minute=5,
    )

    assert topic.render_catalog_queries() == (
        "popular Persian remix",
        "popular international remix",
    )
    with pytest.raises(ValidationError, match="catalog_query"):
        TopicConfig(
            name="bad",
            chat_id=-1001234567890,
            message_thread_id=11,
            genre="remix",
            catalog_query="one",
            catalog_queries=["two"],
            hour=9,
            minute=5,
        )
    with pytest.raises(ValidationError):
        TopicConfig(
            name="bad",
            chat_id=-1001234567890,
            message_thread_id=11,
            genre="remix",
            catalog_queries=["same", "same"],
            hour=9,
            minute=5,
        )


def test_rotation_schedule_defaults_to_two_slots_per_hour_in_tehran() -> None:
    schedule = RotationSchedule()

    assert schedule.enabled is False
    assert schedule.timezone == "Asia/Tehran"
    assert schedule.minutes == [5, 35]


@pytest.mark.parametrize("minutes", [[35, 5], [5, 5], [-1, 35], [5, 60], []])
def test_rotation_schedule_requires_sorted_unique_valid_minutes(minutes: list[int]) -> None:
    with pytest.raises(ValidationError):
        RotationSchedule(enabled=True, minutes=minutes)


def test_load_config_accepts_optional_strict_rotation_schedule(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "rotation_schedule:\n"
        "  enabled: true\n"
        "  timezone: Asia/Tehran\n"
        "  minutes: [5, 35]\n"
        "topics:\n"
        "  - name: remix\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: Persian remix\n"
        "    hour: 9\n"
        "    minute: 0\n"
    )

    config = load_config(path)

    assert config.rotation_schedule is not None
    assert config.rotation_schedule.enabled is True
    with pytest.raises(ValidationError, match="extra"):
        RotationSchedule(unexpected=True)


def test_enabled_rotation_requires_an_enabled_topic(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "rotation_schedule:\n"
        "  enabled: true\n"
        "topics:\n"
        "  - name: paused\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: ambient\n"
        "    hour: 9\n"
        "    minute: 0\n"
        "    enabled: false\n"
    )

    with pytest.raises(ValidationError, match="rotation schedule"):
        load_config(path)


def test_load_topics_rejects_duplicate_names_and_routes(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "topics:\n"
        "  - name: same\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: rock\n"
        "    hour: 9\n"
        "    minute: 0\n"
        "  - name: same\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: jazz\n"
        "    hour: 10\n"
        "    minute: 0\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_topics(path)


def test_load_topics_validates_forum_moderation_groups(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "groups:\n"
        "  - chat_id: -1001234567890\n"
        "    general_thread_id: 1\n"
        "    specialist_thread_ids: [10]\n"
        "topics:\n"
        "  - name: rock\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: rock\n"
        "    hour: 9\n"
        "    minute: 0\n"
    )
    config = load_config(path)
    assert config.groups[0].general_thread_id == 1
    assert config.groups[0].specialist_thread_ids == [10]
    assert not hasattr(config, "source_bots")


def test_moderation_can_be_disabled_without_removing_group_routes(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "moderation_enabled: false\n"
        "groups:\n"
        "  - chat_id: -1001234567890\n"
        "    general_thread_id: 1\n"
        "    specialist_thread_ids: [10]\n"
        "topics:\n"
        "  - name: rock\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: rock\n"
        "    hour: 9\n"
        "    minute: 0\n"
    )

    config = load_config(path)

    assert config.moderation_enabled is False
    assert len(config.groups) == 1


def test_moderation_defaults_off_when_omitted(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "topics:\n"
        "  - name: rock\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: rock\n"
        "    hour: 9\n"
        "    minute: 0\n"
    )

    assert load_config(path).moderation_enabled is False


def test_groups_reject_general_as_specialist(tmp_path: Path) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(
        "groups:\n"
        "  - chat_id: -1001234567890\n"
        "    general_thread_id: 10\n"
        "    specialist_thread_ids: [10]\n"
        "topics:\n"
        "  - name: rock\n"
        "    chat_id: -1001234567890\n"
        "    message_thread_id: 10\n"
        "    genre: rock\n"
        "    hour: 9\n"
        "    minute: 0\n"
    )
    with pytest.raises(ValidationError, match="General"):
        load_topics(path)


@pytest.mark.parametrize(
    "content",
    [
        "topics: []\n",
        (
            "topics:\n"
            "  - name: rock\n"
            "    chat_id: -1001234567890\n"
            "    message_thread_id: 10\n"
            "    genre: rock\n"
            "    hour: 9\n"
            "    minute: 0\n"
            "    enabled: false\n"
        ),
    ],
)
def test_load_topics_requires_an_enabled_topic(tmp_path: Path, content: str) -> None:
    path = tmp_path / "topics.yml"
    path.write_text(content)
    with pytest.raises(ValidationError):
        load_topics(path)


def test_configuration_rejects_unknown_fields_and_template_placeholders(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra"):
        Settings(**valid_settings(tmp_path), unexpected=True, _env_file=None)
    with pytest.raises(ValidationError, match="extra"):
        TopicConfig(
            name="rock",
            chat_id=-1001234567890,
            message_thread_id=10,
            genre="rock",
            hour=9,
            minute=0,
            unexpected=True,
        )
    with pytest.raises(ValidationError, match="placeholder"):
        TopicConfig(
            name="rock",
            chat_id=-1001234567890,
            message_thread_id=10,
            genre="rock",
            hour=9,
            minute=0,
            request_template="{genre} {token}",
        )
    repeated = TopicConfig(
        name="rock",
        chat_id=-1001234567890,
        message_thread_id=10,
        genre="rock",
        hour=9,
        minute=0,
        request_template="{genre} / {genre}",
    )
    assert repeated.render_request() == "rock / rock"


def test_catalog_settings_are_strict_and_bounded(tmp_path: Path) -> None:
    settings = Settings(
        **valid_settings(tmp_path),
        soundcloud_catalog_path=tmp_path / "catalog.json",
        soundcloud_catalog_refresh_interval_seconds=21_600,
        soundcloud_catalog_refresh_timeout_seconds=120,
        soundcloud_candidate_attempts=4,
        chromium_executable_path=Path("/usr/bin/chromium"),
        _env_file=None,
    )

    assert settings.soundcloud_catalog_refresh_interval_seconds == 21_600
    assert settings.soundcloud_candidate_attempts == 4
    for field, value in [
        ("soundcloud_catalog_refresh_interval_seconds", 0),
        ("soundcloud_catalog_refresh_timeout_seconds", 0),
        ("soundcloud_candidate_attempts", 0),
    ]:
        values = {**valid_settings(tmp_path), field: value}
        with pytest.raises(ValidationError):
            Settings(**values, _env_file=None)


def test_example_enables_only_approved_rotation_topics() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "topics.example.yml")

    assert config.rotation_schedule == RotationSchedule(enabled=True)
    remix = next(topic for topic in config.topics if topic.name == "remix")
    assert remix.render_catalog_queries() == (
        "popular Persian remix",
        "popular international remix",
    )
    assert [topic.name for topic in config.topics if topic.enabled] == [
        "house",
        "rap_fa",
        "rock",
        "hiphop",
        "g_old",
        "pop",
        "classic_instrumental",
        "remix",
        "persian_pop_30",
        "arabic",
        "turkey",
    ]
