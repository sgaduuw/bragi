"""Tests for prefix and regex match types in resolve_redirect (#13).

Covers:
- Resolution priority: exact beats prefix beats regex.
- Longest-prefix tie-breaker.
- Regex capture-group substitution into target.
- Bad regex patterns are logged + skipped, not 500.
- hit_count still bumps on the matched row, regardless of match type.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    yield create_delivery_app()


def _site_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id


def _add(
    db_session_factory: sessionmaker[Session],
    site_id: int,
    *,
    source_path: str,
    target: str,
    match_type: str,
    status_code: int = 301,
) -> int:
    with db_session_factory() as db:
        row = Redirect(
            site_id=site_id,
            source_path=source_path,
            target=target,
            match_type=match_type,
            status_code=status_code,
            source=RedirectSource.MANUAL,
            active=True,
        )
        db.add(row)
        db.commit()
        return row.id


# --------------------------- prefix ---------------------------


def test_prefix_appends_unmatched_tail(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path="/old/",
        target="/new/",
        match_type=MatchType.PREFIX,
    )

    resp = delivery_app.test_client().get(
        "/old/foo/bar", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/new/foo/bar")


def test_prefix_longest_wins(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Two PREFIX rules cover the same path; the longer source_path wins."""
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path="/old/",
        target="/A/",
        match_type=MatchType.PREFIX,
    )
    _add(
        db_session_factory,
        site_id,
        source_path="/old/sub/",
        target="/B/",
        match_type=MatchType.PREFIX,
    )

    resp = delivery_app.test_client().get(
        "/old/sub/x", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/B/x")


# --------------------------- regex ---------------------------


def test_regex_capture_groups_expand_into_target(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path=r"^/blog/(\d+)/$",
        target=r"/posts/\1/",
        match_type=MatchType.REGEX,
    )

    resp = delivery_app.test_client().get(
        "/blog/42/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/posts/42/")


def test_regex_requires_full_match(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Regex uses fullmatch; partial matches don't fire."""
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path=r"^/blog/\d+$",
        target=r"/wrong/",
        match_type=MatchType.REGEX,
    )
    # Partial: trailing slash means the pattern doesn't fullmatch.
    resp = delivery_app.test_client().get(
        "/blog/42/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


def test_bad_regex_is_skipped_not_500(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A malformed pattern logs + skips; a good rule still fires."""
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path=r"(unbalanced",  # bad
        target=r"/never/",
        match_type=MatchType.REGEX,
    )
    _add(
        db_session_factory,
        site_id,
        source_path=r"^/legacy/(\w+)$",
        target=r"/new/\1",
        match_type=MatchType.REGEX,
    )

    resp = delivery_app.test_client().get(
        "/legacy/article", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/new/article")


# --------------------------- priority ---------------------------


def test_exact_beats_prefix_and_regex(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path="/foo/",
        target="/exact-target/",
        match_type=MatchType.EXACT,
    )
    _add(
        db_session_factory,
        site_id,
        source_path="/foo/",
        target="/prefix-target/",
        match_type=MatchType.PREFIX,
    )
    _add(
        db_session_factory,
        site_id,
        source_path=r"^/foo/$",
        target="/regex-target/",
        match_type=MatchType.REGEX,
    )

    resp = delivery_app.test_client().get(
        "/foo/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/exact-target/")


def test_prefix_beats_regex(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    _add(
        db_session_factory,
        site_id,
        source_path="/section/",
        target="/prefix/",
        match_type=MatchType.PREFIX,
    )
    _add(
        db_session_factory,
        site_id,
        source_path=r"^/section/.*$",
        target="/regex/",
        match_type=MatchType.REGEX,
    )

    resp = delivery_app.test_client().get(
        "/section/page", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/prefix/page")


# --------------------------- hit count ---------------------------


def test_prefix_match_bumps_hit_count(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    rid = _add(
        db_session_factory,
        site_id,
        source_path="/legacy/",
        target="/new/",
        match_type=MatchType.PREFIX,
    )
    delivery_app.test_client().get(
        "/legacy/article", headers={"Host": "blog.example.com"}
    )
    with db_session_factory() as db:
        row = db.get(Redirect, rid)
    assert row is not None
    assert row.hit_count == 1
    assert row.last_hit_at is not None


def test_regex_match_bumps_hit_count(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    rid = _add(
        db_session_factory,
        site_id,
        source_path=r"^/old/(\w+)$",
        target=r"/new/\1",
        match_type=MatchType.REGEX,
    )
    delivery_app.test_client().get("/old/x", headers={"Host": "blog.example.com"})
    with db_session_factory() as db:
        row = db.get(Redirect, rid)
    assert row is not None
    assert row.hit_count == 1


# --------------------------- inactive ---------------------------


def test_inactive_prefix_rule_ignored(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    with db_session_factory() as db:
        db.add(
            Redirect(
                site_id=site_id,
                source_path="/dead/",
                target="/elsewhere/",
                match_type=MatchType.PREFIX,
                status_code=301,
                source=RedirectSource.MANUAL,
                active=False,
            )
        )
        db.commit()
    resp = delivery_app.test_client().get(
        "/dead/page", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404
