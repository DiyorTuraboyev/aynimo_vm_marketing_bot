"""Run with:  python -m vm_marketing_bot   (from the AYNIMO_BOT folder)"""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import load_settings
from .group import router as group_router
from .handlers import App, router
from .reports import weekly_report_loop
from .sender import Sender
from .storage import Storage


def setup_logging(settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file = RotatingFileHandler(
        settings.data_dir / "bot.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8"
    )
    file.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)

    store = Storage(settings.data_dir / "vm_marketing.db")
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = App(settings=settings, store=store, sender=Sender(bot, store, settings))

    dp = Dispatcher(app=app)
    dp.include_router(group_router)  # first: keeps the group clean
    dp.include_router(router)

    if not settings.admin_ids:
        logging.warning("ADMIN_IDS is empty — nobody can use the bot. Send /chatid to the bot, "
                        "put your ID into ADMIN_IDS and restart.")

    await bot.set_my_commands([
        BotCommand(command="start", description="Bosh menyu"),
        BotCommand(command="load", description="Serverdagi Excel faylni ochish"),
        BotCommand(command="report", description="Shu haftaning hisoboti"),
        BotCommand(command="template", description="Bo'sh shablon"),
        BotCommand(command="clear", description="Shu haftaning belgilarini / test yuborishni tozalash"),
        BotCommand(command="cancel", description="Yuborishni to'xtatish"),
        BotCommand(command="chatid", description="Chat ID"),
    ])

    logging.info(
        "VM/Marketing bot started. group=%s, VM chat=%s, Marketing chat=%s, admins=%s, "
        "weekly report: %s day=%s at %s",
        settings.group_chat_id or "-",
        settings.target_chat("VM", "(sender chat)"),
        settings.target_chat("MARKETING", "(sender chat)"),
        len(settings.admin_ids) or "everyone",
        "on" if settings.report_enabled else "off",
        settings.report_weekday,
        settings.report_time.strftime("%H:%M"),
    )
    report_task = asyncio.create_task(weekly_report_loop(bot, app))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        report_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
