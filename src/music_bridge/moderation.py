"""Forum-topic moderation: redirect user text to each group's General topic."""

from __future__ import annotations

from typing import Any, Protocol

from music_bridge.settings import GroupConfig

MEDIA_FIELDS = (
    "photo",
    "video",
    "audio",
    "document",
    "voice",
    "video_note",
    "animation",
    "sticker",
    "paid_media",
)

# Telegram Bot API message variants that represent chat state rather than user content.
SERVICE_CONTENT_TYPES = {
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "forum_topic_created",
    "forum_topic_closed",
    "forum_topic_reopened",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "video_chat_scheduled",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "proximity_alert_triggered",
    "boost_added",
    "chat_background_set",
    "successful_payment",
    "refunded_payment",
    "write_access_allowed",
    "users_shared",
    "chat_shared",
    "giveaway_created",
    "giveaway",
    "giveaway_winners",
    "giveaway_completed",
}


class ModerationBot(Protocol):
    async def forward_message(self, **kwargs: int) -> Any: ...

    async def delete_message(self, **kwargs: int) -> Any: ...


def is_media_message(message: Any) -> bool:
    """Classify all current Bot API media, including optional paid media."""
    return any(getattr(message, field, None) is not None for field in MEDIA_FIELDS)


class ForumModerator:
    """Forward eligible specialist-topic text, then delete only after confirmation."""

    def __init__(self, bot: ModerationBot, groups: list[GroupConfig], *, bot_id: int) -> None:
        self._bot = bot
        self._groups = {group.chat_id: group for group in groups}
        self._bot_id = bot_id

    async def handle(self, message: Any) -> bool:
        chat_id = int(message.chat.id)
        group = self._groups.get(chat_id)
        thread_id = getattr(message, "message_thread_id", None)
        sender = getattr(message, "from_user", None)
        content_type = getattr(message, "content_type", "")
        content_type_value = getattr(content_type, "value", content_type)
        if (
            group is None
            or thread_id == group.general_thread_id
            or thread_id not in group.specialist_thread_ids
            or sender is None
            or bool(getattr(sender, "is_bot", False))
            or int(getattr(sender, "id", 0)) == self._bot_id
            or content_type_value in SERVICE_CONTENT_TYPES
            or is_media_message(message)
        ):
            return False

        forwarded = await self._bot.forward_message(
            chat_id=chat_id,
            from_chat_id=chat_id,
            message_id=int(message.message_id),
            message_thread_id=group.general_thread_id,
        )
        forwarded_id = getattr(forwarded, "message_id", None)
        if not isinstance(forwarded_id, int) or forwarded_id <= 0:
            raise RuntimeError("moderation_forward_not_confirmed")
        await self._bot.delete_message(chat_id=chat_id, message_id=int(message.message_id))
        return True
