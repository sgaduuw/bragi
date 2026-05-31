"""Tests for bragi.core.text helpers.

`slugify` is exercised end-to-end by every test that creates a page
or post with an auto-suggested slug; the pure-Python collision
disambiguator `_disambiguate_slug` is exercised here in isolation
so the collision-sequence logic is pinned without dragging in a DB
session.
"""

from __future__ import annotations

from bragi.core.text import _disambiguate_slug


def test_returns_base_when_no_collision() -> None:
    assert _disambiguate_slug("foo", set()) == "foo"


def test_appends_2_when_base_taken() -> None:
    assert _disambiguate_slug("foo", {"foo"}) == "foo-2"


def test_appends_3_when_base_and_2_taken() -> None:
    assert _disambiguate_slug("foo", {"foo", "foo-2"}) == "foo-3"


def test_skips_to_first_free_slot() -> None:
    """If foo, foo-2, foo-3, foo-5 are taken but foo-4 is free,
    the next candidate is foo-4 (not foo-6). Predictable, dense."""
    assert _disambiguate_slug("foo", {"foo", "foo-2", "foo-3", "foo-5"}) == "foo-4"


def test_unrelated_taken_slugs_ignored() -> None:
    """Taken slugs that don't share the base prefix are irrelevant."""
    assert _disambiguate_slug("foo", {"bar", "foobar", "foo-bar"}) == "foo"


def test_base_ending_in_digits_disambiguates_with_dash() -> None:
    """A base like 'top-10' that collides becomes 'top-10-2', not 'top-11'.
    Predictable; the disambiguator never edits the base, only suffixes."""
    assert _disambiguate_slug("top-10", {"top-10"}) == "top-10-2"
