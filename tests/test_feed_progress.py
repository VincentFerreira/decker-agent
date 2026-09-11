"""Tests for the scan-progress reporter that bridges LinkedInScraper's
`on_progress` callback (invoked from a worker thread, see
LinkedInScraper.scrape() in app/scraper.py) into a live Telegram message
edit on the bot's event loop.

Usage:
    pytest tests/test_feed_progress.py -v
"""
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

from bot.handlers.feed import _make_progress_reporter


async def _run_from_worker_thread(reporter, text: str) -> None:
    """Call `reporter(text)` from a real separate thread — mirrors how
    LinkedInScraper.scrape() actually calls on_progress, since it runs in a
    thread executor, not on the event loop (see bot/handlers/feed.py:_sync_scrape)."""
    done = threading.Event()

    def worker():
        reporter(text)
        done.set()

    threading.Thread(target=worker).start()

    # run_coroutine_threadsafe only schedules the push — give the loop a
    # chance to actually run it before asserting.
    for _ in range(200):
        if done.is_set():
            await asyncio.sleep(0.01)  # let the scheduled coroutine run too
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker thread never completed")


def test_reporter_edits_message_with_scan_header():
    async def scenario():
        loop = asyncio.get_running_loop()
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        reporter = _make_progress_reporter(query, loop)

        await _run_from_worker_thread(reporter, "Scroll 1/3 — 14 posts total (+11)")
        await asyncio.sleep(0.05)  # let the scheduled coroutine finish

        query.edit_message_text.assert_awaited_once()
        text = query.edit_message_text.call_args.args[0]
        assert text.startswith("⏳ Scanning your feed…")
        assert "Scroll 1/3 — 14 posts total (+11)" in text

    asyncio.run(scenario())


def test_reporter_swallows_edit_failures():
    """Telegram can reject an edit (rate limit, identical text on a
    no-gain scroll step) — that must not crash the scraper's worker thread."""
    async def scenario():
        loop = asyncio.get_running_loop()
        query = MagicMock()
        query.edit_message_text = AsyncMock(side_effect=Exception("boom"))
        reporter = _make_progress_reporter(query, loop)

        # Raises nothing, in the worker thread or afterwards.
        await _run_from_worker_thread(reporter, "Scroll 2/3 — 14 posts total (+0)")
        await asyncio.sleep(0.05)

        query.edit_message_text.assert_awaited_once()

    asyncio.run(scenario())
