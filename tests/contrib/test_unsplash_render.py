"""Tests for the HTML transform that wraps credited images in
<figure> tags. Runs after the markdown render."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from bragi.contrib.unsplash.render import wrap_credited_images
from bragi.core.models.attachment import Attachment
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def _site_with_attachments(db_session: Session) -> tuple[Site, dict[str, Attachment]]:
    owner = User(email="x@example.test", display_name="X", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    credited = Attachment(
        site_id=site.id,
        filename="unsplash.jpg",
        content_type="image/jpeg",
        size_bytes=1000,
        storage_key="UNSPLASH_KEY",
        external_source="unsplash",
        external_source_id="abc123",
        credit_name="Jane Doe",
        credit_url="https://unsplash.com/@jane",
    )
    plain = Attachment(
        site_id=site.id,
        filename="own.jpg",
        content_type="image/jpeg",
        size_bytes=500,
        storage_key="PLAIN_KEY",
    )
    db_session.add_all([credited, plain])
    db_session.commit()
    return site, {"credited": credited, "plain": plain}


def test_wraps_credited_image_in_figure_with_caption(
    db_session: Session,
    _site_with_attachments: tuple[Site, dict[str, Attachment]],
) -> None:
    site, _ = _site_with_attachments
    html = '<p><img src="/attachments/UNSPLASH_KEY" alt="A mountain"></p>'
    out = wrap_credited_images(html, db=db_session, site_id=site.id, app_name="bragi")
    assert "<figure" in out
    assert 'class="credit"' in out
    assert "Jane Doe" in out
    assert "utm_source=bragi" in out
    assert "utm_medium=referral" in out


def test_leaves_plain_image_unwrapped(
    db_session: Session,
    _site_with_attachments: tuple[Site, dict[str, Attachment]],
) -> None:
    site, _ = _site_with_attachments
    html = '<p><img src="/attachments/PLAIN_KEY" alt="Own photo"></p>'
    out = wrap_credited_images(html, db=db_session, site_id=site.id, app_name="bragi")
    assert "<figure" not in out
    assert "credit" not in out


def test_idempotent_no_double_wrap(
    db_session: Session,
    _site_with_attachments: tuple[Site, dict[str, Attachment]],
) -> None:
    site, _ = _site_with_attachments
    html = '<p><img src="/attachments/UNSPLASH_KEY" alt="A mountain"></p>'
    once = wrap_credited_images(html, db=db_session, site_id=site.id, app_name="bragi")
    twice = wrap_credited_images(once, db=db_session, site_id=site.id, app_name="bragi")
    assert twice.count("<figure") == once.count("<figure")


def test_image_referencing_no_attachment_is_left_alone(
    db_session: Session,
    _site_with_attachments: tuple[Site, dict[str, Attachment]],
) -> None:
    site, _ = _site_with_attachments
    html = '<p><img src="https://external.example/photo.jpg" alt="External"></p>'
    out = wrap_credited_images(html, db=db_session, site_id=site.id, app_name="bragi")
    assert "<figure" not in out
