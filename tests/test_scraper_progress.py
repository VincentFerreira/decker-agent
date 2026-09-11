"""Unit tests for the scrape progress messages surfaced via `on_progress`.

Usage:
    pytest tests/test_scraper_progress.py -v
"""
from app.scraper import _initial_progress_message, _scroll_progress_message


def test_initial_progress_message():
    assert _initial_progress_message(7) == "Initial extraction: 7 posts"


def test_scroll_progress_message():
    assert (
        _scroll_progress_message(step=2, max_scrolls=5, total=26, gained=12)
        == "Scroll 2/5 — 26 posts total (+12)"
    )


def test_scroll_progress_message_with_no_gain():
    """A scroll step that found nothing new — the "stopping early" case."""
    assert (
        _scroll_progress_message(step=3, max_scrolls=5, total=26, gained=0)
        == "Scroll 3/5 — 26 posts total (+0)"
    )
