"""Telegram forum topics: one group, separate sections for VM, MARKETING and reports.

If the group has Topics turned on (Group settings → Topics), the bot creates
"🎨 VM", "📢 MARKETING" and "📊 Hisobotlar" itself (it needs the "Manage topics"
admin right) and remembers their ids. If Topics are off, everything goes to the
main chat as before.
"""
from __future__ import annotations

import logging

from aiogram import Bot

from .storage import Storage

log = logging.getLogger(__name__)

TOPIC_NAMES = {
    "VM": "🎨 VM",
    "MARKETING": "📢 MARKETING",
    "REPORT": "📊 Hisobotlar",
}
# Telegram's allowed topic icon colours
TOPIC_COLORS = {"VM": 0x6FB9F0, "MARKETING": 0xFF93B2, "REPORT": 0x8EEE98}

_forum_cache: dict[str, bool] = {}


def _key(chat_id, section: str) -> str:
    return f"topic:{chat_id}:{section}"


def is_thread_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "thread not found" in text or "topic_deleted" in text or "topic_closed" in text


async def is_forum(bot: Bot, chat_id) -> bool:
    key = str(chat_id)
    if key not in _forum_cache:
        try:
            chat = await bot.get_chat(chat_id)
            _forum_cache[key] = bool(getattr(chat, "is_forum", False))
        except Exception as exc:
            log.warning("get_chat(%s) failed: %s", chat_id, exc)
            return False
    return _forum_cache[key]


async def thread_for(bot: Bot, store: Storage, chat_id, section: str, enabled: bool = True) -> int | None:
    """message_thread_id for VM / MARKETING / REPORT in this chat, creating the topic once.
    None = post in the main chat (no topics, private chat, or no right to create topics)."""
    if not enabled or chat_id is None or not await is_forum(bot, chat_id):
        return None
    saved = store.get_kv(_key(chat_id, section))
    if saved:
        return int(saved)
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id, name=TOPIC_NAMES[section], icon_color=TOPIC_COLORS[section]
        )
    except Exception as exc:
        log.warning("Could not create topic %s in %s (give the bot 'Manage topics'): %s", section, chat_id, exc)
        return None
    store.set_kv(_key(chat_id, section), str(topic.message_thread_id))
    log.info("Created topic %s in %s: %s", TOPIC_NAMES[section], chat_id, topic.message_thread_id)
    return topic.message_thread_id


def forget(store: Storage, chat_id, section: str) -> None:
    """The topic was deleted in Telegram — create a new one next time."""
    store.del_kv(_key(chat_id, section))
