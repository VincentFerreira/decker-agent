"""Tests for the "⏰ Remind in 1h" comment-drafting action.

Regression coverage for a bug where `ctx.application.job_queue` was silently
`None` at runtime because APScheduler (python-telegram-bot's `job-queue`
extra) wasn't installed — tapping "Remind in 1h" crashed with
`AttributeError: 'NoneType' object has no attribute 'run_once'`.
Fixed by pinning `python-telegram-bot[job-queue]` in requirements.txt.

No network call is made anywhere here: JobQueue.run_once only touches the
in-process APScheduler, and Application.builder().build() doesn't contact
Telegram until .initialize()/.run_polling(), which these tests never call.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from telegram.ext import Application

from bot.config import AUTHORIZED_USER_ID
from bot.handlers.feed import cb_comment_remind

FAKE_TOKEN = "123456:ABC-test-token-not-a-real-bot"


def _build_context(chat_data: dict):
    """A real Application, so job_queue is the real JobQueue — this is what
    makes test_job_queue_is_available a real regression check rather than a
    tautology against a mock."""
    app = Application.builder().token(FAKE_TOKEN).build()
    ctx = MagicMock()
    ctx.application = app
    ctx.chat_data = chat_data
    return ctx, app


def _build_update(chat_id: int = 999):
    query = MagicMock()
    query.answer = AsyncMock()
    query.message.chat_id = chat_id
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = AUTHORIZED_USER_ID
    return update, query


def test_job_queue_is_available():
    """Root-cause check: without the `job-queue` extra, Application.job_queue
    is silently None and any code calling `.run_once` on it crashes."""
    app = Application.builder().token(FAKE_TOKEN).build()
    assert app.job_queue is not None


def test_remind_schedules_a_job_about_an_hour_out():
    comment_state = {
        "post": {"url": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
        "selected_idx": 0,
        "variants": [{"angle": "NUANCE", "label": "① Nuance", "text": "My comment text"}],
    }
    update, query = _build_update(chat_id=999)
    ctx, app = _build_context({"comment": comment_state})

    asyncio.run(cb_comment_remind(update, ctx))

    jobs = app.job_queue.jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.chat_id == 999

    delay = (job.job.trigger.run_date - datetime.now(tz=timezone.utc)).total_seconds()
    assert 3500 < delay <= 3600  # ~1h, generous margin for test execution time

    assert "comment" not in ctx.chat_data  # drafting state is cleared once scheduled
    query.answer.assert_awaited_once()


def test_remind_job_sends_the_selected_comment_text():
    comment_state = {
        "post": {"url": "https://www.linkedin.com/feed/update/urn:li:activity:1/"},
        "selected_idx": 1,
        "variants": [
            {"angle": "NUANCE", "label": "① Nuance", "text": "Variant zero"},
            {"angle": "EXPERIENCE", "label": "② Experience", "text": "Variant one"},
        ],
    }
    update, _ = _build_update(chat_id=999)
    ctx, app = _build_context({"comment": comment_state})

    asyncio.run(cb_comment_remind(update, ctx))
    job = app.job_queue.jobs()[0]

    fake_context = MagicMock()
    fake_context.bot.send_message = AsyncMock()
    asyncio.run(job.callback(fake_context))

    fake_context.bot.send_message.assert_awaited_once()
    kwargs = fake_context.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 999
    assert "Variant one" in kwargs["text"]  # the *selected* variant, not variant zero
    assert comment_state["post"]["url"] in kwargs["text"]


def test_remind_without_pending_comment_does_not_crash():
    """No "comment" in chat_data (e.g. cleared elsewhere) — the handler must
    no-op rather than raise on the missing key."""
    update, query = _build_update()
    ctx, app = _build_context({})

    asyncio.run(cb_comment_remind(update, ctx))

    assert app.job_queue.jobs() == ()
    query.answer.assert_awaited_once()
