"""Topics (separate VM / MARKETING / report sections), admin-only access, group cleaning."""
import asyncio
from types import SimpleNamespace

import pytest

from vm_marketing_bot import group, handlers, media, reports
from vm_marketing_bot.captions import week_label
from vm_marketing_bot.excel import load_products
from vm_marketing_bot.sender import Sender, SendJob
from vm_marketing_bot.storage import Storage

from .conftest import make_settings


@pytest.fixture
def app(settings, fake_bot, tmp_path, monkeypatch, jpeg_bytes):
    async def fake_fetch(self, path, attempts=3):
        return jpeg_bytes

    monkeypatch.setattr(media.ImageFetcher, "fetch", fake_fetch)
    store = Storage(tmp_path / "t.db")
    return handlers.App(settings=settings, store=store, sender=Sender(fake_bot, store, settings))


def send_all(app, template):
    result = load_products(template)
    job = SendJob(
        result=result, departments=["VM", "MARKETING"],
        targets={d: app.settings.target_chat(d, 555) for d in ("VM", "MARKETING")},
        report_chat_id=555, started_by="Admin", week=week_label(),
    )
    asyncio.run(app.sender.run(job, None))
    return result, job


def test_forum_group_gets_separate_topics(app, template, fake_bot):
    fake_bot.forum = True
    result, job = send_all(app, template)
    assert not job.failed

    topics = {name: tid for kind, _, name, tid in (s for s in fake_bot.sent if s[0] == "topic")}
    assert set(topics) == {"🎨 VM", "📢 MARKETING"}

    photos = [s for s in fake_bot.sent if s[0] == "photo"]
    vm_threads = {s[4] for s in photos if "<b>VM</b>" in s[2]}
    mk_threads = {s[4] for s in photos if "<b>MARKETING</b>" in s[2]}
    assert vm_threads == {topics["🎨 VM"]}
    assert mk_threads == {topics["📢 MARKETING"]}

    # topics are created once and reused next week
    fake_bot.sent.clear()
    send_all(app, template)
    assert not [s for s in fake_bot.sent if s[0] == "topic"]

    # reports go to their own topic
    fake_bot.sent.clear()
    asyncio.run(reports.send_week_report(fake_bot, app, app.settings.group_chat_id))
    report_topic = next(s for s in fake_bot.sent if s[0] == "topic")
    assert report_topic[2] == "📊 Hisobotlar"
    assert next(s for s in fake_bot.sent if s[0] == "text")[4] == report_topic[3]


def test_deleted_topic_is_recreated(app, template, fake_bot):
    fake_bot.forum = True
    send_all(app, template)
    old_vm = next(s[3] for s in fake_bot.sent if s[0] == "topic" and s[2] == "🎨 VM")
    fake_bot.thread_missing = {old_vm}  # somebody deleted the VM topic

    fake_bot.sent.clear()
    _, job = send_all(app, template)
    assert not job.failed
    new_vm = next(s[3] for s in fake_bot.sent if s[0] == "topic" and s[2] == "🎨 VM")
    assert new_vm != old_vm
    assert all(s[4] == new_vm for s in fake_bot.sent if s[0] == "photo" and "<b>VM</b>" in s[2])


def test_normal_group_has_no_topics(app, template, fake_bot):
    send_all(app, template)
    assert not [s for s in fake_bot.sent if s[0] == "topic"]
    assert all(s[4] is None for s in fake_bot.sent if s[0] == "photo")


def test_only_admin_ids_can_use_the_bot(tmp_path):
    assert make_settings(tmp_path, admin_ids=frozenset({1})).is_admin(1)
    assert not make_settings(tmp_path, admin_ids=frozenset({1})).is_admin(2)
    assert not make_settings(tmp_path, admin_ids=frozenset()).is_admin(1)  # empty = nobody
    assert not make_settings(tmp_path).is_admin(None)


def test_group_cleaning_deletes_commands_and_notices(fake_bot):
    message = SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"), message_id=77)
    asyncio.run(group.remove_service_message(message, fake_bot))
    asyncio.run(group.remove_command(message, fake_bot))
    assert [s for s in fake_bot.sent if s[0] == "delete"] == [("delete", -100, [77]), ("delete", -100, [77])]


def test_excel_upload_in_group_is_ignored(app, fake_bot):
    answers = []

    async def answer(text, **_):
        answers.append(text)

    message = SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"), answer=answer,
                              from_user=SimpleNamespace(id=1), document=SimpleNamespace(file_name="x.xlsx"))
    asyncio.run(handlers.on_document(message, fake_bot, app))
    assert not answers and not fake_bot.sent
