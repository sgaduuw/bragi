"""Tests for the external_source / credit columns on Attachment.

The five columns hold provenance + attribution data for images
sourced from external services like Unsplash. The Attachment row
on its own carries no behaviour; the columns are wired up to the
SQLAlchemy mapping and round-trip through a real DB session.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models.attachment import Attachment
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def _seed_site(db_session: Session) -> int:
    owner = User(email="cv@example.test", display_name="CV", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="CV Site",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()
    return site.id


def test_attachment_external_source_defaults_to_none(db_session: Session, _seed_site: int) -> None:
    att = Attachment(
        site_id=_seed_site,
        filename="local.jpg",
        content_type="image/jpeg",
        size_bytes=1024,
        storage_key="abc123",
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)
    assert att.external_source is None
    assert att.external_source_id is None
    assert att.external_source_url is None
    assert att.credit_name is None
    assert att.credit_url is None


def test_attachment_external_source_round_trips(db_session: Session, _seed_site: int) -> None:
    att = Attachment(
        site_id=_seed_site,
        filename="unsplash.jpg",
        content_type="image/jpeg",
        size_bytes=2048,
        storage_key="def456",
        external_source="unsplash",
        external_source_id="abc123XYZ",
        external_source_url="https://unsplash.com/photos/abc123XYZ",
        credit_name="Jane Doe",
        credit_url="https://unsplash.com/@jane",
    )
    db_session.add(att)
    db_session.commit()
    fetched = db_session.execute(
        select(Attachment).where(Attachment.storage_key == "def456")
    ).scalar_one()
    assert fetched.external_source == "unsplash"
    assert fetched.external_source_id == "abc123XYZ"
    assert fetched.external_source_url == "https://unsplash.com/photos/abc123XYZ"
    assert fetched.credit_name == "Jane Doe"
    assert fetched.credit_url == "https://unsplash.com/@jane"
