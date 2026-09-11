"""Tests for the link button's placement on the browse card and the comment
"3 angles" screen — it must be the first row (most prominent) and available
on the comment-drafting flow too, not just the browse card, so checking the
post doesn't require backing all the way out (see bot/handlers/feed.py).

Usage:
    pytest tests/test_keyboards.py -v
"""
from bot.handlers.feed import _browse_keyboard, _variants_keyboard

POST_WITH_URL = {"url": "https://lnkd.in/p/abc123", "author_url": ""}
POST_WITH_AUTHOR_URL_ONLY = {"url": "", "author_url": "https://www.linkedin.com/in/someone/"}
POST_WITH_NO_LINK = {"url": "", "author_url": ""}


def test_browse_keyboard_puts_link_row_first():
    rows = _browse_keyboard(POST_WITH_URL).inline_keyboard
    assert rows[0][0].url == "https://lnkd.in/p/abc123"
    # The triage actions still follow, on the second row.
    assert rows[1][0].callback_data == "browse:comment"


def test_browse_keyboard_falls_back_to_author_url():
    rows = _browse_keyboard(POST_WITH_AUTHOR_URL_ONLY).inline_keyboard
    assert rows[0][0].url == "https://www.linkedin.com/in/someone/"


def test_browse_keyboard_has_no_link_row_when_unavailable():
    rows = _browse_keyboard(POST_WITH_NO_LINK).inline_keyboard
    assert len(rows) == 1  # only the triage-actions row
    assert rows[0][0].callback_data == "browse:comment"


def test_variants_keyboard_puts_link_row_first():
    rows = _variants_keyboard(POST_WITH_URL).inline_keyboard
    assert rows[0][0].url == "https://lnkd.in/p/abc123"
    assert rows[1][0].callback_data == "comment:use:0"


def test_variants_keyboard_has_no_link_row_when_unavailable():
    rows = _variants_keyboard(POST_WITH_NO_LINK).inline_keyboard
    assert rows[0][0].callback_data == "comment:use:0"
