"""Validated environment and YAML configuration."""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TopicConfig(BaseModel):
    """One destination topic and its source query schedule."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    chat_id: int
    message_thread_id: int = Field(gt=0)
    genre: str = Field(min_length=1)
    request_template: str = "random {genre}"
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    timezone: str = "Asia/Tehran"
    enabled: bool = True

    @field_validator("chat_id")
    @classmethod
    def require_supergroup_id(cls, value: int) -> int:
        if not str(value).startswith("-100"):
            raise ValueError("chat_id must be a Telegram supergroup ID beginning with -100")
        return value

    @field_validator("timezone")
    @classmethod
    def require_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("request_template")
    @classmethod
    def require_genre_placeholder(cls, value: str) -> str:
        try:
            fields = [field for _, field, _, _ in Formatter().parse(value) if field is not None]
        except ValueError as exc:
            raise ValueError("request_template has invalid formatting") from exc
        if not fields or set(fields) != {"genre"}:
            raise ValueError("request_template must contain only the {genre} placeholder")
        return value

    def render_request(self) -> str:
        return self.request_template.format(genre=self.genre)


class GroupConfig(BaseModel):
    """Forum topics whose user text is redirected to General."""

    model_config = ConfigDict(extra="forbid")

    chat_id: int
    general_thread_id: int = Field(gt=0)
    specialist_thread_ids: list[int] = Field(min_length=1)

    @field_validator("chat_id")
    @classmethod
    def require_supergroup_id(cls, value: int) -> int:
        return TopicConfig.require_supergroup_id(value)

    @model_validator(mode="after")
    def distinct_threads(self) -> GroupConfig:
        if self.general_thread_id in self.specialist_thread_ids:
            raise ValueError("General topic cannot also be a specialist topic")
        if len(self.specialist_thread_ids) != len(set(self.specialist_thread_ids)):
            raise ValueError("duplicate specialist topic")
        return self


class TopicsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[TopicConfig] = Field(min_length=1)
    groups: list[GroupConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_topics(self) -> TopicsFile:
        names = [topic.name for topic in self.topics]
        routes = [(topic.chat_id, topic.message_thread_id) for topic in self.topics]
        if len(names) != len(set(names)) or len(routes) != len(set(routes)):
            raise ValueError("duplicate topic name or destination route")
        if not any(topic.enabled for topic in self.topics):
            raise ValueError("at least one topic must be enabled")
        group_chats = [group.chat_id for group in self.groups]
        if len(group_chats) != len(set(group_chats)):
            raise ValueError("duplicate moderation group")
        return self


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="", case_sensitive=False, extra="forbid"
    )

    telegram_web_profile_path: Path
    telegram_web_url: str = "https://web.telegram.org/k/"
    telegram_login_screenshot_path: Path | None = None
    destination_bot_token: SecretStr
    topics_config_path: Path
    source_bot_username: str = "@melobot"

    database_url: str = "sqlite+aiosqlite:///var/music_bridge.db"
    source_response_timeout_seconds: int = Field(default=120, gt=0, le=600)
    max_media_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    log_level: str = "INFO"

    @field_validator("source_bot_username")
    @classmethod
    def normalize_source_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("@"):
            normalized = f"@{normalized}"
        if re.fullmatch(
            r"@[A-Za-z0-9_]{5,32}", normalized
        ) is None or not normalized.lower().endswith("bot"):
            raise ValueError("source_bot_username must be a valid Telegram bot username")
        return normalized

    @field_validator("telegram_web_profile_path", "telegram_login_screenshot_path")
    @classmethod
    def require_absolute_private_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("private paths must be absolute")
        return value

    @field_validator("telegram_web_url")
    @classmethod
    def require_official_web_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "web.telegram.org":
            raise ValueError("telegram_web_url must use official Telegram Web over HTTPS")
        return value

    @field_validator("destination_bot_token", mode="before")
    @classmethod
    def require_nonblank_secret(cls, value: Any) -> Any:
        secret = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("credential must not be blank")
        return value


def load_topics(path: Path) -> list[TopicConfig]:
    """Load and strictly validate topic mappings from YAML."""
    return load_config(path).topics


def load_config(path: Path) -> TopicsFile:
    """Load the complete schedule and forum-moderation configuration."""
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return TopicsFile.model_validate(raw)
