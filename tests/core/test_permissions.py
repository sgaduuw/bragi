"""Tests for the per-site role helper (#9)."""

from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.permissions import has_role


@pytest.fixture
def app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Flask:
    plain = User(email="plain@example.com", display_name="Plain", is_active=True)
    sup = User(email="sup@example.com", display_name="Sup", is_active=True, is_superuser=True)
    db_session.add_all([plain, sup])
    db_session.flush()
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=sup.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=sup.id,
    )
    db_session.add_all([blog, other])
    db_session.flush()
    db_session.add(UserSiteRole(user_id=plain.id, site_id=blog.id, role="editor"))
    db_session.add(UserSiteRole(user_id=plain.id, site_id=other.id, role="author"))
    db_session.commit()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    return app


def _site_id(db_session_factory: sessionmaker[Session], slug: str) -> int:
    from sqlalchemy import select

    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == slug)).scalar_one().id


def _user_id(db_session_factory: sessionmaker[Session], email: str) -> int:
    from sqlalchemy import select

    with db_session_factory() as db:
        return db.execute(select(User).where(User.email == email)).scalar_one().id


def test_unauthenticated_has_no_role(app: Flask) -> None:
    with app.test_request_context("/"):
        assert has_role("author", 1) is False


def test_superuser_passes_every_check(
    app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    sup_id = _user_id(db_session_factory, "sup@example.com")
    blog_id = _site_id(db_session_factory, "blog")
    with app.test_request_context("/") as ctx:
        ctx.session["user_id"] = sup_id
        assert has_role("author", blog_id) is True
        assert has_role("editor", blog_id) is True
        assert has_role("admin", blog_id) is True
        # Even sites the superuser has no explicit grant on.
        assert has_role("admin", 999999) is True


def test_role_matrix_editor_on_blog(app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    plain_id = _user_id(db_session_factory, "plain@example.com")
    blog_id = _site_id(db_session_factory, "blog")
    with app.test_request_context("/") as ctx:
        ctx.session["user_id"] = plain_id
        assert has_role("author", blog_id) is True
        assert has_role("editor", blog_id) is True
        assert has_role("admin", blog_id) is False


def test_role_matrix_author_on_other(app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    plain_id = _user_id(db_session_factory, "plain@example.com")
    other_id = _site_id(db_session_factory, "other")
    with app.test_request_context("/") as ctx:
        ctx.session["user_id"] = plain_id
        assert has_role("author", other_id) is True
        assert has_role("editor", other_id) is False
        assert has_role("admin", other_id) is False


def test_role_only_applies_to_granted_site(
    app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Editor on blog does NOT confer editor on a different site."""
    plain_id = _user_id(db_session_factory, "plain@example.com")
    with app.test_request_context("/") as ctx:
        ctx.session["user_id"] = plain_id
        assert has_role("editor", 99999) is False


def test_superuser_bypasses_role_string_validation(
    app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Superusers pass every check, including garbage role strings.
    By design: the bypass happens before the role-rank lookup."""
    sup_id = _user_id(db_session_factory, "sup@example.com")
    blog_id = _site_id(db_session_factory, "blog")
    with app.test_request_context("/") as ctx:
        ctx.session["user_id"] = sup_id
        assert has_role("god-mode", blog_id) is True


def test_unknown_role_string_fails_closed_for_plain_user(
    app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A non-superuser asking for a non-existent role gets False."""
    plain_id = _user_id(db_session_factory, "plain@example.com")
    blog_id = _site_id(db_session_factory, "blog")
    with app.test_request_context("/") as ctx:
        ctx.session["user_id"] = plain_id
        assert has_role("god-mode", blog_id) is False
