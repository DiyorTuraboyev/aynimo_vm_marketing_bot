"""The sending engine: posts every product of the chosen departments to their chats."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .captions import CAPTION_LIMIT, TEXT_LIMIT, build_caption, department_header
from .config import DEPARTMENT_TITLES, Settings
from .excel import LoadResult, Product
from .media import ImageFetcher, shrink_image
from .storage import Storage
from .topics import forget as forget_topic
from .topics import is_thread_error, thread_for

log = logging.getLogger(__name__)
T = TypeVar("T")

PREFETCH = 3  # photos downloaded ahead; kept small for 256 MB hosting plans
PROGRESS_EVERY_SECONDS = 3.0
MAX_INTERVAL = 8.0  # never slower than one post per 8 s
NO_PHOTO_PREFIX = "🖼 <i>Rasm yuklanmadi</i>\n\n"


class FatalSendError(Exception):
    """The target chat itself is unusable (bot not admin, chat not found...)."""


@dataclass
class SendJob:
    result: LoadResult
    departments: list[str]
    targets: dict[str, int | str]
    report_chat_id: int
    started_by: str
    week: str
    sample: bool = False  # only first product of each department, no header/buttons
    cancelled: bool = False
    total: int = 0
    processed: int = 0
    sent: int = 0
    text_only: int = 0
    failed: list[str] = field(default_factory=list)
    current: str = ""
    fatal: str = ""


async def tg_retry(
    call: Callable[[], Awaitable[T]],
    attempts: int = 6,
    on_flood: Callable[[int], None] | None = None,
) -> T:
    for attempt in range(attempts):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            log.info("Flood control: sleeping %ss", exc.retry_after)
            if on_flood is not None:
                on_flood(exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramNetworkError as exc:
            if attempt == attempts - 1:
                raise
            log.warning("Network error: %s (attempt %s)", exc, attempt + 1)
            await asyncio.sleep(3 * (attempt + 1))
    return await call()


def done_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"done:{post_id}")]]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⛔ To'xtatish", callback_data="job:cancel")]]
    )


def _is_fatal(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("chat not found", "not enough rights", "have no rights", "bot was kicked"))


def _filename(product: Product) -> str:
    ext = Path(product.photo_path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    stem = (product.model or "product").replace("/", "_").replace("\\", "_")
    return stem + ext


class Sender:
    def __init__(self, bot: Bot, store: Storage, settings: Settings):
        self.bot = bot
        self.store = store
        self.settings = settings
        self.interval = settings.send_delay  # seconds between posts; grows if Telegram complains

    async def _thread(self, chat, section: str, recreate: bool = False) -> int | None:
        if recreate:
            forget_topic(self.store, chat, section)
        return await thread_for(self.bot, self.store, chat, section, self.settings.use_topics)

    async def _send_text(self, chat, text: str, thread: int | None) -> Message:
        return await tg_retry(
            lambda: self.bot.send_message(chat_id=chat, text=text, message_thread_id=thread),
            on_flood=self._slow_down,
        )

    def _slow_down(self, retry_after: int) -> None:
        """Telegram said "too fast" — post slower for the rest of this send."""
        self.interval = min(self.interval + 1.0, MAX_INTERVAL)
        log.info("Slowing down: one post every %.0f s", self.interval)

    async def _send_photo(self, chat, photo, caption: str, keyboard, thread: int | None = None) -> Message:
        return await tg_retry(
            lambda: self.bot.send_photo(
                chat_id=chat, photo=photo, caption=caption, reply_markup=keyboard, message_thread_id=thread
            ),
            on_flood=self._slow_down,
        )

    async def send_product(
        self,
        chat,
        product: Product,
        caption: str,
        keyboard: InlineKeyboardMarkup | None,
        get_bytes: Callable[[], Awaitable[bytes | None]],
        thread: int | None = None,
    ) -> tuple[Message, bool, str]:
        """Returns (message, has_photo, text_that_was_sent)."""
        path = product.photo_path
        if path:
            file_id = self.store.cached_file_id(path)
            if file_id:
                try:
                    return await self._send_photo(chat, file_id, caption, keyboard, thread), True, caption
                except TelegramBadRequest as exc:
                    if _is_fatal(exc) or is_thread_error(exc):
                        raise
                    log.info("Cached file_id rejected for %s: %s", path, exc)
                    self.store.forget_file_id(path)

            data = await get_bytes()
            if data:
                for attempt, payload in enumerate((data, None)):
                    if payload is None:
                        payload = await asyncio.to_thread(shrink_image, data, True)
                    try:
                        photo = BufferedInputFile(payload, filename=_filename(product))
                        msg = await self._send_photo(chat, photo, caption, keyboard, thread)
                        if msg.photo:
                            self.store.cache_file_id(path, msg.photo[-1].file_id)
                        return msg, True, caption
                    except TelegramBadRequest as exc:
                        if _is_fatal(exc) or is_thread_error(exc):
                            raise
                        log.warning("send_photo failed for %s (try %s): %s", product.label, attempt + 1, exc)

        text = NO_PHOTO_PREFIX + caption
        msg = await tg_retry(
            lambda: self.bot.send_message(
                chat_id=chat, text=text[:TEXT_LIMIT], reply_markup=keyboard, message_thread_id=thread
            ),
            on_flood=self._slow_down,
        )
        return msg, False, text

    async def run(self, job: SendJob, progress: Message | None) -> None:
        queue: list[tuple[str, Product]] = []
        for dept in job.departments:
            items = job.result.for_department(dept)
            queue.extend((dept, p) for p in (items[:1] if job.sample else items))
        job.total = len(queue)

        run_id = None
        if not job.sample:
            run_id = self.store.start_run(job.week, job.result.source.name, job.started_by)

        last_progress = 0.0

        async def update_progress(force: bool = False) -> None:
            nonlocal last_progress
            if progress is None or (not force and time.monotonic() - last_progress < PROGRESS_EVERY_SECONDS):
                return
            last_progress = time.monotonic()
            try:
                await progress.edit_text(self.progress_text(job), reply_markup=cancel_keyboard())
            except TelegramBadRequest:
                pass
            except Exception as exc:
                log.debug("progress update failed: %s", exc)

        prefetch: dict[int, asyncio.Task] = {}

        # Even pacing under Telegram's group limit (~20 messages/minute). The time spent
        # downloading the next photo counts towards the gap, so no time is wasted.
        self.interval = 0.0 if job.sample else self.settings.send_delay
        last_send = 0.0

        async def pace() -> None:
            nonlocal last_send
            wait = self.interval - (time.monotonic() - last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            last_send = time.monotonic()

        async with aiohttp.ClientSession() as session:
            fetcher = ImageFetcher(self.settings, session)

            def ensure_prefetch(start: int) -> None:
                for j in range(start, min(start + PREFETCH, len(queue))):
                    product = queue[j][1]
                    if j in prefetch or not product.photo_path:
                        continue
                    if self.store.cached_file_id(product.photo_path):
                        continue
                    prefetch[j] = asyncio.create_task(fetcher.fetch(product.photo_path))

            current_dept = None
            thread: int | None = None
            try:
                for i, (dept, product) in enumerate(queue):
                    if job.cancelled:
                        break
                    ensure_prefetch(i)
                    chat = job.targets[dept]
                    icon, title = DEPARTMENT_TITLES[dept]
                    job.current = f"{icon} {title}: {product.title}"
                    await update_progress()

                    if dept != current_dept:
                        current_dept = dept
                        thread = await self._thread(chat, dept)
                        if not job.sample:
                            header = department_header(dept, job.result.for_department(dept), job.week)
                            try:
                                await pace()
                                try:
                                    head_msg = await self._send_text(chat, header, thread)
                                except TelegramBadRequest as exc:
                                    if not is_thread_error(exc):
                                        raise
                                    thread = await self._thread(chat, dept, recreate=True)
                                    head_msg = await self._send_text(chat, header, thread)
                                if run_id is not None:
                                    self.store.add_run_message(run_id, chat, head_msg.message_id)
                            except Exception as exc:
                                if _is_fatal(exc):
                                    job.fatal = f"{title} chatiga yuborib bo'lmadi: {exc}"
                                    raise FatalSendError(job.fatal) from exc
                                log.warning("Header send failed: %s", exc)

                    async def get_bytes(idx: int = i, p: Product = product) -> bytes | None:
                        task = prefetch.pop(idx, None)
                        if task is not None:
                            return await task
                        return await fetcher.fetch(p.photo_path)

                    caption = build_caption(product, job.week, CAPTION_LIMIT)
                    post_id = None
                    keyboard = None
                    if run_id is not None and self.settings.done_button:
                        post_id = self.store.create_post(
                            run_id, dept, job.week, product.row, product.model,
                            product.name, product.note, chat, caption,
                            region=product.region.text + (" (bot tavsiyasi)" if product.region.suggested else ""),
                        )
                        keyboard = done_keyboard(post_id)

                    try:
                        await pace()
                        try:
                            msg, has_photo, sent_text = await self.send_product(
                                chat, product, caption, keyboard, get_bytes, thread
                            )
                        except TelegramBadRequest as exc:
                            if not is_thread_error(exc):
                                raise
                            # someone deleted the topic — make a new one and retry once
                            thread = await self._thread(chat, dept, recreate=True)
                            msg, has_photo, sent_text = await self.send_product(
                                chat, product, caption, keyboard, get_bytes, thread
                            )
                        job.sent += 1
                        if not has_photo:
                            job.text_only += 1
                        if post_id is not None:
                            self.store.set_post_message(post_id, msg.message_id, has_photo, sent_text)
                    except Exception as exc:
                        if post_id is not None:
                            self.store.delete_post(post_id)
                        if _is_fatal(exc):
                            job.fatal = f"{title} chatiga yuborib bo'lmadi: {exc}"
                            raise FatalSendError(job.fatal) from exc
                        log.exception("Failed to send %s", product.label)
                        job.failed.append(f"{title} · {product.label}: {exc}")

                    job.processed += 1
            except FatalSendError:
                pass
            finally:
                for task in prefetch.values():
                    task.cancel()

        if run_id is not None:
            self.store.finish_run(run_id, job.sent, len(job.failed), job.cancelled or bool(job.fatal))

        if progress is not None:
            try:
                await progress.edit_text(self.progress_text(job, final=True))
            except Exception:
                pass
        await tg_retry(lambda: self.bot.send_message(job.report_chat_id, self.final_text(job)))

    @staticmethod
    def progress_text(job: SendJob, final: bool = False) -> str:
        if final:
            status = "⛔ To'xtatildi" if job.cancelled else ("❌ Xatolik bilan to'xtadi" if job.fatal else "✅ Yakunlandi")
            return f"{status}: {job.processed}/{job.total}"
        return (
            f"⏳ <b>Yuborilmoqda:</b> {job.processed}/{job.total}\n"
            f"{escape(job.current)}\n\n"
            f"✅ {job.sent} · 🖼 rasmsiz {job.text_only} · ❌ {len(job.failed)}"
        )

    @staticmethod
    def final_text(job: SendJob) -> str:
        head = "👁 <b>Namuna yuborildi</b>" if job.sample else "📬 <b>Yuborish yakunlandi</b>"
        if job.cancelled:
            head = "⛔ <b>Yuborish to'xtatildi</b>"
        if job.fatal:
            head = "❌ <b>Yuborish to'xtadi</b>\n" + escape(job.fatal) + (
                "\n\nBot guruh/kanalga qo'shilganini va xabar yuborish huquqi borligini tekshiring."
            )
        lines = [
            head,
            "",
            f"📦 Jami: {job.total} · ✅ Yuborildi: {job.sent} · ❌ Xato: {len(job.failed)}",
        ]
        if job.text_only:
            lines.append(f"🖼 Rasm yuklanmagani uchun matn ko'rinishida: {job.text_only} ta")
        if job.failed:
            lines.append("")
            lines.append("<b>Xatolar:</b>")
            lines.extend(f"• {escape(f)}" for f in job.failed[:15])
            if len(job.failed) > 15:
                lines.append(f"… yana {len(job.failed) - 15} ta")
        return "\n".join(lines)[:TEXT_LIMIT]
