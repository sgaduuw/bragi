"""Tests for the PROFILE page kind (public h-card + JSON-LD Person)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User

HOST = "blog.example.com"


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    author = User(
        email="ada@example.com",
        display_name="Ada Lovelace",
        is_active=True,
        bio="Mathematician and first programmer.",
        pronouns="she/her",
        location="London",
        avatar_url="https://example.com/ada.jpg",
        profile_links=[
            {"label": "GitHub", "url": "https://github.com/ada"},
            {"label": "Mastodon", "url": "https://fosstodon.org/@ada"},
        ],
    )
    db_session.add(author)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname=HOST,
        title="Blog",
        canonical_url=f"https://{HOST}",
        owner_user_id=author.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(
        Page(
            site_id=site.id,
            slug="about",
            title="About Ada",
            body_markdown="Hello there.",
            body_html="<p>Hello there.</p>",
            body_excerpt="Hello there.",
            author_id=author.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.PROFILE,
        )
    )
    db_session.commit()
    yield create_delivery_app()


def test_profile_renders_h_card(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/about/", headers={"Host": HOST})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="h-card profile"' in body
    assert 'class="p-name">Ada Lovelace</h1>' in body
    assert '<img class="u-photo' in body and "https://example.com/ada.jpg" in body
    assert "she/her" in body
    assert "London" in body
    assert "Mathematician and first programmer." in body  # p-note bio
    # A PROFILE page surfaces the account profile, not its own body: the
    # page body_markdown ("Hello there.") is no longer rendered.
    assert "Hello there." not in body
    # rel=me links
    assert 'rel="me" href="https://github.com/ada"' in body
    assert 'rel="me" href="https://fosstodon.org/@ada"' in body


def test_profile_bio_renders_markdown(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The bio is authored as markdown; the p-note renders it as HTML, while
    the JSON-LD description stays plain text (no markup)."""
    with db_session_factory() as db:
        author = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        author.bio = "I am **bold** and [linked](https://example.com/me)."
        db.commit()
    body = delivery_app.test_client().get("/about/", headers={"Host": HOST}).data.decode()
    # p-note: rendered HTML.
    assert "<strong>bold</strong>" in body
    assert 'href="https://example.com/me"' in body
    # JSON-LD description: plain text, markdown stripped.
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL)
    assert m is not None
    desc = json.loads(m.group(1))["description"]
    assert "**" not in desc and "<strong>" not in desc
    assert "bold" in desc and "linked" in desc


def test_profile_emits_jsonld_person(delivery_app: Flask) -> None:
    body = delivery_app.test_client().get("/about/", headers={"Host": HOST}).data.decode()
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL)
    assert m is not None
    doc = json.loads(m.group(1))
    assert doc["@type"] == "Person"
    assert doc["name"] == "Ada Lovelace"
    assert doc["description"] == "Mathematician and first programmer."
    assert doc["image"] == "https://example.com/ada.jpg"
    assert doc["url"] == "https://blog.example.com/about/"
    assert set(doc["sameAs"]) == {"https://github.com/ada", "https://fosstodon.org/@ada"}


def test_profile_as_home_is_author_cache_aware(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """When a PROFILE page is the site home, an edit to the author's profile
    busts the `/` cache (the validator spans page + author, not page alone)."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        site.home_page_id = page.id
        db.commit()

    client = delivery_app.test_client()
    r1 = client.get("/", headers={"Host": HOST})
    assert r1.status_code == 200
    assert 'class="h-card' in r1.data.decode()
    etag = r1.headers["ETag"]

    # Same ETag -> 304.
    r2 = client.get("/", headers={"Host": HOST, "If-None-Match": etag})
    assert r2.status_code == 304

    # Edit the author's profile -> new validator -> 200 (not a stale 304).
    time.sleep(0.01)
    with db_session_factory() as db:
        author = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        author.bio = "Updated bio."
        db.commit()
    r3 = client.get("/", headers={"Host": HOST, "If-None-Match": etag})
    assert r3.status_code == 200
    assert "Updated bio." in r3.data.decode()


def test_profile_view_none_for_missing_user() -> None:
    """The view model + JSON-LD handle a missing author (the template's
    defensive fallback branch): `profile_view(None)` is None."""
    from bragi.core.profiles import profile_view

    assert profile_view(None) is None
