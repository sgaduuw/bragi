"""Integration tests for the 404-triage feature (file-backed DB).

Covers the two surfaces against the migrated, file-backed fixture
(the recorder opens its own `SessionLocal`, so it needs a real shared
DB, not `:memory:`):

* Recording: a real delivery 404 writes/coalesces a `not_founds` row;
  the scanner blocklist, 410s, and `ignored` rows are excluded.
* Admin: multisite scoping, redirect-membership auto-hide, dismiss,
  the suggestion wiring, and full-vs-partial-vs-boosted htmx dispatch.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.user import User

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
HOST_A = "blog.example.com"
HOST_B = "other.example.com"


@pytest.fixture
def delivery_app_file_db(patched_file_session_locals: sessionmaker[Session]) -> Flask:
    """The real delivery app, bound to the migrated file-backed DB.

    Mirrors `admin_app_file_db` (integration conftest) but for the
    read-side app, so the notfound recorder's `after_request` runs and
    its `SessionLocal()` write lands on the same file DB the test reads.
    """
    from bragi.apps.delivery import create_delivery_app

    return create_delivery_app()


def _seed(factory: sessionmaker[Session]) -> None:
    """Owner + local creds + two sites (A, B) + a published page on A."""
    with factory() as db:
        user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
        db.add(user)
        db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        site_a = Site(
            slug="blog",
            hostname=HOST_A,
            title="Blog",
            canonical_url=f"https://{HOST_A}",
            owner_user_id=user.id,
        )
        site_b = Site(
            slug="other",
            hostname=HOST_B,
            title="Other",
            canonical_url=f"https://{HOST_B}",
            owner_user_id=user.id,
        )
        db.add_all([site_a, site_b])
        db.flush()
        # A published page on site A used by the suggestion test.
        db.add(
            Page(
                site_id=site_a.id,
                slug="introduction",
                title="Introduction",
                body_markdown="hi",
                body_html="<p>hi</p>",
                body_excerpt="hi",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
            )
        )
        db.commit()


def _csrf_token(client: FlaskClient) -> str:
    client.get("/auth/login", headers={"Host": HOST_A})
    with client.session_transaction(environ_overrides={"HTTP_HOST": HOST_A}) as sess:
        return sess["_csrf_token"]


def _login(client: FlaskClient) -> None:
    token = _csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def _rows(factory: sessionmaker[Session]) -> list[NotFound]:
    with factory() as db:
        return list(db.execute(select(NotFound)).scalars().all())


# --------------------------------------------------------------------------
# Recording (delivery side)
# --------------------------------------------------------------------------


def test_delivery_404_is_recorded(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    resp = client.get("/never-existed/", headers={"Host": HOST_A, "Referer": "https://ref/"})
    assert resp.status_code == 404

    rows = _rows(file_db_session_factory)
    assert len(rows) == 1
    assert rows[0].path == "/never-existed/"
    assert rows[0].count == 1
    assert rows[0].status == NotFoundStatus.OPEN
    assert rows[0].last_referrer == "https://ref/"


def test_repeat_404_coalesces_and_bumps_count(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    for _ in range(3):
        assert client.get("/dead/", headers={"Host": HOST_A}).status_code == 404

    rows = _rows(file_db_session_factory)
    assert len(rows) == 1
    assert rows[0].count == 3


def test_blocklisted_404_not_recorded(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    assert client.get("/wp-login.php", headers={"Host": HOST_A}).status_code == 404
    assert client.get("/x.php", headers={"Host": HOST_A}).status_code == 404
    assert _rows(file_db_session_factory) == []


def test_410_is_not_recorded(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    with file_db_session_factory() as db:
        site = db.execute(select(Site).where(Site.hostname == HOST_A)).scalar_one()
        db.add(
            Redirect(
                site_id=site.id,
                source_path="/gone/",
                target="/gone/",
                status_code=410,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()
    client = delivery_app_file_db.test_client()
    assert client.get("/gone/", headers={"Host": HOST_A}).status_code == 410
    assert _rows(file_db_session_factory) == []


def test_ignored_row_is_not_rebumped(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    assert client.get("/noise/", headers={"Host": HOST_A}).status_code == 404
    with file_db_session_factory() as db:
        row = db.execute(select(NotFound)).scalar_one()
        row.status = NotFoundStatus.IGNORED
        db.commit()
    # A further hit must not bump the ignored row.
    assert client.get("/noise/", headers={"Host": HOST_A}).status_code == 404
    rows = _rows(file_db_session_factory)
    assert len(rows) == 1
    assert rows[0].count == 1
    assert rows[0].status == NotFoundStatus.IGNORED


def test_404_recorded_against_the_resolved_site(
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = delivery_app_file_db.test_client()
    assert client.get("/only-on-b/", headers={"Host": HOST_B}).status_code == 404
    with file_db_session_factory() as db:
        site_b = db.execute(select(Site).where(Site.hostname == HOST_B)).scalar_one()
        row = db.execute(select(NotFound)).scalar_one()
        assert row.site_id == site_b.id


# --------------------------------------------------------------------------
# Admin surface
# --------------------------------------------------------------------------


def _record_404(delivery_app: Flask, path: str, host: str = HOST_A) -> None:
    assert delivery_app.test_client().get(path, headers={"Host": host}).status_code == 404


def test_admin_list_is_site_scoped(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/a-only/", HOST_A)

    client = admin_app_file_db.test_client()
    _login(client)
    on_a = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A})
    assert on_a.status_code == 200
    assert b"/a-only/" in on_a.data
    # Site B must not see site A's 404.
    on_b = client.get("/admin/sites/other/not-found/", headers={"Host": HOST_A})
    assert on_b.status_code == 200
    assert b"/a-only/" not in on_b.data


def _record_two(delivery_app: Flask, factory: sessionmaker[Session]) -> list[int]:
    _seed(factory)
    _record_404(delivery_app, "/bulk-a/", HOST_A)
    _record_404(delivery_app, "/bulk-b/", HOST_A)
    with factory() as db:
        return [r.id for r in db.execute(select(NotFound)).scalars().all()]


def test_bulk_dismiss_selected(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    ids = _record_two(delivery_app_file_db, file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    resp = client.post(
        "/admin/sites/blog/not-found/bulk/dismiss",
        data={"_csrf_token": _csrf_token(client), "ids": [str(i) for i in ids]},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302
    with file_db_session_factory() as db:
        for i in ids:
            assert db.get(NotFound, i).status == NotFoundStatus.DISMISSED  # type: ignore[union-attr]


def test_bulk_ignore_selected(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    ids = _record_two(delivery_app_file_db, file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    client.post(
        "/admin/sites/blog/not-found/bulk/ignore",
        data={"_csrf_token": _csrf_token(client), "ids": [str(i) for i in ids]},
        headers={"Host": HOST_A},
    )
    with file_db_session_factory() as db:
        for i in ids:
            assert db.get(NotFound, i).status == NotFoundStatus.IGNORED  # type: ignore[union-attr]


def test_bulk_gone_creates_410_redirects_and_hides_rows(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    ids = _record_two(delivery_app_file_db, file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    client.post(
        "/admin/sites/blog/not-found/bulk/gone",
        data={"_csrf_token": _csrf_token(client), "ids": [str(i) for i in ids]},
        headers={"Host": HOST_A},
    )
    with file_db_session_factory() as db:
        reds = db.execute(select(Redirect).where(Redirect.status_code == 410)).scalars().all()
        paths = {r.source_path for r in reds}
        assert paths == {"/bulk-a/", "/bulk-b/"}
        assert all(r.match_type == MatchType.EXACT and r.active for r in reds)
    # The rows auto-hide (an active exact redirect now covers each path).
    body = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A}).data
    assert b"<code>/bulk-a/</code>" not in body
    assert b"<code>/bulk-b/</code>" not in body


def test_bulk_gone_is_idempotent(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    """A second bulk-gone over an already-covered path does not duplicate
    or error (ON CONFLICT DO NOTHING on the redirects unique key)."""
    ids = _record_two(delivery_app_file_db, file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    for _ in range(2):
        client.post(
            "/admin/sites/blog/not-found/bulk/gone",
            data={"_csrf_token": _csrf_token(client), "ids": [str(i) for i in ids]},
            headers={"Host": HOST_A},
        )
    with file_db_session_factory() as db:
        reds = db.execute(select(Redirect).where(Redirect.status_code == 410)).scalars().all()
        assert len(reds) == 2  # not 4


def test_bulk_empty_selection_is_a_noop(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    ids = _record_two(delivery_app_file_db, file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    client.post(
        "/admin/sites/blog/not-found/bulk/dismiss",
        data={"_csrf_token": _csrf_token(client)},  # no ids
        headers={"Host": HOST_A},
    )
    with file_db_session_factory() as db:
        for i in ids:
            assert db.get(NotFound, i).status == NotFoundStatus.OPEN  # type: ignore[union-attr]


def test_bulk_is_site_scoped(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    """Site A's ids posted under site B's URL touch nothing (the site_id
    predicate filters them out)."""
    ids = _record_two(delivery_app_file_db, file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    client.post(
        "/admin/sites/other/not-found/bulk/ignore",
        data={"_csrf_token": _csrf_token(client), "ids": [str(i) for i in ids]},
        headers={"Host": HOST_A},
    )
    with file_db_session_factory() as db:
        for i in ids:
            assert db.get(NotFound, i).status == NotFoundStatus.OPEN  # type: ignore[union-attr]


def test_row_actions_render_as_dropdown_plus_standalone_ignore(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    """The resolving actions collapse into a `row-actions` dropdown; the
    permanent Ignore stays a standalone button. Guards the markup so the
    dismiss/ignore/redirect endpoints stay wired after the UI tidy-up."""
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/tidy/", HOST_A)

    client = admin_app_file_db.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A}).data.decode()
    assert 'class="row-actions"' in body
    assert "Actions" in body
    # The five folded actions + the standalone Ignore are all present.
    assert "notfound_admin.dismiss" not in body  # url_for resolved to a path
    assert "/dismiss" in body
    assert "/ignore" in body
    assert "New page" in body and "New post" in body
    assert "Create redirect" in body and "Mark Gone" in body
    assert ">Ignore</button>" in body


def test_open_row_hidden_when_exact_redirect_covers_it(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/moved/", HOST_A)
    _record_404(delivery_app_file_db, "/still-open/", HOST_A)
    with file_db_session_factory() as db:
        site = db.execute(select(Site).where(Site.hostname == HOST_A)).scalar_one()
        db.add(
            Redirect(
                site_id=site.id,
                source_path="/moved/",
                target="/new-home/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()

    client = admin_app_file_db.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A})
    assert resp.status_code == 200
    assert b"/moved/" not in resp.data  # covered by the redirect -> hidden
    assert b"/still-open/" in resp.data


def test_dismiss_marks_dismissed_and_drops_from_list(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/spam/", HOST_A)
    with file_db_session_factory() as db:
        nf_id = db.execute(select(NotFound.id)).scalar_one()

    client = admin_app_file_db.test_client()
    _login(client)
    token = _csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/not-found/{nf_id}/dismiss",
        data={"_csrf_token": token},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302

    with file_db_session_factory() as db:
        assert db.get(NotFound, nf_id).status == NotFoundStatus.DISMISSED  # type: ignore[union-attr]
    listing = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A})
    # Assert against the table cell, not the whole page: the "Dismissed
    # /spam/." flash banner also renders the path as plain text.
    assert b"<code>/spam/</code>" not in listing.data


def test_dismissed_row_reopens_on_rehit(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    """A soft-dismissed 404 comes back (status -> OPEN, count bumped) when
    the path 404s again, and reappears in the list."""
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/recurring/", HOST_A)
    with file_db_session_factory() as db:
        nf_id = db.execute(select(NotFound.id)).scalar_one()

    client = admin_app_file_db.test_client()
    _login(client)
    client.post(
        f"/admin/sites/blog/not-found/{nf_id}/dismiss",
        data={"_csrf_token": _csrf_token(client)},
        headers={"Host": HOST_A},
    )
    with file_db_session_factory() as db:
        assert db.get(NotFound, nf_id).status == NotFoundStatus.DISMISSED  # type: ignore[union-attr]

    # Hit the path again -> it should reopen.
    _record_404(delivery_app_file_db, "/recurring/", HOST_A)
    with file_db_session_factory() as db:
        row = db.get(NotFound, nf_id)
        assert row.status == NotFoundStatus.OPEN  # type: ignore[union-attr]
        assert row.count == 2  # type: ignore[union-attr]
    listing = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A})
    assert b"<code>/recurring/</code>" in listing.data


def test_ignore_is_permanent_and_never_reopens(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    """Ignore permanently suppresses: a re-hit does NOT reopen or bump."""
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/noise/", HOST_A)
    with file_db_session_factory() as db:
        nf_id = db.execute(select(NotFound.id)).scalar_one()

    client = admin_app_file_db.test_client()
    _login(client)
    resp = client.post(
        f"/admin/sites/blog/not-found/{nf_id}/ignore",
        data={"_csrf_token": _csrf_token(client)},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302
    with file_db_session_factory() as db:
        assert db.get(NotFound, nf_id).status == NotFoundStatus.IGNORED  # type: ignore[union-attr]

    # Re-hit -> stays ignored, count NOT bumped.
    _record_404(delivery_app_file_db, "/noise/", HOST_A)
    with file_db_session_factory() as db:
        row = db.get(NotFound, nf_id)
        assert row.status == NotFoundStatus.IGNORED  # type: ignore[union-attr]
        assert row.count == 1  # type: ignore[union-attr]


def test_dismiss_rejects_cross_site_row(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/on-a/", HOST_A)
    with file_db_session_factory() as db:
        nf_id = db.execute(select(NotFound.id)).scalar_one()

    client = admin_app_file_db.test_client()
    _login(client)
    token = _csrf_token(client)
    # Dismiss the site-A row under site B's URL -> 404 (cross-site probe).
    resp = client.post(
        f"/admin/sites/other/not-found/{nf_id}/dismiss",
        data={"_csrf_token": token},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 404


def test_suggestion_renders_for_a_near_miss(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)  # seeds a published page "introduction" -> /introduction/
    _record_404(delivery_app_file_db, "/introducton/", HOST_A)  # typo

    client = admin_app_file_db.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/not-found/", headers={"Host": HOST_A})
    assert resp.status_code == 200
    # The fuzzy match surfaces /introduction/ as the suggested target.
    assert b"/introduction/" in resp.data


def test_list_dispatch_full_partial_boosted(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    _record_404(delivery_app_file_db, "/dead/", HOST_A)

    client = admin_app_file_db.test_client()
    _login(client)
    url = "/admin/sites/blog/not-found/"

    # Cold load: full page (admin chrome present).
    full = client.get(url, headers={"Host": HOST_A})
    assert b"notfound-list-table" in full.data
    assert b"admin-content" in full.data

    # htmx in-page swap: bare partial (no chrome).
    partial = client.get(url, headers={"Host": HOST_A, "HX-Request": "true"})
    assert b"notfound-list-table" in partial.data
    assert b"admin-content" not in partial.data

    # Boosted rail nav sends HX-Request AND HX-Boosted: must be full page.
    boosted = client.get(url, headers={"Host": HOST_A, "HX-Request": "true", "HX-Boosted": "true"})
    assert b"admin-content" in boosted.data
