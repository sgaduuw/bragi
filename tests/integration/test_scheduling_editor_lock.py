"""Cancellation must read current state under SQLite's writer lock."""

import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from bragi.core.models.activitypub import ActivityPubFollower, ActivityPubOutbox
from bragi.core.models.page import Page, PageKind
from bragi.core.models.post import Post
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.webmention import WebmentionOutbox
from bragi.core.time import naive_utcnow
from tests.conftest import csrf_token
from tests.contrib import test_post_scheduling

admin_app = test_post_scheduling.admin_app
editor = test_post_scheduling.editor


@pytest.fixture
def db_engine(file_db_engine):
    return file_db_engine


@pytest.mark.parametrize("action", ["inline", "restore"])
def test_cancellation_owns_writer_lock_before_loading_post(
    editor, db_engine, db_session_factory, action
):
    client, _, post_id = editor
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.status, post.scheduled_for = "scheduled", datetime(2027, 1, 1)
        revision = PostRevision(
            post_id=post_id, title="Earlier", slug=post.slug, status="scheduled"
        )
        db.add(revision)
        db.commit()
        rev_id = revision.id
    attempts = []

    def competing_writer(conn, cursor, statement, parameters, context, many):
        if not attempts and statement.lstrip().startswith("SELECT") and "FROM posts" in statement:
            other = sqlite3.connect(db_engine.url.database, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                attempts.append(True)
            else:
                attempts.append(False)
            finally:
                other.rollback()
                other.close()

    token = csrf_token(client)
    event.listen(db_engine, "before_cursor_execute", competing_writer)
    try:
        if action == "inline":
            response = client.patch(
                f"/admin/sites/blog/posts/{post_id}/patch/status",
                data={"_csrf_token": token, "status": "draft"},
            )
        else:
            response = client.post(
                f"/admin/sites/blog/posts/{post_id}/revisions/{rev_id}/restore",
                data={"_csrf_token": token},
            )
    finally:
        event.remove(db_engine, "before_cursor_execute", competing_writer)
    assert response.status_code == (200 if action == "inline" else 302)
    assert attempts and all(attempts)
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        assert (post.status, post.scheduled_for) == ("draft", None)


def test_restore_cleans_notifications_when_publication_won_first(editor, db_session_factory):
    client, _, post_id = editor
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.status, post.scheduled_for = "scheduled", naive_utcnow() - timedelta(minutes=1)
        post.body_markdown = "[External](https://outside.example/article)"
        post.body_html = '<p><a href="https://outside.example/article">External</a></p>'
        db.add(
            Page(
                site_id=post.site_id,
                author_id=post.author_id,
                slug="posts",
                title="Posts",
                kind=PageKind.POST_INDEX,
                status="published",
            )
        )
        db.add(
            ActivityPubFollower(
                site_id=post.site_id,
                actor_url="https://remote.example/actor",
                inbox_url="https://remote.example/inbox",
            )
        )
        revision = PostRevision(
            post_id=post_id, title="Earlier", slug=post.slug, status="scheduled"
        )
        db.add(revision)
        db.commit()
        rev_id = revision.id
    result = client.application.test_cli_runner().invoke(args=["scheduled-publish"])
    assert result.exit_code == 0, result.output
    with db_session_factory() as db:
        assert db.get(Post, post_id).status == "published"
        for model in (ActivityPubOutbox, WebmentionOutbox):
            rows = list(db.scalars(select(model)))
            assert len(rows) == 1 and rows[0].post_id == post_id and rows[0].status == "pending"
    response = client.post(
        f"/admin/sites/blog/posts/{post_id}/revisions/{rev_id}/restore",
        data={"_csrf_token": csrf_token(client)},
    )
    assert response.status_code == 302
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        assert (post.status, post.scheduled_for, post.title) == ("draft", None, "Earlier")
        assert list(db.scalars(select(ActivityPubOutbox))) == []
        assert list(db.scalars(select(WebmentionOutbox))) == []


@pytest.mark.parametrize("status", ["scheduled", "invalid"])
def test_rejected_inline_status_does_not_claim_writer_lock(editor, db_engine, monkeypatch, status):
    from bragi.contrib.post import admin

    client, _, post_id = editor
    token = csrf_token(client)
    render = admin.render_template
    claims = []
    probes = []

    def observe_lock(conn, cursor, statement, parameters, context, many):
        if statement == "BEGIN IMMEDIATE":
            claims.append(statement)

    def probe_render(template, *args, **kwargs):
        assert template == "admin/_status_cell.html"
        other = sqlite3.connect(db_engine.url.database, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            probes.append(True)
        finally:
            other.rollback()
            other.close()
        return render(template, *args, **kwargs)

    monkeypatch.setattr(admin, "render_template", probe_render)
    event.listen(db_engine, "before_cursor_execute", observe_lock)
    try:
        response = client.patch(
            f"/admin/sites/blog/posts/{post_id}/patch/status",
            data={"_csrf_token": token, "status": status},
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", observe_lock)
    assert response.status_code == 200
    assert probes == [True]
    assert claims == []
