"""Truth table for the welcome-fallback detector."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from bragi.contrib.admin_notices.service import _is_welcome_fallback


class FakeSite:
    def __init__(
        self,
        *,
        site_id: int = 1,
        home_page_id: int | None = None,
        theme: str | None = None,
    ) -> None:
        self.id = site_id
        self.home_page_id = home_page_id
        self.theme = theme


def _session_with_post_index(has_published_post_index: bool) -> Any:
    """Stub a session whose 'has any published post_index for this site?'
    query returns True or False."""
    session = MagicMock()
    session.scalar.return_value = has_published_post_index
    return session


def test_no_home_page_and_no_post_index_is_welcome_fallback() -> None:
    site = FakeSite(home_page_id=None)
    assert _is_welcome_fallback(site, session=_session_with_post_index(False)) is True


def test_has_home_page_is_not_welcome_fallback() -> None:
    site = FakeSite(home_page_id=42)
    assert _is_welcome_fallback(site, session=_session_with_post_index(False)) is False


def test_has_post_index_is_not_welcome_fallback() -> None:
    site = FakeSite(home_page_id=None)
    assert _is_welcome_fallback(site, session=_session_with_post_index(True)) is False


def test_has_both_is_not_welcome_fallback() -> None:
    site = FakeSite(home_page_id=42)
    assert _is_welcome_fallback(site, session=_session_with_post_index(True)) is False


def test_custom_theme_suppresses_welcome_fallback() -> None:
    """v1.28.1 band-aid: a non-default theme presumes to own ``/`` and
    suppresses the notice even when the page-tree conditions would
    otherwise fire. See issue #389 for the proper v1.29.0 fix via a
    new claims_root_route hookspec."""
    site = FakeSite(home_page_id=None, theme="zelda")
    assert _is_welcome_fallback(site, session=_session_with_post_index(False)) is False


def test_default_theme_does_not_suppress() -> None:
    """The default theme uses the welcome-page fallback, so it must NOT
    suppress."""
    site = FakeSite(home_page_id=None, theme="default")
    assert _is_welcome_fallback(site, session=_session_with_post_index(False)) is True


def test_none_theme_does_not_suppress() -> None:
    """site.theme=None is equivalent to default-theme behaviour for the
    purposes of this detector — the welcome fallback still fires."""
    site = FakeSite(home_page_id=None, theme=None)
    assert _is_welcome_fallback(site, session=_session_with_post_index(False)) is True
