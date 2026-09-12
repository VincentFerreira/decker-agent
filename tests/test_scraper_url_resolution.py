"""Unit tests for unwrapping LinkedIn's /safety/go/ redirect wrapper around
the permalink read from the "Copy link to post" confirmation toast, and for
_merge_resolved's same-run URL collision guard.

Usage:
    pytest tests/test_scraper_url_resolution.py -v
"""
from datetime import datetime, timezone

from app.models import Post
from app.scraper import _merge_resolved, _unwrap_safety_redirect


def test_unwraps_safety_redirect_to_real_target():
    wrapped = (
        "https://www.linkedin.com/safety/go/"
        "?url=https%3A%2F%2Flnkd%2Ein%2Fp%2FevKYYx-C&urlhash=DVzJ&isSdui=true"
    )
    assert _unwrap_safety_redirect(wrapped) == "https://lnkd.in/p/evKYYx-C"


def test_leaves_non_wrapped_url_untouched():
    direct = "https://www.linkedin.com/feed/update/urn:li:activity:7503376691461775360/"
    assert _unwrap_safety_redirect(direct) == direct


def test_leaves_url_without_target_param_untouched():
    """A /safety/go/ URL missing the `url` query param — don't crash, just
    pass it through unchanged rather than returning an empty string."""
    no_target = "https://www.linkedin.com/safety/go/?urlhash=DVzJ"
    assert _unwrap_safety_redirect(no_target) == no_target


def _post(urn: str, author: str, url: str = "") -> Post:
    return Post(
        urn=urn, author=author, text=f"text by {author}",
        scraped_at=datetime.now(tz=timezone.utc), url=url,
    )


def test_merge_resolved_discards_collision_with_different_post(monkeypatch):
    """A stale menu-item click can hand back a DIFFERENT post's real, validly-
    formed link (confirmed against live data: two posts with different
    authors/urns resolving to the identical URL from the same scrape run).
    _merge_resolved must never let that second resolution overwrite the
    first post's link binding — the post should fall back to no url rather
    than pointing at someone else's post."""
    same_url = "https://lnkd.in/p/eZKA82en"
    monkeypatch.setattr("app.scraper._resolve_post_url", lambda page, key: same_url)

    accumulated: dict[str, Post] = {}
    extractions = [
        (_post("urn:li:post:hash:aaa", "Alice"), "key-a"),
        (_post("urn:li:post:hash:bbb", "Bob"), "key-b"),
    ]
    _merge_resolved(accumulated, extractions, page=None)

    assert accumulated["urn:li:post:hash:aaa"].url == same_url
    assert accumulated["urn:li:post:hash:bbb"].url == ""


def test_merge_resolved_keeps_resolution_when_no_collision(monkeypatch):
    urls = iter(["https://lnkd.in/p/aaa111", "https://lnkd.in/p/bbb222"])
    monkeypatch.setattr("app.scraper._resolve_post_url", lambda page, key: next(urls))

    accumulated: dict[str, Post] = {}
    extractions = [
        (_post("urn:li:post:hash:aaa", "Alice"), "key-a"),
        (_post("urn:li:post:hash:bbb", "Bob"), "key-b"),
    ]
    _merge_resolved(accumulated, extractions, page=None)

    assert accumulated["urn:li:post:hash:aaa"].url == "https://lnkd.in/p/aaa111"
    assert accumulated["urn:li:post:hash:bbb"].url == "https://lnkd.in/p/bbb222"
