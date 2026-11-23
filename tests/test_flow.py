"""Send -> confirm -> weekly report, with a fake Telegram bot (no network)."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from vm_marketing_bot import handlers, media, reports
from vm_marketing_bot.captions import week_label
from vm_marketing_bot.excel import load_products
from vm_marketing_bot.sender import Sender, SendJob
from vm_marketing_bot.storage import Storage


@pytest.fixture
def app(settings, fake_bot, tmp_path, monkeypatch, jpeg_bytes):
    async def fake_fetch(self, path, attempts=3):
        return jpeg_bytes

    monkeypatch.setattr(media.ImageFetcher, "fetch", fake_fetch)
    store = Storage(tmp_path / "t.db")
    return handlers.App(settings=settings, store=store, sender=Sender(fake_bot, store, settings))


def run_send(app, template):
    result = load_products(template)
    here = 555
    job = SendJob(
        result=result, departments=["VM", "MARKETING"],
        targets={d: app.settings.target_chat(d, here) for d in ("VM", "MARKETING")},
        report_chat_id=here, started_by="Admin", week=week_label(),
    )
    asyncio.run(app.sender.run(job, None))
    return result, job


def press(app, post_id, user_id, name="User"):
    answers, edits = [], []

    class Msg:
        async def edit_caption(self, caption, reply_markup):
            edits.append(caption)

        async def edit_text(self, text, reply_markup):
            edits.append(text)

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    callback = SimpleNamespace(
        data=f"done:{post_id}", from_user=SimpleNamespace(id=user_id, full_name=name),
        message=Msg(), answer=answer,
    )
    asyncio.run(handlers.cb_done(callback, app))
    return answers, edits


def test_everything_goes_to_one_group(app, template, fake_bot):
    result, job = run_send(app, template)
    assert job.sent == len(result.products)
    assert not job.failed
    chats = {chat for kind, chat, *_ in fake_bot.sent if kind == "photo"}
    assert chats == {app.settings.group_chat_id}


def test_department_teams_and_debounce(app, template):
    result, _ = run_send(app, template)
    vm_post = next(p["id"] for p in _posts(app) if p["department"] == "VM")

    answers, edits = press(app, vm_post, user_id=20, name="Marketing Ali")  # wrong team
    assert answers[0][1] is True and not edits
    assert not app.store.confirmations(vm_post)

    answers, edits = press(app, vm_post, user_id=10, name="VM Vali")
    assert "Bajardi" in edits[-1]
    answers, _ = press(app, vm_post, user_id=10, name="VM Vali")  # quick repeat ignored
    assert len(app.store.confirmations(vm_post)) == 1


def test_weekly_report(app, template, fake_bot, monkeypatch):
    run_send(app, template)
    vm_post = next(p["id"] for p in _posts(app) if p["department"] == "VM")
    press(app, vm_post, user_id=10, name="VM Vali")

    # Pretend it is Sunday 18:05 of the current week.
    start, _ = reports.week_bounds(reports.now())
    sunday = start.replace(hour=18, minute=5) + timedelta(days=6)
    monkeypatch.setattr(reports, "now", lambda: sunday)

    fake_bot.sent.clear()
    asyncio.run(reports.maybe_send_weekly_report(fake_bot, app))
    texts = [s for s in fake_bot.sent if s[0] == "text"]
    docs = [s for s in fake_bot.sent if s[0] == "doc"]
    assert texts and docs
    assert texts[0][1] == app.settings.group_chat_id
    assert "VM" in texts[0][2] and "1/" in texts[0][2]

    fake_bot.sent.clear()  # second check the same day must not resend
    asyncio.run(reports.maybe_send_weekly_report(fake_bot, app))
    assert not fake_bot.sent


def test_no_report_on_other_days(app, template, fake_bot, monkeypatch):
    run_send(app, template)
    start, _ = reports.week_bounds(reports.now())
    monkeypatch.setattr(reports, "now", lambda: start.replace(hour=20))  # Monday
    fake_bot.sent.clear()
    asyncio.run(reports.maybe_send_weekly_report(fake_bot, app))
    assert not fake_bot.sent


def _posts(app):
    return app.store._all("SELECT * FROM posts ORDER BY id")
