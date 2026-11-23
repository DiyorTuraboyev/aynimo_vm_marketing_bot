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


def clear(app, bot, action):
    texts = []

    class Msg:
        async def edit_text(self, text, reply_markup=None):
            texts.append(text)

    async def answer(text=None, show_alert=False):
        pass

    callback = SimpleNamespace(data=f"clr:go:{action}", from_user=SimpleNamespace(id=1, full_name="Admin"),
                               message=Msg(), answer=answer)
    asyncio.run(handlers.cb_clear(callback, bot, app))
    return texts[-1]


def test_clear_marks(app, template, fake_bot, monkeypatch):
    monkeypatch.setattr(handlers.asyncio, "sleep", _no_sleep)
    run_send(app, template)
    vm_post = next(p["id"] for p in _posts(app) if p["department"] == "VM")
    press(app, vm_post, user_id=10, name="Diyor")
    assert app.store.confirmations(vm_post)

    fake_bot.sent.clear()
    result = clear(app, fake_bot, "marks")
    assert "1 ta belgi" in result
    assert not app.store.confirmations(vm_post)
    edits = [s for s in fake_bot.sent if s[0] == "edit"]
    assert len(edits) == 1 and "Bajardi" not in edits[0][3]  # caption restored
    assert _posts(app)  # posts are kept


def test_clear_sends(app, template, fake_bot):
    result_products, _ = run_send(app, template)
    fake_bot.sent.clear()
    result = clear(app, fake_bot, "sends")
    deleted = sum(len(s[2]) for s in fake_bot.sent if s[0] == "delete")
    assert deleted == len(result_products.products) + 2  # posts + 2 department headers
    assert "o'chirildi" in result
    assert not _posts(app)
    assert not app.store.last_runs()


def _move_runs_to_last_week(app):
    start, _ = reports.week_bounds(reports.now())
    last_week = (start - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    app.store._exec("UPDATE runs SET started_at = ?", (last_week,))


def test_monday_recap_and_cleanup(app, template, fake_bot, monkeypatch):
    from vm_marketing_bot import cleanup

    result_products, _ = run_send(app, template)
    vm_post = next(p["id"] for p in _posts(app) if p["department"] == "VM")
    press(app, vm_post, user_id=10, name="Diyor")
    _move_runs_to_last_week(app)

    start, _ = reports.week_bounds(reports.now())
    monkeypatch.setattr(cleanup, "now", lambda: start.replace(hour=8, minute=59))  # Monday 08:59
    fake_bot.sent.clear()
    asyncio.run(cleanup.maybe_monday_cleanup(fake_bot, app))
    assert not fake_bot.sent  # too early

    monkeypatch.setattr(cleanup, "now", lambda: start.replace(hour=9, minute=1))  # Monday 09:01
    asyncio.run(cleanup.maybe_monday_cleanup(fake_bot, app))
    kinds = [s[0] for s in fake_bot.sent]
    assert kinds.index("text") < kinds.index("delete")  # report first, then delete
    recap = next(s for s in fake_bot.sent if s[0] == "text")
    assert "O'tgan hafta natijasi" in recap[2] and "Diyor" in recap[2]
    assert any(s[0] == "doc" for s in fake_bot.sent)  # Excel too
    deleted = sum(len(s[2]) for s in fake_bot.sent if s[0] == "delete")
    assert deleted == len(result_products.products) + 2
    assert app.store.confirmations(vm_post)  # history is kept for reports

    fake_bot.sent.clear()  # runs once only
    asyncio.run(cleanup.maybe_monday_cleanup(fake_bot, app))
    assert not fake_bot.sent


def test_cleanup_leaves_this_week_alone(app, template, fake_bot, monkeypatch):
    from vm_marketing_bot import cleanup

    run_send(app, template)  # sent this week
    start, _ = reports.week_bounds(reports.now())
    monkeypatch.setattr(cleanup, "now", lambda: start.replace(hour=10))
    fake_bot.sent.clear()
    asyncio.run(cleanup.maybe_monday_cleanup(fake_bot, app))
    assert not any(s[0] == "delete" for s in fake_bot.sent)


def test_cleanup_warns_when_telegram_refuses(app, template, fake_bot, monkeypatch):
    from vm_marketing_bot import cleanup

    run_send(app, template)
    _move_runs_to_last_week(app)
    start, _ = reports.week_bounds(reports.now())
    monkeypatch.setattr(cleanup, "now", lambda: start.replace(hour=9, minute=30))
    monkeypatch.setattr(cleanup.asyncio, "sleep", _no_sleep)
    fake_bot.delete_fails = True
    fake_bot.sent.clear()
    asyncio.run(cleanup.maybe_monday_cleanup(fake_bot, app))
    warning = [s for s in fake_bot.sent if s[0] == "text" and "o'chirib bo'lmadi" in s[2]]
    assert warning and "Delete messages" in warning[0][2]


async def _no_sleep(*_):
    return None


def _posts(app):
    return app.store._all("SELECT * FROM posts ORDER BY id")
