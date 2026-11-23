"""Keeps the products group clean.

In the group the bot only posts products and reports. Everything else is removed:
  * service notices — "X joined", "X left", pinned message, title/photo changes;
  * commands typed in the group (/start, /report, ...) — admins use the bot in private chat.
    /chatid still answers (needed for setup), and both messages disappear after 1 minute.

The bot must be an admin with the "Delete messages" right. Telegram does not let bots
read a group's old history, so only messages arriving from now on can be cleaned.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

log = logging.getLogger(__name__)

router = Router(name="group_cleanup")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

CHATID_REPLY_SECONDS = 60

SERVICE = (
    F.new_chat_members | F.left_chat_member | F.pinned_message | F.new_chat_title
    | F.new_chat_photo | F.delete_chat_photo | F.group_chat_created | F.message_auto_delete_timer_changed
    | F.video_chat_started | F.video_chat_ended | F.video_chat_scheduled
    | F.video_chat_participants_invited
)


async def _delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:  # no rights / already deleted
        log.debug("Could not delete %s in %s: %s", message_id, chat_id, exc)


async def _delete_later(bot: Bot, chat_id: int, message_ids: list[int], seconds: int) -> None:
    await asyncio.sleep(seconds)
    for message_id in message_ids:
        await _delete(bot, chat_id, message_id)


@router.message(SERVICE)
async def remove_service_message(message: Message, bot: Bot) -> None:
    await _delete(bot, message.chat.id, message.message_id)


@router.message(F.text.regexp(r"^/chatid(@\w+)?(\s|$)"))
async def chatid_in_group(message: Message, bot: Bot) -> None:
    user = message.from_user
    reply = await message.answer(
        f"Chat ID: <code>{message.chat.id}</code>\n"
        + (f"Sizning ID: <code>{user.id}</code>\n" if user else "")
        + f"<i>Bu xabar {CHATID_REPLY_SECONDS} soniyada o'chadi.</i>"
    )
    asyncio.create_task(
        _delete_later(bot, message.chat.id, [message.message_id, reply.message_id], CHATID_REPLY_SECONDS)
    )


@router.message(F.text.startswith("/"))
async def remove_command(message: Message, bot: Bot) -> None:
    """Commands are not used in the group — remove them so only products stay.
    The sender gets a short-lived button that opens the private chat with the bot."""
    from .reports import bot_chat_keyboard

    await _delete(bot, message.chat.id, message.message_id)
    markup = await bot_chat_keyboard(bot)
    if markup is None:
        return
    try:
        reply = await bot.send_message(
            message.chat.id,
            f"Buyruqlar bot bilan shaxsiy chatda ishlaydi 👇\n<i>Bu xabar {CHATID_REPLY_SECONDS} soniyada o'chadi.</i>",
            reply_markup=markup,
            message_thread_id=getattr(message, "message_thread_id", None),
        )
    except Exception as exc:
        log.debug("Could not send bot link in %s: %s", message.chat.id, exc)
        return
    asyncio.create_task(_delete_later(bot, message.chat.id, [reply.message_id], CHATID_REPLY_SECONDS))
