import io
import sys
from datetime import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vm_marketing_bot.config import Settings  # noqa: E402

TEMPLATE = ROOT / "VM_MARKETING TEMPLATE.xlsx"


@pytest.fixture
def template() -> Path:
    if not TEMPLATE.exists():
        pytest.skip("template Excel not in repo")
    return TEMPLATE


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        bot_token="1:test", base_image_url="https://example.invalid/", webdav_user="u",
        webdav_password="p", group_chat_id=-1001, vm_chat_id=None, marketing_chat_id=None,
        report_chat_id=None, admin_ids=frozenset({1}), vm_team_ids=frozenset({10}),
        marketing_team_ids=frozenset({20}), excel_file=TEMPLATE, data_dir=tmp_path,
        send_delay=0, done_button=True, report_enabled=True, report_weekday=6,
        report_time=time(18, 0),
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


class FakeBot:
    """Records what would be sent to Telegram."""

    def __init__(self):
        self.mid = 0
        self.sent = []

    def _msg(self, photo: bool):
        self.mid += 1
        return SimpleNamespace(
            message_id=self.mid,
            photo=[SimpleNamespace(file_id=f"FID{self.mid}")] if photo else None,
        )

    forum = False  # set True to simulate a group with Topics turned on
    thread_missing: set = set()  # thread ids that "were deleted" in Telegram

    def _check_thread(self, thread):
        if thread is not None and thread in self.thread_missing:
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.methods import SendMessage

            raise TelegramBadRequest(SendMessage(chat_id=0, text="x"), "Bad Request: message thread not found")

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, is_forum=self.forum and chat_id != 555, title="Guruh", full_name="Guruh")

    async def create_forum_topic(self, chat_id, name, icon_color=None):
        self.mid += 1
        self.sent.append(("topic", chat_id, name, self.mid))
        return SimpleNamespace(message_thread_id=self.mid, name=name)

    async def send_photo(self, chat_id, photo, caption, reply_markup=None, message_thread_id=None):
        self._check_thread(message_thread_id)
        self.sent.append(("photo", chat_id, caption, reply_markup, message_thread_id))
        return self._msg(True)

    async def send_message(self, chat_id, text, reply_markup=None, message_thread_id=None):
        self._check_thread(message_thread_id)
        self.sent.append(("text", chat_id, text, reply_markup, message_thread_id))
        return self._msg(False)

    async def send_document(self, chat_id, document, message_thread_id=None):
        self.sent.append(("doc", chat_id, document.filename, document.data, message_thread_id))

    async def edit_message_caption(self, chat_id, message_id, caption, reply_markup=None):
        self.sent.append(("edit", chat_id, message_id, caption))

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None):
        self.sent.append(("edit", chat_id, message_id, text))

    async def delete_messages(self, chat_id, message_ids):
        if self.delete_fails:
            raise RuntimeError("message can't be deleted")
        self.sent.append(("delete", chat_id, list(message_ids)))
        return True

    async def delete_message(self, chat_id, message_id):
        if self.delete_fails:
            raise RuntimeError("message can't be deleted")
        self.sent.append(("delete", chat_id, [message_id]))
        return True

    delete_fails = False


@pytest.fixture
def fake_bot():
    from vm_marketing_bot import topics

    topics._forum_cache.clear()
    bot = FakeBot()
    bot.thread_missing = set()
    return bot


@pytest.fixture
def jpeg_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(buf, "JPEG")
    return buf.getvalue()
