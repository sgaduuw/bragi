"""Tests for `bragi.contrib.redirects`.

Plugin smoke + DB-backed cases for `resolve_redirect`. Admin
Blueprint, hit-count tracking, and slug-change auto-redirect
tests land alongside those features.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from bragi.api import RedirectTarget
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.plugins import create_plugin_manager
from tests.conftest import make_test_user


def _make_site(session: Session, *, slug: str = "blog") -> Site:
    owner = make_test_user(session)
    site = Site(
        slug=slug,
        hostname=f"{slug}.example.com",
        title=f"{slug} test site",
        canonical_url=f"https://{slug}.example.com",
        owner_user_id=owner.id,
    )
    session.add(site)
    session.flush()
    return site


def test_redirects_plugin_is_registered() -> None:
    """The plugin's hookimpl is reachable via firstresult dispatch."""
    pm = create_plugin_manager()
    # firstresult; with no matching plugin, result is None.
    result = pm.hook.resolve_redirect(site=None, path="/anything")
    assert result is None


def test_resolve_redirect_returns_none_when_site_is_none() -> None:
    """No site context means there's nothing to look up against."""
    from bragi.contrib.redirects.plugin import resolve_redirect

    assert resolve_redirect(site=None, path="/old") is None


def test_resolve_redirect_finds_exact_match(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A seeded exact-match redirect is found and returned."""
    site = _make_site(db_session)
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/old-url",
            target="/new-url",
            status_code=301,
            match_type=MatchType.EXACT,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.commit()

    with patch("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory):
        from bragi.contrib.redirects.plugin import resolve_redirect

        result = resolve_redirect(site=site, path="/old-url")

    assert isinstance(result, RedirectTarget)
    assert result.target == "/new-url"
    assert result.status_code == 301
    assert result.source == RedirectSource.MANUAL


def test_resolve_redirect_returns_none_when_no_match(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A path that isn't in the table returns None."""
    site = _make_site(db_session)
    db_session.commit()

    with patch("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory):
        from bragi.contrib.redirects.plugin import resolve_redirect

        result = resolve_redirect(site=site, path="/never-existed")

    assert result is None


def test_resolve_redirect_ignores_inactive_rows(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Rows with `active=False` are skipped."""
    site = _make_site(db_session)
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/old",
            target="/new",
            active=False,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.commit()

    with patch("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory):
        from bragi.contrib.redirects.plugin import resolve_redirect

        result = resolve_redirect(site=site, path="/old")

    assert result is None


def test_resolve_redirect_is_per_site(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A redirect from site A is not visible when looking up against site B."""
    site_a = _make_site(db_session, slug="a")
    site_b = _make_site(db_session, slug="b")
    db_session.add(
        Redirect(
            site_id=site_a.id,
            source_path="/shared",
            target="/a-target",
            source=RedirectSource.MANUAL,
        )
    )
    db_session.commit()

    with patch("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory):
        from bragi.contrib.redirects.plugin import resolve_redirect

        from_a = resolve_redirect(site=site_a, path="/shared")
        from_b = resolve_redirect(site=site_b, path="/shared")

    assert from_a is not None and from_a.target == "/a-target"
    assert from_b is None


def test_redirect_source_path_must_be_nonempty(db_session: Session) -> None:
    """Empty `source_path` is rejected at the DB level.

    Without the CHECK constraint, PR8's SQL-side PREFIX resolver
    (`substr(:path, 1, length(source_path)) = source_path`) would
    collapse to `'' == ''` for every incoming path and the empty-
    prefix row would hijack every URL on the site (subject to
    longer matches winning, which most sites won't have). Closes
    #184.
    """
    from sqlalchemy.exc import IntegrityError

    site = _make_site(db_session, slug="ck")
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="",  # rejected by ck_redirects_source_path_nonempty
            target="/somewhere",
            source=RedirectSource.MANUAL,
        )
    )
    try:
        db_session.commit()
    except IntegrityError:
        db_session.rollback()
    else:  # pragma: no cover
        raise AssertionError("empty source_path was accepted")
