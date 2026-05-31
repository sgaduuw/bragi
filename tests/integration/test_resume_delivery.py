"""Full-stack resume delivery tests.

Builds a multi-section resume page through the ORM, requests its
public URL, and asserts the rendered HTML contains the expected
semantic markup, JSON-LD payload, linked-project annotation, and
section-omission behaviour.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> Iterator[Flask]:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    owner = User(email="cv@example.test", display_name="CV", is_active=True)
    db_session.add(owner)
    db_session.flush()
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="CV Site",
            canonical_url="https://blog.example.com",
            owner_user_id=owner.id,
        )
    )
    db_session.commit()
    yield create_delivery_app()


def _seed_resume_page(db: Session, *, slug: str, resume_data: dict) -> int:
    site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    author = db.execute(select(User).where(User.email == "cv@example.test")).scalar_one()
    page = Page(
        site_id=site.id,
        slug=slug,
        title="Eelco Wesemann",
        body_markdown="Pragmatic engineer.",
        body_html="<p>Pragmatic engineer.</p>",
        body_excerpt="",
        author_id=author.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.RESUME,
        resume_data=resume_data,
    )
    db.add(page)
    db.commit()
    return page.id


def test_full_resume_renders_all_sections_with_microdata(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    resume_data = {
        "header": {
            "tagline": "Platform Engineer",
            "location": "Den Haag, NL",
            "profile_links": [
                {"id": "l1", "label": "LinkedIn", "url": "https://linkedin.test/x"},
            ],
        },
        "highlights": ["Cut deploy time 70%"],
        "experience": [
            {
                "id": "pos1",
                "company": "Logius",
                "role": "Platform Engineer",
                "location": "Den Haag",
                "start_date": "2024-04",
                "end_date": None,
                "description_markdown": "- built CI/CD",
                "impacts": ["Cut build time 60%"],
            }
        ],
        "projects": [
            {
                "id": "prj1",
                "name": "GitOps deploy",
                "role": "Tech lead",
                "url": "https://example.test/repo",
                "linked_position_id": "pos1",
                "location": None,
                "start_date": None,
                "end_date": None,
                "description_markdown": "Multi-tenant pipeline.",
                "impacts": [],
            }
        ],
        "education": [
            {
                "id": "edu1",
                "institution": "Hogeschool Utrecht",
                "degree": "HBO Informatica",
                "location": None,
                "start_date": None,
                "end_date": None,
                "description_markdown": "",
            }
        ],
        "skills": [{"id": "skl1", "group_label": "Stack", "items": ["Python", "Go"]}],
        "certifications": [
            {"id": "crt1", "name": "CKA", "issuer": "CNCF", "year": 2024, "url": None}
        ],
        "languages": [
            {"id": "lng1", "name": "Dutch", "level": "Native"},
            {"id": "lng2", "name": "English", "level": "C2"},
        ],
    }

    with db_session_factory() as db:
        _seed_resume_page(db, slug="full-cv", resume_data=resume_data)

    resp = delivery_app.test_client().get("/full-cv/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()

    # Top-level Person microdata
    assert 'itemscope itemtype="https://schema.org/Person"' in body
    assert 'itemprop="name"' in body
    assert 'itemprop="jobTitle"' in body

    # Sections render
    assert "Cut deploy time 70%" in body  # Highlights
    assert "Logius" in body  # Experience company
    assert "Platform Engineer" in body  # Experience role
    assert "GitOps deploy" in body  # Projects name
    assert "Hogeschool Utrecht" in body  # Education
    assert "Python" in body and "Go" in body  # Skills
    assert "CKA" in body  # Certifications
    assert "Dutch" in body and "English" in body  # Languages

    # JSON-LD block present + parseable; key fields match
    jsonld_block = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL)
    assert jsonld_block, "JSON-LD <script> block missing"
    parsed = json.loads(jsonld_block.group(1))
    assert parsed["@type"] == "Person"
    assert parsed["name"] == "Eelco Wesemann"
    assert parsed["jobTitle"] == "Platform Engineer"
    assert parsed["worksFor"][0]["name"] == "Logius"
    assert parsed["alumniOf"][0]["name"] == "Hogeschool Utrecht"

    # Project linked to position: annotation + anchor link
    assert 'href="#position-pos1"' in body
    assert "at Logius" in body


def test_resume_with_empty_resume_data_hides_all_optional_sections(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """An empty resume still renders a valid page with just header
    + summary; no broken empty-section markup."""
    with db_session_factory() as db:
        _seed_resume_page(db, slug="empty-cv", resume_data={})

    resp = delivery_app.test_client().get("/empty-cv/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Page still rendered
    assert "Eelco Wesemann" in body
    # No "Experience" / "Projects" / etc. section headers when empty
    assert "<h2>Experience</h2>" not in body
    assert "<h2>Projects</h2>" not in body
    assert "<h2>Skills</h2>" not in body


def test_resume_with_dangling_linked_position_id_skips_annotation(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Project.linked_position_id pointing at a non-existent Position
    falls through to no annotation (graceful -- the validation layer
    doesn't FK-check)."""
    resume_data = {
        "experience": [],
        "projects": [
            {
                "id": "prj1",
                "name": "Solo project",
                "role": None,
                "url": None,
                "linked_position_id": "does-not-exist",
                "location": None,
                "start_date": None,
                "end_date": None,
                "description_markdown": "",
                "impacts": [],
            }
        ],
    }
    with db_session_factory() as db:
        _seed_resume_page(db, slug="dangling-cv", resume_data=resume_data)

    resp = delivery_app.test_client().get("/dangling-cv/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Solo project" in body
    # No "at X" annotation; no broken anchor
    assert "at does-not-exist" not in body
    assert 'href="#position-does-not-exist"' not in body


def test_resume_emits_social_meta_and_canonical(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A published resume page emits og:title, twitter:card, and canonical.

    Parity with page.html: resume pages are not SEO-second-class.
    The Site fixture has canonical_url set, so the canonical link
    should surface for any page under that site.
    """
    with db_session_factory() as db:
        _seed_resume_page(db, slug="seo-cv", resume_data={"highlights": ["one"]})

    resp = delivery_app.test_client().get("/seo-cv/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # OG title surfaces page.title (no meta_title set on the seed)
    assert 'property="og:title"' in body
    # Twitter card type
    assert 'name="twitter:card"' in body
    # Canonical link (the site has canonical_url="https://blog.example.com")
    assert 'rel="canonical"' in body
    # Title format includes site title
    assert "CV Site" in body


def _position_heading_html(html: str) -> str:
    """Extract the first <h3>...</h3> inside the experience section,
    so we can assert which span comes first."""
    match = re.search(
        r'<article class="position"[^>]*>\s*<h3>(.*?)</h3>',
        html,
        re.DOTALL,
    )
    assert match, "no position heading found in rendered resume"
    return match.group(1)


_MINIMAL_RESUME = {
    "header": {"tagline": "Eng", "location": "NL", "profile_links": []},
    "highlights": [],
    "experience": [
        {
            "id": "p1",
            "company": "Acme Corp",
            "role": "Senior Engineer",
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


def test_position_heading_default_leads_with_company(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Default (lead_with_role omitted or False): company first."""
    with db_session_factory() as db:
        _seed_resume_page(db, slug="cv-default", resume_data=dict(_MINIMAL_RESUME))
    resp = delivery_app.test_client().get("/cv-default", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    heading = _position_heading_html(resp.get_data(as_text=True))
    company_pos = heading.find('itemprop="name"')
    role_pos = heading.find('class="role"')
    assert company_pos != -1 and role_pos != -1
    assert (
        company_pos < role_pos
    ), f"expected company before role with lead_with_role=False; got {heading!r}"


def test_position_heading_lead_with_role_swaps_order(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """lead_with_role=True: role appears before company in the h3."""
    rd = dict(_MINIMAL_RESUME)
    rd["lead_with_role"] = True
    with db_session_factory() as db:
        _seed_resume_page(db, slug="cv-role-first", resume_data=rd)
    resp = delivery_app.test_client().get("/cv-role-first", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    heading = _position_heading_html(resp.get_data(as_text=True))
    company_pos = heading.find('itemprop="name"')
    role_pos = heading.find('class="role"')
    assert company_pos != -1 and role_pos != -1
    assert (
        role_pos < company_pos
    ), f"expected role before company with lead_with_role=True; got {heading!r}"


def _jsonld_block(html: str) -> str:
    """Extract the JSON-LD <script> body from the rendered page."""
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "no JSON-LD <script> block found"
    return match.group(1).strip()


def test_lead_with_role_does_not_affect_jsonld(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """JSON-LD output is byte-identical regardless of lead_with_role.
    Pins the design invariant 'visible-order only; semantic mapping
    is fixed'."""
    rd_false = dict(_MINIMAL_RESUME)
    rd_true = dict(_MINIMAL_RESUME) | {"lead_with_role": True}
    with db_session_factory() as db:
        _seed_resume_page(db, slug="cv-jsonld-false", resume_data=rd_false)
        _seed_resume_page(db, slug="cv-jsonld-true", resume_data=rd_true)
    client = delivery_app.test_client()
    jsonld_false = _jsonld_block(
        client.get("/cv-jsonld-false", headers={"Host": "blog.example.com"}).get_data(as_text=True)
    )
    jsonld_true = _jsonld_block(
        client.get("/cv-jsonld-true", headers={"Host": "blog.example.com"}).get_data(as_text=True)
    )
    # Parse to dicts so canonical-form comparison ignores incidental
    # key-order differences from json.dumps.
    assert json.loads(jsonld_false) == json.loads(jsonld_true)
