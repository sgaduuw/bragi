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
    ) -> None:
        self.id = site_id
        self.home_page_id = home_page_id


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
