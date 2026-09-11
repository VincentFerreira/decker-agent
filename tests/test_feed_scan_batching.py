"""Tests for feed:scan's batched Claude evaluation (bot/handlers/feed.py).

Regression coverage for a bug where evaluate_posts() failing on one batch
used to leave EVERY candidate unnotified for the whole scan — since a single
oversized prompt (all unnotified posts, unbounded) routinely exceeded
CLAUDE_TIMEOUT once the backlog grew, every scan failed completely and the
backlog only grew further. Batching + marking each batch notified as soon as
it succeeds means progress on earlier batches survives a later one failing.

Usage:
    pytest tests/test_feed_scan_batching.py -v
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from bot.config import AUTHORIZED_USER_ID
from bot.handlers import feed as feed_module
from bot.storage.json_posts_store import JSONPostsStore
from bot.storage.json_store import JSONSettingsStore


def _make_post(urn: str) -> dict:
    return {
        "urn": urn,
        "author": "Someone",
        "text": "Some post text",
        "reactions": 0,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        "posted_at": None,
        "url": "",
        "author_url": "",
    }


def _build_update():
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = AUTHORIZED_USER_ID
    return update, query


def test_a_failed_batch_does_not_wipe_out_an_earlier_successful_one(tmp_path, monkeypatch):
    posts_store = JSONPostsStore(tmp_path / "bot_posts.json")
    settings_store = JSONSettingsStore(tmp_path / "bot_settings.json")
    settings_store.set_interests(["QA"])

    monkeypatch.setattr(feed_module, "_posts_store", posts_store)
    monkeypatch.setattr(feed_module, "_settings_store", settings_store)
    monkeypatch.setattr(feed_module, "EVAL_BATCH_SIZE", 2)

    posts = [_make_post(f"urn:{i}") for i in range(4)]
    monkeypatch.setattr(feed_module, "_sync_scrape", lambda on_progress: posts)

    # First batch (posts 0,1) succeeds; second batch (posts 2,3) fails —
    # mirrors evaluate_posts() returning {} on a Claude timeout/error.
    calls = {"n": 0}

    def fake_evaluate(batch, interests):
        calls["n"] += 1
        if calls["n"] == 1:
            return {p["urn"]: {"relevant": False, "score": 0, "reason": "n/a"} for p in batch}
        return {}

    monkeypatch.setattr(feed_module, "evaluate_posts", fake_evaluate)

    update, query = _build_update()
    ctx = MagicMock()
    ctx.chat_data = {}

    asyncio.run(feed_module.cb_feed_scan(update, ctx))

    assert calls["n"] == 2  # both batches were attempted
    stored = {p["urn"]: p for p in posts_store._read()}
    assert stored["urn:0"]["notified"] is True
    assert stored["urn:1"]["notified"] is True
    assert stored["urn:2"]["notified"] is False
    assert stored["urn:3"]["notified"] is False


def test_unnotified_posts_are_retried_on_the_next_scan(tmp_path, monkeypatch):
    """The posts left unnotified after a failed batch must show up again in
    get_unnotified() — confirming they'll actually be retried, not lost."""
    posts_store = JSONPostsStore(tmp_path / "bot_posts.json")
    posts_store.record_scrape([_make_post("urn:2"), _make_post("urn:3")])

    assert {p["urn"] for p in posts_store.get_unnotified()} == {"urn:2", "urn:3"}
