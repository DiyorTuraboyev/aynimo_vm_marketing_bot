"""Weekly "how much was done" report: Telegram text + Excel file, and the Sunday scheduler."""
from __future__ import annotations

import asyncio
import io
import logging
from collections import Counter
from datetime import datetime, timedelta
from html import escape
from typing import TYPE_CHECKING

import pandas as pd
from aiogram import Bot
from aiogram.types import BufferedInputFile

from .captions import TEXT_LIMIT
from .config import DEPARTMENT_TITLES, DEPARTMENTS, now

if TYPE_CHECKING:
    from .handlers import App

log = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 60


def week_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """Monday 00:00 of the week containing `moment`, and the next Monday 00:00."""
    start = (moment - timedelta(days=moment.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def _percent(done: int, total: int) -> str:
    return f"{round(done * 100 / total)}%" if total else "—"


def _bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "🟩" * filled + "⬜" * (width - filled)


def build_report(app: App, run_ids: list[int], title: str) -> tuple[str, bytes | None]:
    store = app.store
    rows = [dict(r) for r in store.report_rows(run_ids)]
    if not rows:
        return f"📊 <b>{escape(title)}</b>\n\nBu davrda hech narsa yuborilmagan.", None

    df = pd.DataFrame(rows)
    lines = [f"📊 <b>{escape(title)}</b>", ""]

    for dept in DEPARTMENTS:
        part = df[df["department"] == dept]
        if part.empty:
            continue
        icon, name = DEPARTMENT_TITLES[dept]
        done = int((part["done_count"] > 0).sum())
        total = len(part)
        lines.append(f"{icon} <b>{name}</b>: {done}/{total} bajarildi ({_percent(done, total)})")
        lines.append(_bar(done, total))

    for dept in DEPARTMENTS:
        pending = df[(df["department"] == dept) & (df["done_count"] == 0)]
        if pending.empty:
            continue
        icon, name = DEPARTMENT_TITLES[dept]
        lines.append("")
        lines.append(f"⏳ <b>{name} — bajarilmagan ({len(pending)}):</b>")
        for _, r in pending.head(15).iterrows():
            label = r["model"] or ""
            if r["name"] and r["name"] != r["model"]:
                label = f"{label} — {r['name']}" if label else r["name"]
            lines.append(f"• {escape(str(label))[:90]}")
        if len(pending) > 15:
            lines.append(f"… yana {len(pending) - 15} ta (Excel faylda)")

    people = Counter()
    for c in store.confirmation_rows(run_ids):
        people[(c["user_name"] or str(c["user_id"]), c["department"])] += 1
    if people:
        lines.append("")
        lines.append("👥 <b>Kim qancha bajardi:</b>")
        for (person, dept), count in people.most_common(15):
            icon, _ = DEPARTMENT_TITLES.get(dept, ("•", dept))
            lines.append(f"{icon} {escape(person)} — {count} ta")

    text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[: text.rfind("\n", 0, TEXT_LIMIT - 10)] + "\n…"

    return text, _excel(df)


def _excel(df: pd.DataFrame) -> bytes:
    df = df.rename(columns={
        "run_id": "Run", "week": "Hafta", "department": "Bo'lim", "excel_row": "Excel qator",
        "model": "Модель", "name": "Наименование", "note": "Izoh", "chat_id": "Chat",
        "message_id": "Xabar ID", "created_at": "Yuborilgan", "done_count": "Bajarganlar soni",
        "done_by": "Kim bajardi",
    })
    df.insert(
        df.columns.get_loc("Bajarganlar soni"), "Holat",
        df["Bajarganlar soni"].map(lambda n: "✅ Bajarildi" if n else "⏳ Bajarilmagan"),
    )
    summary = (
        df.groupby("Bo'lim")
        .agg(Jami=("Holat", "size"), Bajarildi=("Bajarganlar soni", lambda s: int((s > 0).sum())))
        .reset_index()
    )
    summary["Foiz"] = (summary["Bajarildi"] * 100 / summary["Jami"]).round().astype(int).astype(str) + "%"

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Xulosa")
        df.to_excel(writer, index=False, sheet_name="Mahsulotlar")
        for sheet in writer.sheets.values():
            for col in sheet.columns:
                width = max(len(str(c.value or "")) for c in col)
                sheet.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
            sheet.freeze_panes = "A2"
    return buf.getvalue()


async def send_report(bot: Bot, app: App, chat_id, run_ids: list[int], title: str) -> None:
    text, data = await asyncio.to_thread(build_report, app, run_ids, title)
    await bot.send_message(chat_id, text)
    if data:
        name = f"hisobot_{now():%Y%m%d_%H%M}.xlsx"
        await bot.send_document(chat_id, BufferedInputFile(data, filename=name))


def week_title(start: datetime) -> str:
    end = start + timedelta(days=6)
    return f"Haftalik hisobot · {start.isocalendar().week}-hafta ({start:%d.%m}–{end:%d.%m.%Y})"


async def send_week_report(bot: Bot, app: App, chat_id, moment: datetime | None = None) -> bool:
    start, end = week_bounds(moment or now())
    runs = app.store.runs_between(start, end)
    await send_report(bot, app, chat_id, [r["id"] for r in runs], week_title(start))
    return bool(runs)


async def maybe_send_weekly_report(bot: Bot, app: App) -> None:
    settings = app.settings
    moment = now()
    if not settings.report_enabled:
        return
    if moment.weekday() != settings.report_weekday or moment.time() < settings.report_time:
        return

    start, end = week_bounds(moment)
    runs = app.store.runs_between(start, end)
    if not runs or all(r["report_sent_at"] for r in runs):
        return  # nothing sent this week, or already reported

    run_ids = [r["id"] for r in runs]
    target = settings.weekly_report_chat()
    chats = [target] if target is not None else app.store.run_chats(run_ids)
    for chat in dict.fromkeys(chats):
        try:
            await send_report(bot, app, chat, run_ids, week_title(start))
        except Exception:
            log.exception("Weekly report to %s failed", chat)
    app.store.mark_report_sent(run_ids)
    log.info("Weekly report sent for runs %s", run_ids)


async def weekly_report_loop(bot: Bot, app: App) -> None:
    """Checks every minute; sends once per week on REPORT_DAY after REPORT_TIME.
    If the bot was offline at that moment it sends as soon as it starts (same day)."""
    while True:
        try:
            await maybe_send_weekly_report(bot, app)
        except Exception:
            log.exception("Weekly report check failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
