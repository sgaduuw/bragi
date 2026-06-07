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


def _pm_with_claims(claimed: bool | None) -> Any:
    """Stub a plugin manager whose claims_root_route hook returns the
    given value (None = no plugin claims, True = a plugin owns /)."""
    pm = MagicMock()
    pm.hook.claims_root_route.return_value = claimed
    return pm


def test_no_home_page_and_no_post_index_is_welcome_fallback() -> None:
    site = FakeSite(home_page_id=None)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(False),
            pm=_pm_with_claims(None),
        )
        is True
    )


def test_has_home_page_is_not_welcome_fallback() -> None:
    site = FakeSite(home_page_id=42)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(False),
            pm=_pm_with_claims(None),
        )
        is False
    )


def test_has_post_index_is_not_welcome_fallback() -> None:
    site = FakeSite(home_page_id=None)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(True),
            pm=_pm_with_claims(None),
        )
        is False
    )


def test_has_both_is_not_welcome_fallback() -> None:
    site = FakeSite(home_page_id=42)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(True),
            pm=_pm_with_claims(None),
        )
        is False
    )


def test_claims_root_route_true_suppresses_welcome_fallback() -> None:
    site = FakeSite(home_page_id=None)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(False),
            pm=_pm_with_claims(True),
        )
        is False
    )


def test_claims_root_route_none_does_not_suppress() -> None:
    site = FakeSite(home_page_id=None)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(False),
            pm=_pm_with_claims(None),
        )
        is True
    )


def test_claims_root_route_false_does_not_suppress() -> None:
    """False is rarely useful but documented as 'do not claim'. It
    must NOT suppress, since suppression is opt-in via True."""
    site = FakeSite(home_page_id=None)
    assert (
        _is_welcome_fallback(
            site,
            session=_session_with_post_index(False),
            pm=_pm_with_claims(False),
        )
        is True
    )
