"""Tests for `bragi.contrib.redirects`.

Plugin smoke + DB-backed cases for `resolve_redirect`. Admin
Blueprint, hit-count tracking, and slug-change auto-redirect
tests land alongside those features.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import RedirectTarget
from bragi.core.models.page import PageStatus
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


def _seed_move_pages(db, *, published: bool):
    """Create site + OLD/NEW parents + a child P under OLD. Returns
    (site_id, child, old_parent_id, new_parent_id) with the child
    already moved under NEW in the session (parent_id reassigned)."""
    from bragi.core.models.page import Page, PageStatus

    user = make_test_user(db)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db.add(site)
    db.flush()

    def _page(slug, parent_id, status):
        p = Page(
            site_id=site.id,
            parent_id=parent_id,
            slug=slug,
            title=slug,
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=status,
        )
        db.add(p)
        db.flush()
        return p

    status = PageStatus.PUBLISHED if published else PageStatus.DRAFT
    old = _page("old", None, PageStatus.PUBLISHED)
    new = _page("new", None, PageStatus.PUBLISHED)
    child = _page("child", old.id, status)
    old_parent_id = child.parent_id
    child.parent_id = new.id
    db.commit()
    return site.id, child, old_parent_id, new.id


def test_parent_move_published_inserts_prefix_301(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    from bragi.contrib.redirects.plugin import on_post_updated

    site_id, child, old_parent_id, new_parent_id = _seed_move_pages(db_session, published=True)
    before = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": old_parent_id}
    after = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": new_parent_id}

    on_post_updated(item=child, before=before, after=after, session=db_session)

    db_session.expire_all()
    rows = db_session.execute(select(Redirect).where(Redirect.site_id == site_id)).scalars().all()
    move_rows = [r for r in rows if r.source == RedirectSource.PARENT_MOVE]
    assert len(move_rows) == 1
    row = move_rows[0]
    assert row.source_path == "/old/child/"
    assert row.target == "/new/child/"
    assert row.match_type == MatchType.PREFIX


def test_parent_move_draft_inserts_no_redirect(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    from bragi.contrib.redirects.plugin import on_post_updated

    site_id, child, old_parent_id, new_parent_id = _seed_move_pages(db_session, published=False)
    before = {"slug": "child", "status": PageStatus.DRAFT, "parent_id": old_parent_id}
    after = {"slug": "child", "status": PageStatus.DRAFT, "parent_id": new_parent_id}

    on_post_updated(item=child, before=before, after=after, session=db_session)

    db_session.expire_all()
    rows = db_session.execute(select(Redirect).where(Redirect.site_id == site_id)).scalars().all()
    assert rows == []


def test_parent_move_multilevel_reconstructs_old_path(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """The old path is rebuilt by walking the full pre-move parent
    chain, not just the immediate parent."""
    from bragi.contrib.redirects.plugin import on_post_updated
    from bragi.core.models.page import Page, PageStatus

    user = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()

    def _page(slug, parent_id):
        p = Page(
            site_id=site.id,
            parent_id=parent_id,
            slug=slug,
            title=slug,
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
        )
        db_session.add(p)
        db_session.flush()
        return p

    a = _page("a", None)
    b = _page("b", a.id)
    child = _page("child", b.id)
    new = _page("new", None)
    old_parent_id = child.parent_id  # b.id, which is nested under a
    child.parent_id = new.id
    db_session.commit()

    before = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": old_parent_id}
    after = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": new.id}
    on_post_updated(item=child, before=before, after=after, session=db_session)

    db_session.expire_all()
    rows = db_session.execute(select(Redirect).where(Redirect.site_id == site.id)).scalars().all()
    move_rows = [r for r in rows if r.source == RedirectSource.PARENT_MOVE]
    assert len(move_rows) == 1
    assert move_rows[0].source_path == "/a/b/child/"
    assert move_rows[0].target == "/new/child/"
    assert move_rows[0].match_type == MatchType.PREFIX


def test_parent_move_with_slug_change_emits_only_parent_move(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A simultaneous slug+parent change emits exactly one parent-move
    PREFIX rule (from the true old path to the fully-new path) and does
    NOT also emit a slug-change rule."""
    from bragi.contrib.redirects.plugin import on_post_updated
    from bragi.core.models.page import PageStatus

    site_id, child, old_parent_id, new_parent_id = _seed_move_pages(db_session, published=True)
    # Change the slug in the same edit as the move.
    child.slug = "kid"
    db_session.commit()

    before = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": old_parent_id}
    after = {"slug": "kid", "status": PageStatus.PUBLISHED, "parent_id": new_parent_id}
    on_post_updated(item=child, before=before, after=after, session=db_session)

    db_session.expire_all()
    rows = db_session.execute(select(Redirect).where(Redirect.site_id == site_id)).scalars().all()
    assert [r.source for r in rows] == [RedirectSource.PARENT_MOVE]
    assert rows[0].source_path == "/old/child/"
    assert rows[0].target == "/new/kid/"
    assert rows[0].match_type == MatchType.PREFIX


def test_on_post_updated_writes_on_supplied_session_not_sessionlocal(
    db_session: Session,
) -> None:
    """Regression guard: the redirects on_post_updated MUST write the
    redirect on the SUPPLIED session, never a fresh SessionLocal. A
    sibling in-session hookimpl (search FTS, internal_links) holds the
    SQLite write lock on the request connection, so a second
    connection's redirect INSERT deadlocks ("database is locked",
    surfaced as a 500 on parent moves in prod). See MEMORY 2026-06-14.
    """
    from unittest.mock import MagicMock

    from bragi.contrib.redirects.plugin import on_post_updated

    site_id, child, old_parent_id, new_parent_id = _seed_move_pages(db_session, published=True)
    before = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": old_parent_id}
    after = {"slug": "child", "status": PageStatus.PUBLISHED, "parent_id": new_parent_id}

    # If on_post_updated opens its own SessionLocal, calling the sentinel
    # raises and the test fails loudly.
    sentinel = MagicMock(side_effect=AssertionError("on_post_updated opened its own SessionLocal"))
    with patch("bragi.contrib.redirects.plugin.SessionLocal", sentinel):
        on_post_updated(item=child, before=before, after=after, session=db_session)

    sentinel.assert_not_called()
    db_session.expire_all()
    rows = db_session.execute(select(Redirect).where(Redirect.site_id == site_id)).scalars().all()
    assert any(r.source == RedirectSource.PARENT_MOVE for r in rows)
