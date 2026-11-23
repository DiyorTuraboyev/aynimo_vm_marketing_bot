"""Telegram handlers: upload/preview/send flow, "Bajarildi" buttons, reports."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .captions import CAPTION_LIMIT, TEXT_LIMIT, preview_text, week_label, with_confirmations
from . import reports
from .config import DEPARTMENT_TITLES, DEPARTMENTS, Settings, now
from .excel import ExcelFormatError, LoadResult, load_products
from .sender import Sender, SendJob, done_keyboard, tg_retry
from .storage import Storage

log = logging.getLogger(__name__)
router = Router(name="vm_marketing")

MAX_UPLOAD_BYTES = 10_000_000  # a weekly Excel is ~15 KB; 10 MB means pictures were pasted inside


@dataclass
class App:
    settings: Settings
    store: Storage
    sender: Sender
    loaded: dict[int, LoadResult] = field(default_factory=dict)
    job: SendJob | None = None
    task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


# ---------------------------------------------------------------------------
# keyboards & helpers
# ---------------------------------------------------------------------------

def start_keyboard(app: App) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📂 Oxirgi faylni ochish ({latest_excel(app).name[-40:]})",
                callback_data="load:default",
            )],
            [InlineKeyboardButton(text="📊 Shu haftaning hisoboti", callback_data="report:week")],
        ]
    )


def preview_keyboard(result: LoadResult) -> InlineKeyboardMarkup:
    vm, mk = result.count("VM"), result.count("MARKETING")
    rows = []
    if vm and mk:
        rows.append([InlineKeyboardButton(text=f"🚀 Hammasini yuborish ({vm + mk})", callback_data="ask:ALL")])
    dept_row = []
    if vm:
        dept_row.append(InlineKeyboardButton(text=f"🎨 Faqat VM ({vm})", callback_data="ask:VM"))
    if mk:
        dept_row.append(InlineKeyboardButton(text=f"📢 Faqat Marketing ({mk})", callback_data="ask:MARKETING"))
    if dept_row:
        rows.append(dept_row)
    rows.append([
        InlineKeyboardButton(text="👁 Namuna (menga)", callback_data="sample"),
        InlineKeyboardButton(text="❌ Bekor", callback_data="close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(what: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, yuborish", callback_data=f"go:{what}"),
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back"),
        ]]
    )


def departments_for(what: str) -> list[str]:
    return list(DEPARTMENTS) if what == "ALL" else [what]


async def describe_chat(bot: Bot, chat_id, here: int) -> str:
    if chat_id == here:
        return "shu chat"
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or chat.full_name or str(chat_id)
    except Exception:
        return f"{chat_id} ⚠️ bot bu chatni ko'rmayapti"


async def targets_text(app: App, bot: Bot, here: int) -> dict[str, str]:
    return {d: await describe_chat(bot, app.settings.target_chat(d, here), here) for d in DEPARTMENTS}


async def deny(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("⛔ Bu bot faqat adminlar uchun.", show_alert=True)
    elif event.chat.type == "private":
        user_id = event.from_user.id if event.from_user else "?"
        await event.answer(
            "⛔ Bu bot faqat adminlar uchun.\n"
            f"Sizning ID: <code>{user_id}</code> — admin bo'lishingiz kerak bo'lsa, shu raqamni adminga yuboring."
        )


KEEP_UPLOADS = 8  # weekly files kept on the server (512 MB disk) — older ones are deleted


def uploads_dir(app: App):
    return app.settings.data_dir / "uploads"


def latest_excel(app: App):
    """The most recently uploaded weekly file, or the template shipped with the bot."""
    files = sorted(uploads_dir(app).glob("*.xls*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else app.settings.excel_file


def cleanup_uploads(app: App) -> None:
    files = sorted(uploads_dir(app).glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_UPLOADS:]:
        try:
            old.unlink()
        except OSError as exc:
            log.warning("Could not delete %s: %s", old, exc)


async def show_preview(app: App, bot: Bot, user_id: int, chat_id: int, path, edit: Message | None = None) -> bool:
    """Reads the Excel and shows the summary. Returns False if the file could not be read."""
    try:
        result = await asyncio.to_thread(load_products, path)
    except (ExcelFormatError, FileNotFoundError) as exc:
        await bot.send_message(chat_id, f"❌ {escape(str(exc))}")
        return False
    except Exception as exc:
        log.exception("Excel read failed")
        await bot.send_message(chat_id, f"❌ Excelni o'qib bo'lmadi:\n<code>{escape(str(exc))}</code>")
        return False

    app.loaded[user_id] = result
    text = preview_text(result, week_label(), await targets_text(app, bot, chat_id))
    if not result.products:
        text += "\n\n❗ Yuboriladigan mahsulot topilmadi."
    keyboard = preview_keyboard(result)
    if edit is not None:
        try:
            await edit.edit_text(text, reply_markup=keyboard)
            return True
        except TelegramBadRequest:
            pass
    await bot.send_message(chat_id, text, reply_markup=keyboard)
    return True


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

HELP = (
    "👋 <b>VM & Marketing bot</b>\n\n"
    "Har <b>dushanba</b>:\n"
    "1️⃣ VM_MARKETING Excel faylni shu chatga yuboring\n"
    "2️⃣ Bot faylni tekshirib, xulosa ko'rsatadi\n"
    "3️⃣ <b>👁 Namuna</b> bilan dizaynni ko'ring → <b>🚀 Hammasini yuborish</b>\n"
    "4️⃣ VM va Marketing xodimlari o'z postlari ostidagi <b>✅ Bajarildi</b> tugmasini bosadi\n"
    "5️⃣ <b>Yakshanba</b> kuni bot guruhga haftalik hisobot yuboradi\n"
    "6️⃣ <b>Dushanba 09:00</b> — o'tgan hafta hisoboti qayta yuboriladi va o'tgan haftaning "
    "postlari guruhdan o'chiriladi (hisobotlar o'chirilmaydi)\n\n"
    "<b>Department</b> ustuni: <code>VM</code> yoki <code>MARKETING</code> "
    "(ikkalasi uchun <code>VM/MARKETING</code>).\n"
    "<b>Comment</b> ustuni — postda izoh sifatida chiqadi.\n\n"
    "Buyruqlar:\n"
    "/load — serverdagi faylni ochish\n"
    "/report — shu haftaning hisoboti; /report 4 — oxirgi 4 ta yuborish\n"
    "/template — bo'sh shablon fayl\n"
    "/clear — belgilarni, takroriy postlarni yoki test yuborishni tozalash\n"
    "/cancel — yuborishni to'xtatish\n"
    "/chatid — chat ID ni bilish"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, app: App) -> None:
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)
    await message.answer(HELP, reply_markup=start_keyboard(app))


@router.message(Command("chatid"))
async def cmd_chatid(message: Message, app: App) -> None:
    user = message.from_user
    text = f"Chat ID: <code>{message.chat.id}</code>\nChat turi: {message.chat.type}\n"
    if user:
        text += f"Sizning ID: <code>{user.id}</code>\n"
        dept = app.settings.department_of(user.id)
        roles = (["admin"] if user.id in app.settings.admin_ids else []) + ([dept] if dept else [])
        text += "Rolingiz: " + (", ".join(roles) if roles else "—")
    await message.answer(text)


@router.message(Command("load"))
async def cmd_load(message: Message, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)
    await show_preview(app, bot, message.from_user.id, message.chat.id, latest_excel(app))


@router.message(Command("template"))
async def cmd_template(message: Message, app: App) -> None:
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)
    path = app.settings.excel_file
    if not path.exists():
        await message.answer("Shablon fayl serverda topilmadi.")
        return
    await message.answer_document(FSInputFile(path), caption="📄 VM / Marketing shablon")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, app: App) -> None:
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)
    if app.busy and app.job:
        app.job.cancelled = True
        await message.answer("⛔ To'xtatilmoqda...")
    else:
        await message.answer("Hozir hech narsa yuborilmayapti.")


@router.message(F.document)
async def on_document(message: Message, bot: Bot, app: App) -> None:
    if message.chat.type != "private":
        return  # the group stays clean: Excel files are sent to the bot in private chat
    doc = message.document
    name = doc.file_name or "file.xlsx"
    if not name.lower().endswith((".xlsx", ".xlsm")):
        if message.chat.type == "private":
            await message.answer("Faqat Excel (.xlsx) fayl yuboring.")
        return
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)

    stamp = now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer(
            f"❌ Fayl juda katta ({doc.file_size // 1_000_000} MB). "
            "Rasmlarni Excel ichidan olib tashlang — bot rasmlarni PHOTO_PATH orqali o'zi oladi."
        )
        return
    path = uploads_dir(app) / f"{stamp}_{safe}"
    await bot.download(doc, destination=path)
    if await show_preview(app, bot, message.from_user.id, message.chat.id, path):
        cleanup_uploads(app)
    else:
        path.unlink(missing_ok=True)  # unreadable file — don't keep it on the small disk


# ---------------------------------------------------------------------------
# preview / send callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "load:default")
async def cb_load_default(callback: CallbackQuery, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    await callback.answer("Fayl o'qilmoqda...")
    await show_preview(
        app, bot, callback.from_user.id, callback.message.chat.id,
        latest_excel(app), edit=callback.message,
    )


@router.callback_query(F.data.startswith("ask:"))
async def cb_ask(callback: CallbackQuery, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    result = app.loaded.get(callback.from_user.id)
    if not result:
        await callback.answer("Fayl eskirgan, qaytadan yuklang.", show_alert=True)
        return
    what = callback.data.split(":", 1)[1]
    here = callback.message.chat.id
    lines = ["❓ <b>Rostdan ham yuborasizmi?</b>", ""]
    for dept in departments_for(what):
        icon, title = DEPARTMENT_TITLES[dept]
        target = await describe_chat(bot, app.settings.target_chat(dept, here), here)
        lines.append(f"{icon} {title}: <b>{result.count(dept)}</b> ta → {escape(target)}")
    await callback.message.edit_text("\n".join(lines), reply_markup=confirm_keyboard(what))
    await callback.answer()


@router.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    result = app.loaded.get(callback.from_user.id)
    if not result:
        await callback.answer("Fayl eskirgan, qaytadan yuklang.", show_alert=True)
        return
    here = callback.message.chat.id
    text = preview_text(result, week_label(), await targets_text(app, bot, here))
    await callback.message.edit_text(text, reply_markup=preview_keyboard(result))
    await callback.answer()


@router.callback_query(F.data == "close")
async def cb_close(callback: CallbackQuery, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    app.loaded.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ Bekor qilindi.")
    await callback.answer()


async def _start_job(callback: CallbackQuery, app: App, departments: list[str], sample: bool) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    result = app.loaded.get(callback.from_user.id)
    if not result:
        await callback.answer("Fayl eskirgan, qaytadan yuklang.", show_alert=True)
        return
    if app.busy:
        await callback.answer("Boshqa yuborish davom etmoqda. /cancel bilan to'xtating.", show_alert=True)
        return

    here = callback.message.chat.id
    departments = [d for d in departments if result.count(d)]
    targets = {d: (here if sample else app.settings.target_chat(d, here)) for d in departments}
    job = SendJob(
        result=result,
        departments=departments,
        targets=targets,
        report_chat_id=here,
        started_by=callback.from_user.full_name,
        week=week_label(),
        sample=sample,
    )
    app.job = job
    await callback.answer("Boshlandi")

    progress = None
    if not sample:
        progress = await callback.message.edit_text("⏳ Yuborish boshlanmoqda...")
        if not isinstance(progress, Message):
            progress = callback.message

    async def runner() -> None:
        try:
            await app.sender.run(job, progress)
        except Exception as exc:
            log.exception("Send job crashed")
            try:
                await callback.bot.send_message(here, f"❌ Kutilmagan xatolik: <code>{escape(str(exc))}</code>")
            except Exception:
                pass

    app.task = asyncio.create_task(runner())


@router.callback_query(F.data.startswith("go:"))
async def cb_go(callback: CallbackQuery, app: App) -> None:
    await _start_job(callback, app, departments_for(callback.data.split(":", 1)[1]), sample=False)


@router.callback_query(F.data == "sample")
async def cb_sample(callback: CallbackQuery, app: App) -> None:
    await _start_job(callback, app, list(DEPARTMENTS), sample=True)


@router.callback_query(F.data == "job:cancel")
async def cb_cancel(callback: CallbackQuery, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    if app.busy and app.job:
        app.job.cancelled = True
        await callback.answer("To'xtatilmoqda...")
    else:
        await callback.answer("Yuborish allaqachon tugagan.")


# ---------------------------------------------------------------------------
# "✅ Bajarildi" button under each post
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("done:") | F.data.startswith("undone:"))
async def cb_done(callback: CallbackQuery, app: App) -> None:
    """'✅ Bajarildi' marks the post done; '❌ Bajarilmadi' takes the mark back.
    Two separate actions (not a toggle), so repeated or queued taps can't flip it back and forth."""
    action, _, raw_id = callback.data.partition(":")
    try:
        post_id = int(raw_id)
    except ValueError:
        await callback.answer()
        return
    post = app.store.get_post(post_id)
    if post is None:
        await callback.answer("Bu post bazada topilmadi.", show_alert=True)
        return

    user = callback.from_user
    department = post["department"]
    if not app.settings.can_confirm(department, user.id):
        icon, title = DEPARTMENT_TITLES.get(department, ("", department))
        await safe_answer(callback, f"⛔ Bu {title} bo'limining vazifasi. Faqat {title} xodimlari belgilaydi.", alert=True)
        return
    if action == "done":
        app.store.add_confirmation(post_id, user.id, user.full_name)
        answer = "✅ Bajarildi deb belgilandi, rahmat!"
    else:
        app.store.clear_post_confirmations(post_id)
        log.info("Post %s marked NOT done by %s (%s)", post_id, user.full_name, user.id)
        answer = "❌ Bajarilmadi — belgi olib tashlandi"

    text, keyboard = _post_view(app, post)
    try:
        if post["has_photo"]:
            await callback.message.edit_caption(caption=text, reply_markup=keyboard)
        else:
            await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Could not update post %s: %s", post_id, exc)
    await safe_answer(callback, answer)


def _post_view(app: App, post) -> tuple[str, InlineKeyboardMarkup]:
    """Caption with '✅ Bajardi: ...' and the matching button for the post's current state."""
    people = [
        (row["user_name"], datetime.strptime(row["confirmed_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M"))
        for row in app.store.confirmations(post["id"])
    ]
    limit = CAPTION_LIMIT if post["has_photo"] else TEXT_LIMIT
    text = with_confirmations(post["caption"] or "", people, limit)
    return text, done_keyboard(post["id"], done=bool(people))


async def refresh_post(bot: Bot, app: App, post_id: int) -> None:
    """Re-draw a post in the group with its current '✅ Bajardi: ...' list."""
    post = app.store.get_post(post_id)
    if post is None or not post["message_id"]:
        return
    text, keyboard = _post_view(app, post)
    try:
        if post["has_photo"]:
            await tg_retry(lambda: bot.edit_message_caption(
                chat_id=post["chat_id"], message_id=post["message_id"], caption=text, reply_markup=keyboard))
        else:
            await tg_retry(lambda: bot.edit_message_text(
                text=text, chat_id=post["chat_id"], message_id=post["message_id"], reply_markup=keyboard))
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Could not update post %s: %s", post_id, exc)


async def safe_answer(callback: CallbackQuery, text: str, alert: bool = False) -> None:
    # Presses delivered after a bot restart are "too old" to answer — the mark is
    # already saved, so just ignore that error.
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject, bot: Bot, app: App) -> None:
    """/report — this week's report;  /report 3 — the last 3 sends."""
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)
    args = (command.args or "").strip()
    count = int(args) if args.isdigit() else 0
    await _send_report(bot, app, message.chat.id, count)
    await _offer_group(message.chat, bot, app, count)


async def _send_report(bot: Bot, app: App, chat_id, count: int) -> None:
    """count = 0 -> this week's report; otherwise the last `count` sends."""
    if count:
        runs = app.store.last_runs(max(1, min(count, 50)))
        await reports.send_report(bot, app, chat_id, [r["id"] for r in runs],
                                  f"Hisobot · oxirgi {len(runs)} ta yuborish")
    else:
        await reports.send_week_report(bot, app, chat_id)


async def _offer_group(chat, bot: Bot, app: App, count: int) -> None:
    """After a report in private chat: one tap to post the same report in the group."""
    group = app.settings.weekly_report_chat()
    if chat.type != "private" or group is None:
        return
    await bot.send_message(
        chat.id,
        "Shu hisobotni guruhga ham yuborasizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📤 Guruhga yuborish", callback_data=f"rgroup:{count}"),
        ]]),
    )


@router.callback_query(F.data.startswith("report:"))
async def cb_report(callback: CallbackQuery, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    await callback.answer()
    await _send_report(bot, app, callback.message.chat.id, 0)
    await _offer_group(callback.message.chat, bot, app, 0)


@router.callback_query(F.data.startswith("rgroup:"))
async def cb_report_to_group(callback: CallbackQuery, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    group = app.settings.weekly_report_chat()
    if group is None:
        return await callback.answer("GROUP_CHAT_ID sozlanmagan.", show_alert=True)
    await callback.answer("Yuborilmoqda...")
    try:
        await _send_report(bot, app, group, int(callback.data.split(":", 1)[1] or 0))
    except Exception as exc:
        log.exception("Report to group failed")
        await callback.message.edit_text(f"❌ Guruhga yuborib bo'lmadi: <code>{escape(str(exc))}</code>")
        return
    await callback.message.edit_text("✅ Hisobot guruhga yuborildi (📊 Hisobotlar).")




# ---------------------------------------------------------------------------
# /clear — remove test marks or whole test sends of this week
# ---------------------------------------------------------------------------

def _week_runs(app: App) -> list[int]:
    start, end = reports.week_bounds(now())
    return [r["id"] for r in app.store.runs_between(start, end)]


def clear_keyboard(step: str = "menu", action: str = "") -> InlineKeyboardMarkup:
    if step == "confirm":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, bajarish", callback_data=f"clr:go:{action}"),
            InlineKeyboardButton(text="⬅️ Yo'q", callback_data="clr:menu"),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Belgilarni tozalash", callback_data="clr:ask:marks")],
        [InlineKeyboardButton(text="♻️ Takrorlarni o'chirish", callback_data="clr:ask:dupes")],
        [InlineKeyboardButton(text="🗑 Yuborishni o'chirish (test)", callback_data="clr:ask:sends")],
        [InlineKeyboardButton(text="❌ Bekor", callback_data="clr:close")],
    ])


def clear_menu_text(app: App) -> str:
    run_ids = _week_runs(app)
    all_posts = app.store.posts_of_runs(run_ids)
    posts = len(all_posts)
    duplicates = posts - len({reports.product_key(p) for p in all_posts})
    marks = app.store.confirmation_count(run_ids)
    return (
        "🧹 <b>Shu haftani tozalash</b>\n\n"
        f"Yuborishlar: {len(run_ids)} ta · postlar: {posts} ta · ✅ belgilar: {marks} ta\n\n"
        "🧹 <b>Belgilarni tozalash</b> — barcha \"✅ Bajarildi\" belgilari o'chadi, "
        "postlar guruhda qoladi, hisobot 0 dan boshlanadi.\n"
        f"♻️ <b>Takrorlarni o'chirish</b> — bir mahsulot bir necha marta yuborilgan bo'lsa "
        f"(hozir {duplicates} ta takror), eskilari guruhdan o'chiriladi, eng yangisi qoladi; "
        "belgilar saqlanadi.\n"
        "🗑 <b>Yuborishni o'chirish</b> — test uchun: shu haftaning postlari guruhdan va "
        "bazadan o'chiriladi."
    )


@router.message(Command("clear"))
async def cmd_clear(message: Message, app: App) -> None:
    if not app.settings.is_admin(message.from_user and message.from_user.id):
        return await deny(message)
    await message.answer(clear_menu_text(app), reply_markup=clear_keyboard())


@router.callback_query(F.data.startswith("clr:"))
async def cb_clear(callback: CallbackQuery, bot: Bot, app: App) -> None:
    if not app.settings.is_admin(callback.from_user.id):
        return await deny(callback)
    parts = callback.data.split(":")
    step = parts[1]

    if step == "close":
        await callback.message.edit_text("❌ Bekor qilindi.")
        return await callback.answer()
    if step == "menu":
        await callback.message.edit_text(clear_menu_text(app), reply_markup=clear_keyboard())
        return await callback.answer()
    if step == "ask":
        action = parts[2]
        question = {
            "marks": "❓ Shu haftaning <b>barcha ✅ belgilari</b> o'chirilsinmi?",
            "dupes": "❓ Takroriy (eski) postlar guruhdan o'chirilsinmi? Har mahsulotning eng yangi posti qoladi.",
        }.get(action, "❓ Shu haftaning <b>barcha postlari guruhdan o'chirilsinmi</b>?\nBuni qaytarib bo'lmaydi.")
        await callback.message.edit_text(question, reply_markup=clear_keyboard("confirm", action))
        return await callback.answer()

    # step == "go"
    action = parts[2]
    if app.busy:
        return await callback.answer("Hozir yuborish davom etmoqda — keyinroq urinib ko'ring.", show_alert=True)
    await callback.answer("Bajarilmoqda...")
    await callback.message.edit_text("⏳ Tozalanmoqda...")
    run_ids = _week_runs(app)

    if action == "dupes":
        from .cleanup import delete_messages  # local import avoids an import cycle

        removed, changed, headers = app.store.merge_duplicate_posts(run_ids, reports.product_key)
        targets = [(p["chat_id"], p["message_id"]) for p in removed]
        targets += [(h["chat_id"], h["message_id"]) for h in headers]
        deleted, failed = await delete_messages(bot, targets)
        for post_id in changed:  # marks moved here from an older copy — show them
            await refresh_post(bot, app, post_id)
            await asyncio.sleep(0.5)
        text = f"♻️ {len(removed)} ta takroriy post o'chirildi (guruhdan: {deleted} ta xabar)."
        if failed:
            text += f"\n⚠️ {failed} ta xabarni o'chirib bo'lmadi — botga \"Delete messages\" huquqini bering."
        await callback.message.edit_text(text)
        return

    if action == "marks":
        posts = app.store.posts_of_runs(run_ids, only_confirmed=True)
        removed = app.store.clear_confirmations(run_ids)
        for post in posts:  # put the captions back without "Bajardi: ..."
            try:
                if post["has_photo"]:
                    await tg_retry(lambda p=post: bot.edit_message_caption(
                        chat_id=p["chat_id"], message_id=p["message_id"],
                        caption=p["caption"], reply_markup=done_keyboard(p["id"])))
                else:
                    await tg_retry(lambda p=post: bot.edit_message_text(
                        text=p["caption"], chat_id=p["chat_id"], message_id=p["message_id"],
                        reply_markup=done_keyboard(p["id"])))
            except TelegramBadRequest as exc:
                log.info("Caption reset skipped for post %s: %s", post["id"], exc)
            await asyncio.sleep(0.5)
        await callback.message.edit_text(
            f"✅ {removed} ta belgi o'chirildi, {len(posts)} ta post yangilandi.\n"
            "Hisobot endi 0 dan boshlanadi."
        )
        return

    # action == "sends": delete this week's posts from the chats, then from the database
    from .cleanup import delete_run_messages  # local import avoids an import cycle

    deleted, failed = await delete_run_messages(bot, app.store, run_ids)
    app.store.delete_runs(run_ids)

    text = f"🗑 {len(run_ids)} ta yuborish o'chirildi. Guruhdan o'chirilgan xabarlar: {deleted} ta."
    if failed:
        text += (
            f"\n⚠️ {failed} ta xabarni o'chirib bo'lmadi (48 soatdan eski yoki botda huquq yo'q) — "
            "ularni guruhda qo'lda o'chiring."
        )
    await callback.message.edit_text(text)
