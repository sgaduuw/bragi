"""Scheduled publication races and lifecycle effects on migrated SQLite."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import click
import pytest
from flask import Flask
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.contrib.post.cli import scheduled_publish
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.indexnow_ping import IndexNowPing
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.time import naive_utcnow


@pytest.fixture
def scheduled_posts(
    admin_app_file_db: Flask, file_db_session_factory: sessionmaker[Session]
) -> tuple[int, int]:
    with file_db_session_factory() as db:
        user = User(email="scheduler@example.com", display_name="Scheduler")
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        posts = [
            Post(
                site_id=site.id,
                author_id=user.id,
                slug=f"due-{i}",
                title=f"Due {i}",
                status=PostStatus.SCHEDULED,
                scheduled_for=naive_utcnow() - timedelta(minutes=2 - i),
            )
            for i in range(2)
        ]
        db.add_all(posts)
        db.commit()
        return posts[0].id, posts[1].id


def _candidate_select(statement: str) -> bool:
    return statement.lstrip().startswith("SELECT") and "posts.scheduled_for <=" in statement


@pytest.mark.parametrize("change", ["cancel", "postpone", "edit"])
def test_rechecks_current_schedule_and_content(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    file_db_engine: Engine,
    scheduled_posts: tuple[int, int],
    change: str,
) -> None:
    first, _ = scheduled_posts
    interleaved = []
    seen = []

    def change_after_selection(conn, cursor, statement, parameters, context, executemany):
        if _candidate_select(statement) and not interleaved:
            interleaved.append(True)
            with file_db_session_factory() as db:
                post = db.get(Post, first)
                assert post is not None and post.status == PostStatus.SCHEDULED
                if change == "cancel":
                    post.status = PostStatus.DRAFT
                    post.scheduled_for = None
                elif change == "postpone":
                    post.scheduled_for = naive_utcnow() + timedelta(days=1)
                else:
                    post.title = "Changed after selection"
                db.commit()

    class Probe:
        @hookimpl
        def on_post_published(self, item, session):
            seen.append((item.id, item.title))

    pm = admin_app_file_db.extensions["plugin_manager"]
    pm.register(Probe(), name="schedule-probe")
    event.listen(file_db_engine, "after_cursor_execute", change_after_selection)
    try:
        result = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    finally:
        event.remove(file_db_engine, "after_cursor_execute", change_after_selection)
        pm.unregister(name="schedule-probe")
    assert result.exit_code == 0, result.output
    assert interleaved == [True]
    with file_db_session_factory() as db:
        post = db.get(Post, first)
        if change == "edit":
            assert (first, "Changed after selection") in seen
        else:
            assert post.status == (PostStatus.DRAFT if change == "cancel" else PostStatus.SCHEDULED)
            assert first not in [post_id for post_id, title in seen]


def test_overlapping_workers_commit_each_publication_once(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    file_db_engine: Engine,
    scheduled_posts: tuple[int, int],
) -> None:
    barrier = Barrier(2)
    selections = []
    effects = []

    def overlap(conn, cursor, statement, parameters, context, executemany):
        if _candidate_select(statement):
            selections.append(True)
            barrier.wait(timeout=10)

    class Probe:
        @hookimpl
        def on_post_published(self, item, session):
            # A real transactional side effect without a deduplication constraint.
            session.add(
                AuditLog(action="test.publish", target_id=item.id, occurred_at=naive_utcnow())
            )
            effects.append(item.id)

    pm = admin_app_file_db.extensions["plugin_manager"]
    pm.register(Probe(), name="schedule-probe")
    event.listen(file_db_engine, "after_cursor_execute", overlap)

    def run_worker(_):
        # Click's CliRunner captures global stdout, so run callbacks directly in threads.
        with admin_app_file_db.app_context(), click.Context(scheduled_publish):
            scheduled_publish.callback(dry_run=False)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run_worker, range(2)))
    finally:
        event.remove(file_db_engine, "after_cursor_execute", overlap)
        pm.unregister(name="schedule-probe")
    assert selections == [True, True]
    assert sorted(effects) == sorted(scheduled_posts)
    with file_db_session_factory() as db:
        assert sorted(
            db.scalars(select(AuditLog.target_id).where(AuditLog.action == "test.publish"))
        ) == sorted(scheduled_posts)


@pytest.mark.parametrize("failure", ["hook", "flush"])
def test_failure_rolls_back_effects_continues_batch_and_can_retry(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    scheduled_posts: tuple[int, int],
    failure: str,
) -> None:
    first, second = scheduled_posts

    class FailingHook:
        @hookimpl(trylast=True)
        def on_post_published(self, item, session):
            session.add(
                AuditLog(action="test.publish", target_id=item.id, occurred_at=naive_utcnow())
            )
            session.flush()
            if item.id == first:
                if failure == "flush":
                    session.add(
                        IndexNowPing(
                            site_id=-1, url="https://invalid.example/", not_before=naive_utcnow()
                        )
                    )
                    session.flush()
                raise RuntimeError("injected publish failure")

    pm = admin_app_file_db.extensions["plugin_manager"]
    pm.register(FailingHook(), name="failing-schedule-probe")
    try:
        result = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    finally:
        pm.unregister(name="failing-schedule-probe")
    with file_db_session_factory() as db:
        assert db.get(Post, first).status == PostStatus.SCHEDULED
        assert db.get(Post, first).published_at is None
        assert db.get(Post, second).status == PostStatus.PUBLISHED
        assert list(
            db.scalars(select(AuditLog.target_id).where(AuditLog.action == "test.publish"))
        ) == [second]
    assert result.exit_code != 0, result.output
    retry = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    assert retry.exit_code == 0, retry.output
    with file_db_session_factory() as db:
        assert db.get(Post, first).status == PostStatus.PUBLISHED
    again = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    assert again.exit_code == 0 and "nothing due" in again.output


def test_cache_failure_reports_committed_publication_and_does_not_republish(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    scheduled_posts: tuple[int, int],
) -> None:
    seen = []

    class FailingPurge:
        @hookimpl
        def on_post_published(self, item, session):
            seen.append(item.id)

        @hookimpl
        def on_cache_purge(self, scope, key):
            raise RuntimeError("cache unavailable")

    pm = admin_app_file_db.extensions["plugin_manager"]
    pm.register(FailingPurge(), name="failing-purge")
    try:
        result = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
        again = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    finally:
        pm.unregister(name="failing-purge")
    assert result.exit_code == 0, result.output
    assert "2 post(s) published" in result.output
    assert "cache purge failed" in result.output.lower()
    assert "FAILED id=" not in result.output
    assert again.exit_code == 0 and "nothing due" in again.output
    assert sorted(seen) == sorted(scheduled_posts)
    with file_db_session_factory() as db:
        assert all(
            db.get(Post, post_id).status == PostStatus.PUBLISHED for post_id in scheduled_posts
        )


def test_scheduled_publish_audit_and_original_publication_date(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    scheduled_posts: tuple[int, int],
) -> None:
    first, second = scheduled_posts
    original = naive_utcnow() - timedelta(days=30)
    with file_db_session_factory() as db:
        db.get(Post, first).published_at = original
        scheduled_for = db.get(Post, second).scheduled_for
        db.commit()
    started = naive_utcnow()
    result = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    assert result.exit_code == 0, result.output
    with file_db_session_factory() as db:
        assert db.get(Post, first).published_at == original
        assert started <= db.get(Post, second).published_at <= naive_utcnow()
        rows = list(db.scalars(select(AuditLog).where(AuditLog.action == "post.updated")))
        assert sorted(r.target_id for r in rows) == sorted(scheduled_posts)
        assert all(r.actor_user_id is None and r.site_id is not None for r in rows)
        row = next(r for r in rows if r.target_id == second)
        assert row.extra["source"] == "scheduled-publish"
        assert row.extra["scheduled_for"] == scheduled_for.isoformat()


@pytest.mark.usefixtures("editor_client")
def test_scheduled_and_manual_publish_persist_same_lifecycle_effects(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
    scheduled_posts: tuple[int, int],
) -> None:
    from flask import g
    from sqlalchemy import text

    from bragi.contrib.auth_local.passwords import hash_password
    from bragi.core.models.activitypub import ActivityPubFollower, ActivityPubOutbox
    from bragi.core.models.internal_link import InternalLink
    from bragi.core.models.local_credential import LocalCredential
    from bragi.core.models.page import Page, PageKind
    from bragi.core.models.webmention import WebmentionOutbox
    from bragi.core.render.markdown import render_markdown
    from tests.integration.test_lifecycle_commit import EMAIL, HOST, PASSWORD, _csrf_token, _login

    scheduled_id, manual_id = scheduled_posts
    with file_db_session_factory() as db:
        scheduled = db.get(Post, scheduled_id)
        site = db.get(Site, scheduled.site_id)
        user = db.get(User, scheduled.author_id)
        user.email = EMAIL
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        site.extra_settings = {"indexnow_key": "abcdef0123456789abcdef0123456789"}
        index = Page(
            site_id=site.id,
            author_id=user.id,
            slug="posts",
            title="Posts",
            kind=PageKind.POST_INDEX,
            status="published",
        )
        target = Page(
            site_id=site.id, author_id=user.id, slug="target", title="Target", status="published"
        )
        db.add_all([index, target])
        db.add(
            ActivityPubFollower(
                site_id=site.id,
                actor_url="https://social.example/actor",
                inbox_url="https://social.example/inbox",
            )
        )
        db.delete(db.get(Post, manual_id))
        db.commit()
        target_id = target.id
        site_id = site.id

    with admin_app_file_db.app_context(), file_db_session_factory() as db:
        g.site = db.get(Site, site_id)
        markdown = f"Lifecycle needle [target](page:{target_id}) and [external](https://example.net/article)."
        html = render_markdown(markdown)
        assert f'data-bragi-link="page:{target_id}"' in html
        post = db.get(Post, scheduled_id)
        post.body_markdown = markdown
        post.body_html = html
        db.commit()
        assert list(db.scalars(select(ActivityPubOutbox))) == []
        assert list(db.scalars(select(WebmentionOutbox))) == []
        assert list(db.scalars(select(IndexNowPing))) == []
        assert list(db.scalars(select(InternalLink))) == []
        assert db.execute(text("SELECT rowid FROM posts_fts")).all() == []

    client = admin_app_file_db.test_client()
    _login(client)
    response = client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "Manual publication",
            "slug": "due-1",
            "body_markdown": markdown,
            "status": "published",
            "_csrf_token": _csrf_token(client),
        },
        headers={"Host": HOST},
    )
    assert response.status_code == 302, response.data
    with file_db_session_factory() as db:
        manual_id = db.scalar(select(Post.id).where(Post.slug == "due-1"))
        assert manual_id is not None
    expected_ids = {scheduled_id, manual_id}
    result = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    assert result.exit_code == 0, result.output
    again = admin_app_file_db.test_cli_runner().invoke(args=["scheduled-publish"])
    assert again.exit_code == 0 and "nothing due" in again.output

    with file_db_session_factory() as db:
        assert (
            set(
                db.execute(
                    text("SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'needle'")
                ).scalars()
            )
            == expected_ids
        )
        edges = list(db.scalars(select(InternalLink)))
        assert {(r.source_id, r.target_type, r.target_id) for r in edges} == {
            (scheduled_id, "page", target_id),
            (manual_id, "page", target_id),
        }
        assert sorted(db.scalars(select(ActivityPubOutbox.post_id))) == sorted(expected_ids)
        assert sorted(db.scalars(select(WebmentionOutbox.post_id))) == sorted(expected_ids)
        assert set(db.scalars(select(WebmentionOutbox.target_url))) == {
            "https://example.net/article"
        }
        assert set(db.scalars(select(IndexNowPing.url))) == {
            "https://blog.example.com/posts/due-0/",
            "https://blog.example.com/posts/due-1/",
        }
