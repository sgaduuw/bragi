"""Tests for the Page delivery dispatch path.

Exercises page-kind-specific dispatch in `_render_page`, covering
the resume kind dispatching to its own template rather than the
generic page template.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import make_test_user


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
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

    yield create_delivery_app()


def test_resume_kind_dispatches_to_resume_template(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A page with kind=resume renders the resume template, not the
    generic page template. Smoke test: the rendered body contains
    schema.org Person markers (itemtype + JSON-LD) that the generic
    page template never emits."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        author = make_test_user(db, email="resume-author@example.test")
        page = Page(
            site_id=site.id,
            slug="my-cv",
            title="Test CV",
            body_markdown="A summary.",
            body_html="<p>A summary.</p>",
            body_excerpt="",
            author_id=author.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.RESUME,
            resume_data={"highlights": ["one"]},
        )
        db.add(page)
        db.commit()

    resp = delivery_app.test_client().get("/my-cv/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'itemtype="https://schema.org/Person"' in body
    assert '"@type": "Person"' in body  # JSON-LD block
