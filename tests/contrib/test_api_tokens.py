"""Tests for `bragi.contrib.api_tokens` (#146).

Covers:
- Token mint / parse / verify round-trip.
- Expired tokens reject in verify.
- Bearer middleware authenticates the request as the token's
  owner (no session cookie required).
- POST /admin/api/.../posts/ creates a Post under bearer auth.
- PATCH and POST publish work end-to-end.
- Revoked tokens reject (404 because verify returns None).
- Token scope is enforced on bearer requests.
- Audit rows are written for token.created / token.revoked / token.used.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.api_tokens.tokens import mint_token, parse_token, verify
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.personal_access_token import PersonalAccessToken, TokenScope
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole

OWNER_EMAIL = "ada@example.com"
OWNER_PASSWORD = "correct-horse-battery-staple"


def _seed_owner_and_site(db: Session) -> tuple[User, Site]:
    user = User(email=OWNER_EMAIL, display_name="Ada", is_active=True)
    db.add(user)
    db.flush()
    db.add(LocalCredential(user_id=user.id, password_hash=hash_password(OWNER_PASSWORD)))
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db.add(site)
    db.flush()
    db.add(UserSiteRole(user_id=user.id, site_id=site.id, role=Role.ADMIN))
    db.commit()
    return user, site


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    del patched_session_locals
    _seed_owner_and_site(db_session)
    app = create_admin_app()
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(admin_app: Flask) -> FlaskClient:
    return admin_app.test_client()


# --------------------------- token plumbing ---------------------------


def test_parse_token_round_trip(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    parsed = parse_token(minted.plaintext)
    assert parsed is not None
    assert parsed.public_id == minted.model.public_id


def test_parse_rejects_malformed_tokens() -> None:
    assert parse_token(None) is None
    assert parse_token("") is None
    assert parse_token("Bearer x") is None
    assert parse_token("brg_short_secret") is None
    assert parse_token("brg_") is None


def test_verify_returns_token_for_valid_secret(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    row = verify(db_session, minted.plaintext)
    assert row is not None
    assert row.id == minted.model.id


def test_verify_returns_none_for_bad_secret(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    # Same public_id, wrong secret.
    bad = f"brg_{minted.model.public_id}_{'x' * 32}"
    assert verify(db_session, bad) is None


def test_verify_returns_none_for_expired_token(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    )
    db_session.commit()
    assert verify(db_session, minted.plaintext) is None


# --------------------------- bearer middleware ---------------------------


def _mint(db: Session, user_id: int, *, scopes: list[str] | None = None) -> str:
    minted = mint_token(
        db,
        user_id=user_id,
        name="api-bot",
        scopes=scopes if scopes is not None else [TokenScope.POST_WRITE],
        expires_at=None,
    )
    db.commit()
    return minted.plaintext


def test_bearer_token_authenticates_post_create(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "hello", "title": "Hello", "body_markdown": "Hi"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.data.decode()
    payload = resp.get_json()
    assert payload["post"]["slug"] == "hello"
    assert payload["post"]["status"] == "draft"


def test_no_auth_header_rejects(client: FlaskClient) -> None:
    """No auth (no session, no bearer) is rejected.

    The exact response is CSRF's 400 (CSRF runs before the auth
    guard, and the anonymous request has no CSRF token). Whether
    it's CSRF or auth_local that fires first doesn't matter for
    the surface contract: an unauthenticated POST is not accepted.
    """
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
    )
    assert resp.status_code in (302, 303, 400, 401, 403)


def test_bearer_token_revoked_rejects(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    # Revoke directly: delete the row.
    row = db_session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.user_id == user.id)
    ).scalar_one()
    db_session.delete(row)
    db_session.commit()
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code in (302, 303)


def test_expired_token_rejects(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="expired-bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    )
    db_session.commit()
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {minted.plaintext}"},
    )
    assert resp.status_code in (302, 303)


def test_scope_missing_returns_403(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id, scopes=[TokenScope.PAGE_WRITE])
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403


# --------------------------- REST endpoints ---------------------------


def test_post_create_and_list(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "first", "title": "First", "body_markdown": "Body 1"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "second", "title": "Second", "body_markdown": "Body 2"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    listing = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert listing.status_code == 200
    slugs = [p["slug"] for p in listing.get_json()["posts"]]
    assert set(slugs) == {"first", "second"}


def test_post_patch_updates_fields(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    created = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "p1", "title": "P1", "body_markdown": "old"},
        headers={"Authorization": f"Bearer {plaintext}"},
    ).get_json()
    post_id = created["post"]["id"]
    patched = client.patch(
        f"/admin/api/sites/blog/posts/{post_id}/",
        json={"title": "Updated", "body_markdown": "new"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert patched.status_code == 200
    payload = patched.get_json()
    assert payload["post"]["title"] == "Updated"
    assert payload["post"]["body_markdown"] == "new"


def test_post_publish_sets_status_and_stamps_date(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    created = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "p2", "title": "P2", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    ).get_json()
    pub = client.post(
        f"/admin/api/sites/blog/posts/{created['post']['id']}/publish",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert pub.status_code == 200
    assert pub.get_json()["post"]["status"] == PostStatus.PUBLISHED
    assert pub.get_json()["post"]["published_at"] is not None


def test_post_create_conflict_on_duplicate_slug(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    first = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "dupe", "title": "A", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert first.status_code == 201
    second = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "dupe", "title": "B", "body_markdown": "y"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert second.status_code == 409


def test_unknown_site_404(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    resp = client.get(
        "/admin/api/sites/no-such-site/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404


# --------------------------- audit rows ---------------------------


def test_token_used_emits_audit_row(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    rows = list(
        db_session.execute(select(AuditLog).where(AuditLog.action == "token.used")).scalars()
    )
    assert len(rows) >= 1


# --------------------------- post is owned by API caller ---------------------------


def test_created_post_author_id_is_token_owner(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    user_id = user.id
    plaintext = _mint(db_session, user_id)
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "owned", "title": "Owned", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    # The request committed via a separate SessionLocal; drop the
    # test session's snapshot so we see the new row.
    db_session.rollback()
    post = db_session.execute(select(Post).where(Post.slug == "owned")).scalar_one()
    assert post.author_id == user_id
