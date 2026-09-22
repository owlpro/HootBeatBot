from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.enums import ContentType

from music_bridge.moderation import ForumModerator, is_media_message
from music_bridge.settings import GroupConfig


def message(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "chat": SimpleNamespace(id=-1001234567890),
        "message_id": 42,
        "message_thread_id": 10,
        "from_user": SimpleNamespace(id=7, is_bot=False),
        "content_type": "text",
        "photo": None,
        "video": None,
        "audio": None,
        "document": None,
        "voice": None,
        "video_note": None,
        "animation": None,
        "sticker": None,
        "paid_media": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "field",
    [
        "photo",
        "video",
        "audio",
        "document",
        "voice",
        "video_note",
        "animation",
        "sticker",
        "paid_media",
    ],
)
def test_media_classification_includes_all_supported_media(field: str) -> None:
    assert is_media_message(message(**{field: object()}))


def test_text_is_not_media() -> None:
    assert not is_media_message(message())


DEFAULT_RESULT = object()


class FakeBot:
    def __init__(self, result: Any = DEFAULT_RESULT, error: Exception | None = None):
        self.result = SimpleNamespace(message_id=99) if result is DEFAULT_RESULT else result
        self.error = error
        self.events: list[tuple[str, dict[str, int]]] = []

    async def forward_message(self, **kwargs: int) -> Any:
        self.events.append(("forward", kwargs))
        if self.error:
            raise self.error
        return self.result

    async def delete_message(self, **kwargs: int) -> bool:
        self.events.append(("delete", kwargs))
        return True


def moderator(bot: FakeBot) -> ForumModerator:
    return ForumModerator(
        bot,
        [
            GroupConfig(
                chat_id=-1001234567890,
                general_thread_id=1,
                specialist_thread_ids=[10, 11],
            )
        ],
        bot_id=100,
    )


@pytest.mark.asyncio
async def test_forwards_to_exact_general_topic_then_deletes_original() -> None:
    bot = FakeBot()
    handled = await moderator(bot).handle(message())
    assert handled
    assert bot.events == [
        (
            "forward",
            {
                "chat_id": -1001234567890,
                "from_chat_id": -1001234567890,
                "message_id": 42,
                "message_thread_id": 1,
            },
        ),
        ("delete", {"chat_id": -1001234567890, "message_id": 42}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ignored",
    [
        message(message_thread_id=1),
        message(message_thread_id=999),
        message(from_user=SimpleNamespace(id=8, is_bot=True)),
        message(from_user=SimpleNamespace(id=100, is_bot=False)),
        message(content_type="forum_topic_created"),
        message(content_type=ContentType.FORUM_TOPIC_CREATED),
        message(photo=[object()]),
    ],
)
async def test_ignores_general_other_topics_bots_self_service_and_media(ignored: Any) -> None:
    bot = FakeBot()
    assert not await moderator(bot).handle(ignored)
    assert bot.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot",
    [
        FakeBot(error=RuntimeError("forward failed")),
        FakeBot(result=SimpleNamespace(message_id=0)),
        FakeBot(result=SimpleNamespace()),
    ],
)
async def test_never_deletes_when_forward_fails_or_result_is_invalid(bot: FakeBot) -> None:
    with pytest.raises(RuntimeError):
        await moderator(bot).handle(message())
    assert all(event[0] != "delete" for event in bot.events)
