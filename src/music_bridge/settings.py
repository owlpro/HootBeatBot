"""Validated environment and YAML configuration."""

from __future__ import annotations

from pathlib import Path
from string import Formatter
from typing import Any
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
    catalog_query: str | None = Field(default=None, min_length=1)
    catalog_queries: list[str] | None = Field(default=None, min_length=1)
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

    def render_catalog_query(self) -> str:
        return self.render_catalog_queries()[0]

    def render_catalog_queries(self) -> tuple[str, ...]:
        if self.catalog_queries is not None:
            return tuple(self.catalog_queries)
        return (self.catalog_query or self.render_request(),)

    @model_validator(mode="after")
    def require_one_catalog_query_form(self) -> TopicConfig:
        if self.catalog_query is not None and self.catalog_queries is not None:
            raise ValueError("catalog_query and catalog_queries are mutually exclusive")
        if self.catalog_queries is not None:
            normalized = [query.strip() for query in self.catalog_queries]
            if any(not query for query in normalized) or len(normalized) != len(set(normalized)):
                raise ValueError("catalog_queries must be nonblank and unique")
            self.catalog_queries = normalized
        return self


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


class RotationSchedule(BaseModel):
    """Hourly minute slots shared by a fair rotation of enabled topics."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    timezone: str = "Asia/Tehran"
    minutes: list[int] = Field(default_factory=lambda: [5, 35], min_length=1)

    @field_validator("timezone")
    @classmethod
    def require_timezone(cls, value: str) -> str:
        return TopicConfig.require_timezone(value)

    @field_validator("minutes")
    @classmethod
    def require_sorted_unique_minutes(cls, value: list[int]) -> list[int]:
        if any(minute < 0 or minute > 59 for minute in value):
            raise ValueError("rotation minutes must be between 0 and 59")
        if value != sorted(set(value)):
            raise ValueError("rotation minutes must be sorted and unique")
        return value


class TopicsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[TopicConfig] = Field(min_length=1)
    rotation_schedule: RotationSchedule | None = None
    moderation_enabled: bool = False
    groups: list[GroupConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_topics(self) -> TopicsFile:
        names = [topic.name for topic in self.topics]
        routes = [(topic.chat_id, topic.message_thread_id) for topic in self.topics]
        if len(names) != len(set(names)) or len(routes) != len(set(routes)):
            raise ValueError("duplicate topic name or destination route")
        if not any(topic.enabled for topic in self.topics):
            if self.rotation_schedule is not None and self.rotation_schedule.enabled:
                raise ValueError("enabled rotation schedule requires at least one enabled topic")
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

    destination_bot_token: SecretStr
    topics_config_path: Path
    database_url: str = "sqlite+aiosqlite:///var/music_bridge.db"
    source_response_timeout_seconds: int = Field(default=180, gt=0, le=900)
    max_media_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    soundcloud_min_duration_seconds: int = Field(default=60, ge=30, le=600)
    soundcloud_search_limit: int = Field(default=50, ge=1, le=50)
    soundcloud_catalog_path: Path = Path("/data/state/soundcloud-catalog.json")
    soundcloud_catalog_refresh_interval_seconds: int = Field(default=21_600, ge=300, le=604_800)
    soundcloud_catalog_refresh_timeout_seconds: int = Field(default=120, ge=10, le=900)
    soundcloud_candidate_attempts: int = Field(default=3, ge=1, le=20)
    chromium_executable_path: Path = Path("/usr/bin/chromium")
    log_level: str = "INFO"

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
