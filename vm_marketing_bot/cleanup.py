"""Deleting old product posts from the group, and the Monday-morning routine:

  Monday 09:00 → 1) send last week's report again ("O'tgan hafta natijasi")
                 2) delete last week's product posts + department headers from the group.

Report messages are never deleted (they are not tracked for deletion at all).
History (posts, who confirmed what) stays in the database, so /report and Excel still work.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import Bot

from . import reports, topics
from .config import now
from .sender import tg_retry
from .storage import Storage

if TYPE_CHECKING:
    from .handlers import App

log = logging.getLogger(__name__)


async def delete_run_messages(bot: Bot, store: Storage, run_ids: list[int]) -> tuple[int, int]:
    """Deletes the product posts and headers of these sends from Telegram.
    Returns (deleted, failed)."""
    by_chat: dict[str, list[int]] = {}
    for post in store.posts_of_runs(run_ids):
        by_chat.setdefault(post["chat_id"], []).append(post["message_id"])
    for extra in store.run_messages(run_ids):
        by_chat.setdefault(extra["chat_id"], []).append(extra["message_id"])

    deleted = failed = 0
    for chat_id, ids in by_chat.items():
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                await tg_retry(lambda c=chat_id, m=chunk: bot.delete_messages(chat_id=c, message_ids=m))
                deleted += len(chunk)
                continue
            except Exception as exc:
                log.info("Bulk delete failed in %s (%s) — trying one by one", chat_id, exc)
            for message_id in chunk:  # find out exactly which ones can't be deleted
                try:
                    await tg_retry(lambda c=chat_id, m=message_id: bot.delete_message(chat_id=c, message_id=m))
                    deleted += 1
                except Exception:
                    failed += 1
                await asyncio.sleep(0.3)
    return deleted, failed


def _scheduled_moment(app: App):
    s = app.settings
    week_start, _ = reports.week_bounds(now())
    return week_start + timedelta(days=s.cleanup_weekday, hours=s.cleanup_time.hour, minutes=s.cleanup_time.minute)


async def maybe_monday_cleanup(bot: Bot, app: App) -> None:
    settings = app.settings
    if not settings.cleanup_enabled or now() < _scheduled_moment(app):
        return
    if app.busy:
        return  # a new week's send is running right now — try again next minute

    this_week, _ = reports.week_bounds(now())
    last_week = this_week - timedelta(days=7)

    # 1) Last week's results again (once).
    last_runs = app.store.runs_between(last_week, this_week)
    recap_ids = [r["id"] for r in last_runs if not r["recap_sent_at"]]
    if recap_ids:
        all_ids = [r["id"] for r in last_runs]
        target = settings.weekly_report_chat()
        chats = [target] if target is not None else app.store.run_chats(all_ids)
        title = "O'tgan hafta natijasi · " + reports.week_title(last_week).split("·", 1)[1].strip()
        for chat in dict.fromkeys(chats):
            try:
                await reports.send_report(bot, app, chat, all_ids, title)
            except Exception:
                log.exception("Monday recap to %s failed", chat)
        app.store.mark_recap_sent(recap_ids)

    # 2) Delete every older send's posts that are still in the group (also catches weeks the bot was offline).
    old_runs = app.store.runs_to_clean(this_week)
    if not old_runs:
        return
    run_ids = [r["id"] for r in old_runs]
    deleted, failed = await delete_run_messages(bot, app.store, run_ids)
    app.store.mark_cleaned(run_ids)
    log.info("Weekly cleanup: runs %s, deleted %s, failed %s", run_ids, deleted, failed)

    if failed:
        target = settings.weekly_report_chat()
        notify = [target] if target is not None else app.store.run_chats(run_ids)
        for chat in dict.fromkeys(notify):
            try:
                thread = await topics.thread_for(bot, app.store, chat, "REPORT", settings.use_topics)
                await bot.send_message(
                    chat,
                    message_thread_id=thread,
                    text=
                    f"⚠️ O'tgan haftaning {failed} ta postini o'chirib bo'lmadi.\n"
                    "Botni guruhda <b>admin</b> qiling va <b>\"Delete messages\"</b> huquqini bering — "
                    "Telegram 48 soatdan eski xabarlarni faqat shunda o'chirishga ruxsat beradi.",
                )
            except Exception:
                log.exception("Cleanup warning to %s failed", chat)
