"""Unit tests for the "best available link" fallback used on browse cards,
the comment-ready screen, and the 1h reminder message.

Usage:
    pytest tests/test_post_link.py -v
"""
from bot.handlers.feed import _post_link


def test_prefers_resolved_post_url():
    post = {"url": "https://lnkd.in/p/abc123", "author_url": "https://www.linkedin.com/in/someone/"}
    assert _post_link(post) == ("🔗 Open on LinkedIn", "https://lnkd.in/p/abc123")


def test_falls_back_to_author_url_when_post_url_missing():
    post = {"url": "", "author_url": "https://www.linkedin.com/in/someone/"}
    assert _post_link(post) == ("🔗 Open author's profile", "https://www.linkedin.com/in/someone/")


def test_no_link_available():
    assert _post_link({"url": "", "author_url": ""}) == ("", "")


def test_missing_keys_treated_as_no_link():
    assert _post_link({}) == ("", "")
