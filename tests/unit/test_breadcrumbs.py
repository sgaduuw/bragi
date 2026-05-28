"""Unit tests for the breadcrumb helper.

Views call `set_breadcrumbs(*crumbs)` before render; the admin
context processor exposes `g.breadcrumbs` to templates. Repeat
calls within one request overwrite (not append) so that a chain
of decorators or context processors cannot accidentally pollute
each other's chains.
"""

from __future__ import annotations

import dataclasses

import pytest
from flask import Flask, g

from bragi.api import Crumb, set_breadcrumbs


@pytest.fixture
def app() -> Flask:
    return Flask("test-breadcrumbs")


def test_crumb_is_frozen_dataclass() -> None:
    c = Crumb("Posts", "post_admin.list_posts")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.label = "Other"  # type: ignore[misc]


def test_crumb_defaults() -> None:
    c = Crumb("Posts", "post_admin.list_posts")
    assert c.values is None


def test_crumb_terminal_has_no_endpoint() -> None:
    c = Crumb("My imported post", None)
    assert c.endpoint is None


def test_set_breadcrumbs_writes_g(app: Flask) -> None:
    with app.test_request_context("/"):
        set_breadcrumbs(
            Crumb("Posts", "post_admin.list_posts"),
            Crumb("Editing 'X'", None),
        )
        assert g.breadcrumbs == (
            Crumb("Posts", "post_admin.list_posts"),
            Crumb("Editing 'X'", None),
        )


def test_set_breadcrumbs_overwrites_not_appends(app: Flask) -> None:
    """Repeat calls within one request overwrite. A view that
    builds its chain in steps should still produce the right shape.
    """
    with app.test_request_context("/"):
        set_breadcrumbs(Crumb("A", None))
        set_breadcrumbs(Crumb("B", None), Crumb("C", None))
        assert g.breadcrumbs == (Crumb("B", None), Crumb("C", None))


def test_set_breadcrumbs_empty_clears(app: Flask) -> None:
    with app.test_request_context("/"):
        set_breadcrumbs(Crumb("A", None))
        set_breadcrumbs()
        assert g.breadcrumbs == ()


def test_crumb_with_values() -> None:
    c = Crumb("Editing post 5", "post_admin.edit_post", {"post_id": 5})
    assert c.values == {"post_id": 5}
