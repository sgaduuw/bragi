"""Tests for the team admin Blueprint (P4 / #80).

Covers the acceptance criteria from #80:

- Owner sees the team page; non-owner members get 403 even if
  they have admin role.
- Granting a role inserts a UserSiteRole row and an audit row.
- Granting to a non-existent email shows a friendly error.
- Revoking a collaborator removes the row; the owner cannot be
  revoked (the form does not render and a forged POST returns
  403).
- The Team nav entry appears in-site only for owners
  (or superusers).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    # Cast of characters:
    #   - ada  : site owner of `blog`
    #   - bob  : admin role on `blog` (collaborator, NOT owner)
    #   - eve  : author role on `blog` (collaborator)
    #   - mal  : no role anywhere
    #   - sam  : superuser
    # Plus a `candidate` user used as a grant target so the
    # "user exists" path is covered.
    ada = User(email="ada@example.com", display_name="Ada", is_active=True)
    bob = User(email="bob@example.com", display_name="Bob", is_active=True)
    eve = User(email="eve@example.com", display_name="Eve", is_active=True)
    mal = User(email="mal@example.com", display_name="Mal", is_active=True)
    sam = User(email="sam@example.com", display_name="Sam", is_active=True, is_superuser=True)
    candidate = User(email="candidate@example.com", display_name="Candidate", is_active=True)
    db_session.add_all([ada, bob, eve, mal, sam, candidate])
    db_session.flush()
    for user in (ada, bob, eve, mal, sam, candidate):
        db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=ada.id,
    )
    db_session.add(site)
    db_session.flush()

    db_session.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="admin"))
    db_session.add(UserSiteRole(user_id=eve.id, site_id=site.id, role="author"))
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient, email: str) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


# ============================================================
# View visibility
# ============================================================


def test_owner_can_view_team(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    resp = client.get("/admin/sites/blog/team/")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Owner row, plus the two collaborators.
    assert "ada@example.com" in body
    assert "bob@example.com" in body
    assert "eve@example.com" in body
    # Owner appears with the "Owner" badge, not a regular role
    # cell.
    assert "Owner" in body


def test_admin_collaborator_gets_403(admin_app: Flask) -> None:
    """Bob has the admin role on `blog`. The P1 (#77) semantic
    deliberately reserves team management to the owner; admin
    collaborators do not get to invite / revoke."""
    client = admin_app.test_client()
    _login(client, "bob@example.com")
    resp = client.get("/admin/sites/blog/team/")
    assert resp.status_code == 403


def test_author_collaborator_gets_403(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, "eve@example.com")
    resp = client.get("/admin/sites/blog/team/")
    assert resp.status_code == 403


def test_non_member_gets_403(admin_app: Flask) -> None:
    """Mal has no role on `blog`. resolve_site_or_abort short-
    circuits with 403 before the owner check runs."""
    client = admin_app.test_client()
    _login(client, "mal@example.com")
    resp = client.get("/admin/sites/blog/team/")
    assert resp.status_code == 403


def test_superuser_can_view_team(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, "sam@example.com")
    resp = client.get("/admin/sites/blog/team/")
    assert resp.status_code == 200


def test_unknown_site_returns_404(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    resp = client.get("/admin/sites/nope/team/")
    assert resp.status_code == 404


# ============================================================
# Nav visibility
# ============================================================


def test_team_nav_entry_visible_for_owner(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    resp = client.get("/admin/sites/blog/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Team" in body


def test_team_nav_entry_hidden_for_collaborator(admin_app: Flask) -> None:
    """Bob has admin role on blog but isn't the owner. He hits
    /admin/sites/blog/posts/ (which renders the chrome) and
    must NOT see the Team link."""
    client = admin_app.test_client()
    _login(client, "bob@example.com")
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.data.decode()
    # The Team link's href is the giveaway; checking for "Team"
    # would false-match the placeholder we removed. Look for the
    # endpoint URL instead.
    assert "/admin/sites/blog/team/" not in body


def test_team_nav_entry_visible_for_superuser(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, "sam@example.com")
    resp = client.get("/admin/sites/blog/")
    body = resp.data.decode()
    assert "/admin/sites/blog/team/" in body


# ============================================================
# Grant flow
# ============================================================


def test_grant_inserts_role_and_audit_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    token = csrf_token(client, path="/admin/sites/blog/team/")
    resp = client.post(
        "/admin/sites/blog/team/grant",
        data={
            "email": "candidate@example.com",
            "role": "author",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        candidate = db.execute(
            select(User).where(User.email == "candidate@example.com")
        ).scalar_one()
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        row = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == candidate.id,
                UserSiteRole.site_id == site.id,
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.role == "author"
        audit_row = db.execute(
            select(AuditLog).where(AuditLog.action == "team.granted")
        ).scalar_one_or_none()
        assert audit_row is not None
        assert audit_row.site_id == site.id


def test_grant_to_nonexistent_email_shows_error(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    token = csrf_token(client, path="/admin/sites/blog/team/")
    resp = client.post(
        "/admin/sites/blog/team/grant",
        data={"email": "ghost@example.com", "role": "author", "_csrf_token": token},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert "No user with email" in body
    # No row was inserted.
    with db_session_factory() as db:
        rows = db.execute(
            select(UserSiteRole).join(User).where(User.email == "ghost@example.com")
        ).all()
        assert rows == []


def test_grant_existing_role_updates_in_place(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Re-granting an existing collaborator updates their role
    rather than failing on the (user_id, site_id) UNIQUE."""
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    token = csrf_token(client, path="/admin/sites/blog/team/")
    # Eve currently has 'author'; promote her to 'editor'.
    resp = client.post(
        "/admin/sites/blog/team/grant",
        data={"email": "eve@example.com", "role": "editor", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        eve = db.execute(select(User).where(User.email == "eve@example.com")).scalar_one()
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        row = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == eve.id,
                UserSiteRole.site_id == site.id,
            )
        ).scalar_one()
        assert row.role == "editor"
        audit_row = db.execute(
            select(AuditLog).where(AuditLog.action == "team.role_changed")
        ).scalar_one_or_none()
        assert audit_row is not None


def test_grant_blocks_owner_email(admin_app: Flask) -> None:
    """Granting an explicit role to the site owner is a no-op
    (the owner is implicit admin via P1) so the view rejects
    with a friendly message instead of inserting a redundant
    row."""
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    token = csrf_token(client, path="/admin/sites/blog/team/")
    resp = client.post(
        "/admin/sites/blog/team/grant",
        data={"email": "ada@example.com", "role": "admin", "_csrf_token": token},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert "site owner" in body or "implicit admin" in body


def test_collaborator_cannot_grant(admin_app: Flask) -> None:
    """A POST to /grant from a non-owner returns 403."""
    client = admin_app.test_client()
    _login(client, "bob@example.com")  # admin role, not owner
    token = csrf_token(client, path="/admin/sites/blog/")
    resp = client.post(
        "/admin/sites/blog/team/grant",
        data={"email": "candidate@example.com", "role": "author", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ============================================================
# Revoke flow
# ============================================================


def test_revoke_removes_role_and_writes_audit(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    token = csrf_token(client, path="/admin/sites/blog/team/")
    with db_session_factory() as db:
        eve = db.execute(select(User).where(User.email == "eve@example.com")).scalar_one()
        eve_id = eve.id
    resp = client.post(
        f"/admin/sites/blog/team/{eve_id}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        rows = db.execute(select(UserSiteRole).where(UserSiteRole.user_id == eve_id)).all()
        assert rows == []
        audit_row = db.execute(
            select(AuditLog).where(AuditLog.action == "team.revoked")
        ).scalar_one_or_none()
        assert audit_row is not None


def test_revoke_owner_returns_403(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A forged POST to revoke the owner must return 403 and
    leave the site's `owner_user_id` untouched."""
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        owner_id = site.owner_user_id
    token = csrf_token(client, path="/admin/sites/blog/team/")
    resp = client.post(
        f"/admin/sites/blog/team/{owner_id}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.owner_user_id == owner_id


def test_revoke_missing_role_is_idempotent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Revoking a role that doesn't exist is a soft no-op (the
    operator's intent is already satisfied). The page redirects
    back to the team list with a friendly note instead of 404."""
    client = admin_app.test_client()
    _login(client, "ada@example.com")
    with db_session_factory() as db:
        mal = db.execute(select(User).where(User.email == "mal@example.com")).scalar_one()
        mal_id = mal.id
    token = csrf_token(client, path="/admin/sites/blog/team/")
    resp = client.post(
        f"/admin/sites/blog/team/{mal_id}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_collaborator_cannot_revoke(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Bob (admin role, not owner) cannot revoke Eve."""
    client = admin_app.test_client()
    _login(client, "bob@example.com")
    with db_session_factory() as db:
        eve = db.execute(select(User).where(User.email == "eve@example.com")).scalar_one()
        eve_id = eve.id
    token = csrf_token(client, path="/admin/sites/blog/")
    resp = client.post(
        f"/admin/sites/blog/team/{eve_id}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 403
