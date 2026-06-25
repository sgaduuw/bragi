"""Unit tests for IndexNowPing model (issue #443).

Verifies:
- Round-trip construction and field defaults.
- UNIQUE(site_id, url) rejects a second pending ping for the same URL on
  the same site (the coalesce-key invariant).
- Same URL under a different site is allowed (cross-site isolation).
- Cascade delete: removing the parent Site deletes its pending pings.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bragi.core.models import IndexNowPing
from bragi.core.time import naive_utcnow
from tests.conftest import make_test_site  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ping(
    db: Session,
    site_id: int,
    url: str = "https://example.com/post/hello/",
    *,
    debounce_seconds: int = 300,
) -> IndexNowPing:
    """Insert an IndexNowPing and flush (within the caller's transaction)."""
    ping = IndexNowPing(
        site_id=site_id,
        url=url,
        not_before=naive_utcnow() + timedelta(seconds=debounce_seconds),
    )
    db.add(ping)
    db.flush()
    return ping


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIndexNowPingModel:
    def test_roundtrip_defaults(self, db_session: Session) -> None:
        """IndexNowPing persists with correct field defaults after a flush."""
        site = make_test_site(
            db_session,
            slug="s1",
            hostname="s1.example.com",
            title="S1",
            canonical_url="https://s1.example.com",
        )
        _make_ping(db_session, site.id)
        db_session.commit()

        row = db_session.execute(
            select(IndexNowPing).where(IndexNowPing.site_id == site.id)
        ).scalar_one()

        assert row.id is not None
        assert row.site_id == site.id
        assert row.url == "https://example.com/post/hello/"
        assert row.not_before is not None
        # not_before should be in the future (within a few seconds of creation)
        assert row.not_before > naive_utcnow() - timedelta(seconds=5)
        assert row.attempt_count == 0
        assert row.last_error is None
        assert row.created_at is not None
        assert row.updated_at is not None

    def test_unique_constraint_rejects_duplicate_url_same_site(self, db_session: Session) -> None:
        """UNIQUE(site_id, url) rejects a second ping for the same URL on the same site."""
        site = make_test_site(
            db_session,
            slug="s2",
            hostname="s2.example.com",
            title="S2",
            canonical_url="https://s2.example.com",
        )
        _make_ping(db_session, site.id, "https://s2.example.com/post/hello/")
        db_session.commit()

        # A second ping for the same (site_id, url) must raise IntegrityError.
        db_session.add(
            IndexNowPing(
                site_id=site.id,
                url="https://s2.example.com/post/hello/",
                not_before=naive_utcnow() + timedelta(seconds=300),
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_unique_constraint_allows_same_url_different_site(self, db_session: Session) -> None:
        """The same URL may appear as a pending ping on two different sites."""
        site_a = make_test_site(
            db_session,
            slug="a",
            hostname="a.example.com",
            title="A",
            canonical_url="https://a.example.com",
        )
        site_b = make_test_site(
            db_session,
            slug="b",
            hostname="b.example.com",
            title="B",
            canonical_url="https://b.example.com",
        )
        # Shared URL (unusual in practice but valid at the DB level).
        url = "https://shared.example.com/page/"
        _make_ping(db_session, site_a.id, url)
        _make_ping(db_session, site_b.id, url)
        db_session.commit()

        rows = db_session.execute(select(IndexNowPing)).scalars().all()
        assert len(rows) == 2

    def test_cascade_delete_with_site(self, db_session: Session) -> None:
        """Deleting the parent Site cascades to its pending IndexNow pings."""
        site = make_test_site(
            db_session,
            slug="s3",
            hostname="s3.example.com",
            title="S3",
            canonical_url="https://s3.example.com",
        )
        ping = _make_ping(db_session, site.id)
        db_session.commit()

        ping_id = ping.id
        db_session.delete(site)
        db_session.commit()

        assert db_session.get(IndexNowPing, ping_id) is None
