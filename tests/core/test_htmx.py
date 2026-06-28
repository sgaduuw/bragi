"""Unit tests for the htmx dispatch helpers.

`wants_partial()` is the load-bearing distinction: a boosted rail
navigation sends both `HX-Request` and `HX-Boosted`, and must NOT be
served a partial (it swaps only `.admin-content` out of a full page).
"""

from __future__ import annotations

from flask import Flask

from bragi.core.htmx import is_boosted, is_htmx, wants_partial

app = Flask(__name__)


def test_plain_request_is_neither_htmx_nor_boosted() -> None:
    with app.test_request_context("/"):
        assert is_htmx() is False
        assert is_boosted() is False
        assert wants_partial() is False


def test_in_page_htmx_swap_wants_partial() -> None:
    with app.test_request_context("/", headers={"HX-Request": "true"}):
        assert is_htmx() is True
        assert is_boosted() is False
        assert wants_partial() is True


def test_boosted_navigation_does_not_want_partial() -> None:
    headers = {"HX-Request": "true", "HX-Boosted": "true"}
    with app.test_request_context("/", headers=headers):
        assert is_htmx() is True
        assert is_boosted() is True
        # Boosted == full-page navigation: render the whole page.
        assert wants_partial() is False
