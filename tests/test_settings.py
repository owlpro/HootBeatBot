from pathlib import Path

import pytest
from pydantic import ValidationError

from music_bridge.settings import Settings, TopicConfig, load_config, load_topics


def valid_settings(tmp_path: Path) -> dict[str, object]:
    return {
        "telegram_web_profile_path": tmp_path / "telegram-profile",
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


@pytest.mark.parametrize("username", ["", "@", "bad name", "@channel", "@four"])
def test_settings_reject_invalid_source_bot_usernames(tmp_path: Path, username: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**valid_settings(tmp_path), source_bot_username=username, _env_file=None)


def test_settings_hide_secrets_and_default_source(tmp_path: Path) -> None:
    settings = Settings(**valid_settings(tmp_path), _env_file=None)
    assert settings.source_bot_username == "@melobot"
    assert settings.telegram_web_url == "https://web.telegram.org/k/"
    assert "secret-token" not in repr(settings)


def test_settings_reject_insecure_web_url_and_non_absolute_profile(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="official Telegram Web"):
        Settings(**valid_settings(tmp_path), telegram_web_url="https://example.com", _env_file=None)
    values = valid_settings(tmp_path)
    values["telegram_web_profile_path"] = Path("relative-profile")
    with pytest.raises(ValidationError, match="absolute"):
        Settings(**values, _env_file=None)


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
