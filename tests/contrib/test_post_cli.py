"""Tests for the post plugin CLI (`cms scheduled-publish`)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.apps.admin import create_admin_app
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def seeded(db_session: Session) -> tuple[Site, User]:
    """One site + one author so tests can plant posts cheaply."""
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.commit()
    return site, user


def _make_post(
    db: Session,
    site: Site,
    user: User,
    *,
    slug: str,
    status: str,
    scheduled_for: datetime | None,
    published_at: datetime | None = None,
) -> int:
    """Persist a Post and return its id.

    Returns the id (not the row) so callers can re-fetch in a
    fresh session without tripping over expire-on-commit detach.
    """
    post = Post(
        site_id=site.id,
        slug=slug,
        title=slug.title(),
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=status,
        scheduled_for=scheduled_for,
        published_at=published_at,
    )
    db.add(post)
    db.commit()
    return int(post.id)


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Real admin app so the CLI command runs with the full plugin
    manager wired up (lifecycle hooks dispatch through pm.hook)."""
    del patched_session_locals
    yield create_admin_app()


def test_flips_due_posts_to_published(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    site, user = seeded
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(hours=1)

    with db_session_factory() as db:
        due_id = _make_post(
            db, site, user, slug="due", status=PostStatus.SCHEDULED, scheduled_for=past
        )
        future_id = _make_post(
            db, site, user, slug="future", status=PostStatus.SCHEDULED, scheduled_for=future
        )

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "scheduled-publish"])
    assert result.exit_code == 0, result.output
    assert "1 post(s) published" in result.output

    with db_session_factory() as db:
        after_due = db.get(Post, due_id)
        after_future = db.get(Post, future_id)
    assert after_due is not None and after_future is not None
    assert after_due.status == PostStatus.PUBLISHED
    assert after_due.published_at is not None
    assert after_future.status == PostStatus.SCHEDULED
    assert after_future.published_at is None


def test_dry_run_writes_nothing(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    site, user = seeded
    past = datetime.now(UTC) - timedelta(minutes=1)
    with db_session_factory() as db:
        post_id = _make_post(
            db, site, user, slug="due", status=PostStatus.SCHEDULED, scheduled_for=past
        )

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "scheduled-publish", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would be published" in result.output
    assert f"id={post_id}" in result.output

    with db_session_factory() as db:
        after = db.get(Post, post_id)
    assert after is not None
    assert after.status == PostStatus.SCHEDULED
    assert after.published_at is None


def test_idempotent_no_op_on_second_run(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    site, user = seeded
    past = datetime.now(UTC) - timedelta(minutes=1)
    with db_session_factory() as db:
        _make_post(db, site, user, slug="due", status=PostStatus.SCHEDULED, scheduled_for=past)

    runner = admin_app.test_cli_runner()
    first = runner.invoke(args=["cms", "scheduled-publish"])
    assert first.exit_code == 0, first.output
    assert "1 post(s) published" in first.output

    second = runner.invoke(args=["cms", "scheduled-publish"])
    assert second.exit_code == 0, second.output
    assert "nothing due" in second.output


def test_skips_drafts_and_already_published(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    """The CLI only flips `scheduled` posts. A draft with a past
    `scheduled_for` (shouldn't happen via the admin UI, but guard
    against it) and an already-published post are both untouched."""
    site, user = seeded
    past = datetime.now(UTC) - timedelta(minutes=1)
    earlier = datetime.now(UTC) - timedelta(days=1)
    with db_session_factory() as db:
        draft_id = _make_post(
            db, site, user, slug="draft", status=PostStatus.DRAFT, scheduled_for=past
        )
        already_id = _make_post(
            db,
            site,
            user,
            slug="already",
            status=PostStatus.PUBLISHED,
            scheduled_for=past,
            published_at=earlier,
        )

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "scheduled-publish"])
    assert result.exit_code == 0, result.output
    assert "nothing due" in result.output

    with db_session_factory() as db:
        after_draft = db.get(Post, draft_id)
        after_already = db.get(Post, already_id)
    assert after_draft is not None and after_already is not None
    assert after_draft.status == PostStatus.DRAFT
    assert after_already.status == PostStatus.PUBLISHED
    # Preserved, not bumped to "now". SQLite returns the stored
    # value as a naive datetime; compare in naive form regardless
    # of how it went in.
    assert after_already.published_at is not None
    stored = after_already.published_at
    stored_naive = stored.replace(tzinfo=None) if stored.tzinfo else stored
    assert stored_naive == earlier.replace(tzinfo=None)


def test_fires_on_post_published_hook(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    """A scheduled flip must dispatch `on_post_published` so search
    index, audit log, and any third-party subscribers stay in step
    with the admin's manual publish path."""
    site, user = seeded
    past = datetime.now(UTC) - timedelta(minutes=1)
    with db_session_factory() as db:
        post_id = _make_post(
            db, site, user, slug="due", status=PostStatus.SCHEDULED, scheduled_for=past
        )

    seen: list[int] = []

    class Probe:
        # pluggy needs the project's hookimpl marker; an undecorated
        # method on the registered object isn't picked up.
        @hookimpl
        def on_post_published(self, item: object, session: object) -> None:
            del session
            assert isinstance(item, Post)
            seen.append(item.id)

    pm = admin_app.extensions["plugin_manager"]
    pm.register(Probe(), name="test-probe")
    try:
        runner = admin_app.test_cli_runner()
        result = runner.invoke(args=["cms", "scheduled-publish"])
        assert result.exit_code == 0, result.output
    finally:
        pm.unregister(name="test-probe")

    assert seen == [post_id]
