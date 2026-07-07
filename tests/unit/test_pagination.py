"""`page_arg` parses `?page=` safely: clamps to >= 1, never raises."""

from __future__ import annotations

import pytest
from flask import Flask

from bragi.core.pagination import page_arg

app = Flask(__name__)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?page=3", 3),
        ("?page=1", 1),
        ("", 1),  # missing -> default
        ("?page=0", 1),  # clamped up
        ("?page=-4", 1),  # clamped up
        ("?page=abc", 1),  # non-int -> default, not a 500
        ("?page=", 1),  # empty -> default
        ("?page=2.5", 1),  # float string is not an int
    ],
)
def test_page_arg(query: str, expected: int) -> None:
    with app.test_request_context(f"/{query}"):
        assert page_arg() == expected


def test_page_arg_custom_default_is_also_clamped() -> None:
    with app.test_request_context("/?page=nope"):
        assert page_arg(default=5) == 5
        assert page_arg(default=0) == 1
