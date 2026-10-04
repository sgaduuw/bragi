"""Scheduling through real post forms, with UTC persisted independently of the browser."""

from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from bragi.contrib.post import admin
from bragi.core.models.post import Post
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.site import Site
from tests.conftest import csrf_token
from tests.contrib import test_post_admin

admin_app = test_post_admin.admin_app
_login = test_post_admin._login


@pytest.fixture
def editor(admin_app, db_session_factory, monkeypatch):
    monkeypatch.setattr(admin, "naive_utcnow", lambda: datetime(2026, 1, 1))
    with db_session_factory() as db:
        site = db.scalar(select(Site))
        site.timezone = "Europe/Amsterdam"
        post = db.scalar(select(Post))
        post_id = post.id
        db.commit()
    client = admin_app.test_client()
    _login(client)
    return client, f"/admin/sites/blog/posts/{post_id}/edit", post_id


def form(client, path, **changes):
    response = client.get(path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    token = soup.select_one('[name="_edit_token"]')
    data = {
        "_csrf_token": csrf_token(client),
        "_edit_token": token["value"],
        "title": "Scheduled writing",
        "slug": "scheduled-writing",
        "body_markdown": "Keep my writing",
        "status": "scheduled",
        "scheduled_for": "2026-07-01T12:30:00",
        "schedule_timezone": "Europe/Amsterdam",
    }
    data.update(changes)
    return data


def test_schedule_create_edit_and_cancel(editor, db_session_factory):
    client, path, post_id = editor
    new = "/admin/sites/blog/posts/new"
    assert client.post(new, data=form(client, new, slug="new-schedule")).status_code == 302
    assert client.post(path, data=form(client, path)).status_code == 302
    with db_session_factory() as db:
        posts = list(db.scalars(select(Post)))
        assert len(posts) == 2
        assert all(p.scheduled_for == datetime(2026, 7, 1, 10, 30) for p in posts)
        assert all(p.status == "scheduled" and p.published_at is None for p in posts)
    soup = BeautifulSoup(client.get(path).data, "html.parser")
    assert soup.select_one('[name="scheduled_for"]')["value"] == "2026-07-01T12:30:00"
    assert soup.select_one('[name="schedule_timezone"]')["value"] == "Europe/Amsterdam"
    assert "+02:00" in soup.get_text()
    assert (
        client.post(path, data=form(client, path, scheduled_for="2026-12-01T12:30")).status_code
        == 302
    )
    with db_session_factory() as db:
        assert db.get(Post, post_id).scheduled_for == datetime(2026, 12, 1, 11, 30)
    assert client.post(path, data=form(client, path, status="draft")).status_code == 302
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        assert (p.status, p.scheduled_for, p.published_at) == ("draft", None, None)


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("", "required"),
        ("nonsense", "valid"),
        ("2026-01-01T00:00", "future"),
        ("2026-03-29T02:30", "does not exist"),
        ("2026-10-25T02:30", "ambiguous"),
        ("2026-07-01T12:30+03:00", "valid"),
    ],
)
def test_invalid_schedule_keeps_writing_and_database(editor, db_session_factory, raw, reason):
    client, path, post_id = editor
    response = client.post(path, data=form(client, path, scheduled_for=raw))
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert reason in soup.get_text().lower()
    assert soup.select_one('[name="body_markdown"]').get_text() == "Keep my writing"
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        assert (p.status, p.scheduled_for, p.title) == ("draft", None, "Hello World")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-03-29T01:30", datetime(2026, 3, 29, 0, 30)),
        ("2026-03-29T03:30", datetime(2026, 3, 29, 1, 30)),
        ("2026-10-25T03:30", datetime(2026, 10, 25, 2, 30)),
    ],
)
def test_dst_boundaries_store_correct_utc(editor, db_session_factory, raw, expected):
    client, path, post_id = editor
    assert client.post(path, data=form(client, path, scheduled_for=raw)).status_code == 302
    with db_session_factory() as db:
        assert db.get(Post, post_id).scheduled_for == expected


def test_unchanged_overdue_schedule_is_not_reset(editor, db_session_factory):
    client, path, post_id = editor
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        p.status, p.scheduled_for = "scheduled", datetime(2025, 12, 1, 11, 30, 15)
        db.commit()
    response = client.post(path, data=form(client, path, scheduled_for="2025-12-01T12:30:15"))
    assert response.status_code == 302
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        assert (p.status, p.scheduled_for) == ("scheduled", datetime(2025, 12, 1, 11, 30, 15))


def test_timezone_change_does_not_reinterpret_existing_schedule(editor, db_session_factory):
    client, path, post_id = editor
    assert client.post(path, data=form(client, path)).status_code == 302
    old_form = form(client, path)
    with db_session_factory() as db:
        db.scalar(select(Site)).timezone = "America/New_York"
        db.commit()
    response = client.post(path, data=old_form)
    assert response.status_code == 200
    assert "timezone changed" in response.get_data(as_text=True).lower()
    soup = BeautifulSoup(client.get(path).data, "html.parser")
    assert soup.select_one('[name="scheduled_for"]')["value"] == "2026-07-01T06:30:00"
    with db_session_factory() as db:
        assert db.get(Post, post_id).scheduled_for == datetime(2026, 7, 1, 10, 30)


def test_invalid_site_zone_is_actionable_and_cancellation_still_works(editor, db_session_factory):
    client, path, post_id = editor
    with db_session_factory() as db:
        db.scalar(select(Site)).timezone = "not/a-zone"
        p = db.get(Post, post_id)
        p.status, p.scheduled_for = "scheduled", datetime(2027, 1, 1)
        db.commit()
    response = client.get(path)
    assert response.status_code == 200
    assert "timezone" in response.get_data(as_text=True).lower()
    response = client.post(path, data=form(client, path, schedule_timezone="not/a-zone"))
    assert response.status_code == 200
    with db_session_factory() as db:
        assert db.get(Post, post_id).scheduled_for == datetime(2027, 1, 1)
    response = client.post(path, data=form(client, path, status="draft"))
    assert response.status_code == 302
    with db_session_factory() as db:
        assert db.get(Post, post_id).scheduled_for is None


@pytest.mark.parametrize(
    "headers", [{}, {"HX-Request": "true"}, {"HX-Request": "true", "HX-Boosted": "true"}]
)
def test_scheduled_list_distinguishes_future_overdue_and_missing_time(
    editor, db_session_factory, headers
):
    client, _, post_id = editor
    for when, state in [
        (datetime(2099, 7, 1, 10, 30), "future"),
        (datetime(2020, 1, 1), "overdue"),
        (None, "missing"),
    ]:
        with db_session_factory() as db:
            p = db.get(Post, post_id)
            p.status, p.scheduled_for = "scheduled", when
            db.commit()
        response = client.get("/admin/sites/blog/posts/", headers=headers)
        assert response.status_code == 200
        soup = BeautifulSoup(response.data, "html.parser")
        status = soup.select_one(f"#post-status-cell-{post_id} [data-schedule-state]")
        assert status is not None and status["data-schedule-state"] == state
        if when:
            assert "Europe/Amsterdam" in status.get_text()
        if state == "overdue":
            assert "task runner" in status.get_text().lower()


def test_inline_cancel_clears_scheduled_time(editor, db_session_factory):
    client, _, post_id = editor
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        p.status, p.scheduled_for = "scheduled", datetime(2099, 1, 1)
        db.commit()
    response = client.patch(
        f"/admin/sites/blog/posts/{post_id}/patch/status",
        data={"_csrf_token": csrf_token(client), "status": "draft"},
    )
    assert response.status_code == 200
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        assert (p.status, p.scheduled_for, p.published_at) == ("draft", None, None)


@pytest.mark.parametrize("existing", [datetime(2026, 10, 25, 0, 30), datetime(2026, 10, 25, 1, 30)])
def test_unchanged_repeated_hour_keeps_its_exact_instant(editor, db_session_factory, existing):
    client, path, post_id = editor
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        p.status, p.scheduled_for = "scheduled", existing
        db.commit()
    response = client.post(path, data=form(client, path, scheduled_for="2026-10-25T02:30:00"))
    assert response.status_code == 302
    with db_session_factory() as db:
        assert db.get(Post, post_id).scheduled_for == existing


def test_schedule_rejects_utc_overflow_without_losing_writing(editor, db_session_factory):
    client, path, post_id = editor
    with db_session_factory() as db:
        db.scalar(select(Site)).timezone = "America/New_York"
        db.commit()
    response = client.post(
        path,
        data=form(
            client, path, scheduled_for="9999-12-31T23:30", schedule_timezone="America/New_York"
        ),
    )
    assert response.status_code == 200
    assert "supported range" in response.get_data(as_text=True)
    with db_session_factory() as db:
        assert db.get(Post, post_id).status == "draft"


def test_existing_schedule_outside_local_range_stays_visible(editor, db_session_factory):
    client, path, post_id = editor
    with db_session_factory() as db:
        p = db.get(Post, post_id)
        p.status, p.scheduled_for = "scheduled", datetime(9999, 12, 31, 23)
        db.commit()
    for target in (path, "/admin/sites/blog/posts/"):
        response = client.get(target)
        assert response.status_code == 200
        assert "9999-12-31 23:00:00+00:00" in response.get_data(as_text=True)


@pytest.mark.parametrize("status", ["draft", "scheduled"])
def test_restoring_scheduled_revision_requires_a_new_schedule(editor, db_session_factory, status):
    client, path, post_id = editor
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.status, post.scheduled_for = "scheduled", datetime(2027, 1, 1)
        db.commit()
    # Cancel, or replace the schedule. Neither permits a revision to reuse it.
    response = client.post(
        path, data=form(client, path, status=status, scheduled_for="2028-01-01T12:00")
    )
    assert response.status_code == 302
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        assert post.status == status
        assert post.scheduled_for == (None if status == "draft" else datetime(2028, 1, 1, 11))
        revision = db.scalar(select(PostRevision).where(PostRevision.post_id == post_id))
        assert revision.status == "scheduled" and revision.title == "Hello World"
        rev_id = revision.id
    response = client.post(
        f"/admin/sites/blog/posts/{post_id}/revisions/{rev_id}/restore",
        data={"_csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        assert (post.status, post.scheduled_for, post.published_at) == ("draft", None, None)
        assert post.title == "Hello World"
    assert "restored as Draft" in response.get_data(as_text=True)
    assert "Choose a new publication time" in response.get_data(as_text=True)


def test_existing_out_of_range_schedule_can_be_replaced(editor, db_session_factory):
    client, path, post_id = editor
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.status, post.scheduled_for = "scheduled", datetime(9999, 12, 31, 23)
        db.commit()
    response = client.post(path, data=form(client, path))
    assert response.status_code == 302
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        assert (post.status, post.scheduled_for) == ("scheduled", datetime(2026, 7, 1, 10, 30))
