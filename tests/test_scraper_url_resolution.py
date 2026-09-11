"""Unit tests for unwrapping LinkedIn's /safety/go/ redirect wrapper around
the permalink read from the "Copy link to post" confirmation toast.

Usage:
    pytest tests/test_scraper_url_resolution.py -v
"""
from app.scraper import _unwrap_safety_redirect


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
