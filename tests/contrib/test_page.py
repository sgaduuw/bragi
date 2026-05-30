"""Tests for the Page content type (#14)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


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
    db_session.flush()

    # Top-level page
    about = Page(
        site_id=site.id,
        slug="about",
        title="About",
        body_markdown="About us.",
        body_html="<p>About us.</p>",
        body_excerpt="About us.",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
    )
    db_session.add(about)
    db_session.flush()
    # Nested page
    db_session.add(
        Page(
            site_id=site.id,
            parent_id=about.id,
            slug="team",
            title="The Team",
            body_markdown="Team.",
            body_html="<p>Team.</p>",
            body_excerpt="Team.",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
        )
    )
    # Draft (should 404 publicly)
    db_session.add(
        Page(
            site_id=site.id,
            slug="secret",
            title="Secret",
            body_markdown="Draft.",
            body_html="<p>Draft.</p>",
            body_excerpt="Draft.",
            author_id=user.id,
            status=PageStatus.DRAFT,
        )
    )
    db_session.commit()

    yield create_delivery_app()


def test_top_level_page_resolves(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/about/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"About" in resp.data
    assert b"About us." in resp.data


def test_nested_page_resolves(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/about/team/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"The Team" in resp.data


def test_draft_page_returns_404(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/secret/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_nested_path_without_parent_returns_404(delivery_app: Flask) -> None:
    """Child slug under wrong parent: no such page."""
    resp = delivery_app.test_client().get("/secret/team/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_unknown_host_returns_404(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/about/", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


def test_same_slug_under_different_parents_allowed(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Two pages with slug 'overview' under different parents must coexist."""
    with db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
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

        parent_a = Page(
            site_id=site.id,
            slug="docs",
            title="Docs",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
        )
        parent_b = Page(
            site_id=site.id,
            slug="guides",
            title="Guides",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
        )
        db.add_all([parent_a, parent_b])
        db.flush()
        # Both children share slug 'overview' but sit under different parents.
        db.add(
            Page(
                site_id=site.id,
                parent_id=parent_a.id,
                slug="overview",
                title="Docs overview",
                body_markdown="",
                body_html="",
                body_excerpt="",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            )
        )
        db.add(
            Page(
                site_id=site.id,
                parent_id=parent_b.id,
                slug="overview",
                title="Guides overview",
                body_markdown="",
                body_html="",
                body_excerpt="",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            )
        )
        db.commit()
        # Both committed without violating the UNIQUE constraint.


def test_same_parent_same_slug_rejected(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Two pages with the same slug under the same parent must fail."""
    with db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
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
        parent = Page(
            site_id=site.id,
            slug="docs",
            title="Docs",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
        )
        db.add(parent)
        db.flush()
        db.add(
            Page(
                site_id=site.id,
                parent_id=parent.id,
                slug="overview",
                title="A",
                body_markdown="",
                body_html="",
                body_excerpt="",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            )
        )
        db.commit()

        db.add(
            Page(
                site_id=site.id,
                parent_id=parent.id,
                slug="overview",
                title="B",
                body_markdown="",
                body_html="",
                body_excerpt="",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_page_plugin_registers_content_type(delivery_app: Flask) -> None:
    registry = delivery_app.extensions["registry"]
    spec = registry.content_type("page")
    assert spec is not None
    assert spec.label == "Page"
    assert spec.sitemap_eligible is True
    assert spec.feed_eligible is False


def test_resume_admin_extras_global_returns_list(patched_session_locals, db_session) -> None:
    """`resume_admin_extras()` returns a list (empty when no
    plugin registers a template). Smoke-checks the aggregator
    wiring before any resume-source plugin is loaded."""
    from bragi.apps.admin import create_admin_app

    app = create_admin_app()
    assert "resume_admin_extras" in app.jinja_env.globals
    extras_fn = app.jinja_env.globals["resume_admin_extras"]
    with app.test_request_context("/"):
        result = extras_fn()
    assert isinstance(result, list)


# ----------------------- resume_data validation contract ----


def test_validate_resume_data_accepts_empty_description_markdown() -> None:
    """The resume form's JS serialiser sends `description_markdown:
    ""` for blank narrative textareas (Position / Project /
    Education). The Pydantic schema declares
    `description_markdown: str = ""`, so an empty string MUST be
    accepted. Regression: a `null` here used to break save after
    importing a current LinkedIn position with no Description
    set, because the JS serialiser previously converted "" to
    null for every text field including textareas. See the inline
    comment in `_resume_fieldset.html`'s rowToObject for the
    fix."""
    import json

    from bragi.contrib.page.admin import _validate_resume_data

    payload = {
        "header": {"tagline": None, "location": None, "profile_links": []},
        "highlights": [],
        "experience": [
            {
                "id": "abc123def456",
                "company": "Acme",
                "role": "Engineer",
                "location": None,
                "start_date": "2020-01",
                "end_date": None,
                "description_markdown": "",
                "impacts": [],
            }
        ],
        "projects": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
    }
    result, err = _validate_resume_data(json.dumps(payload))
    assert err is None, err
    assert result is not None


def test_validate_resume_data_rejects_null_description_markdown() -> None:
    """A `null` `description_markdown` is the bug shape that
    pydantic must reject; the resume form's JS serialiser is the
    only producer of this payload and was fixed to send "". This
    test guards the contract from the server side so a future
    regression in the JS gets caught: if the JS starts sending
    null again, manual import flows will fail in exactly this
    way."""
    import json

    from bragi.contrib.page.admin import _validate_resume_data

    payload = {
        "header": {"tagline": None, "location": None, "profile_links": []},
        "highlights": [],
        "experience": [
            {
                "id": "abc123def456",
                "company": "Acme",
                "role": "Engineer",
                "location": None,
                "start_date": "2020-01",
                "end_date": None,
                "description_markdown": None,
                "impacts": [],
            }
        ],
        "projects": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
    }
    result, err = _validate_resume_data(json.dumps(payload))
    assert result is None
    assert err is not None
    assert "experience.0.description_markdown" in err
    assert "valid string" in err
