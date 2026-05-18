"""Tests for the chronological archive endpoints (#144).

Three levels: years index, months-in-a-year, posts-in-a-month.
The dispatcher peels segments from the post_index URL space, so
the routes live at `<post_index>/archive/[/<year>/[/<month>/]]`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    del patched_session_locals
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
    seed_blog_index(db_session, site, commit=False)

    def _add(slug: str, when: datetime, status: str = PostStatus.PUBLISHED) -> None:
        db_session.add(
            Post(
                site_id=site.id,
                slug=slug,
                title=slug.title(),
                body_markdown="x",
                body_html="<p>x</p>",
                body_excerpt=slug,
                author_id=user.id,
                status=status,
                published_at=when,
            )
        )

    # Two posts in 2024-01, one in 2024-05, three in 2025-03, plus a draft.
    _add("a", datetime(2024, 1, 5, tzinfo=UTC))
    _add("b", datetime(2024, 1, 20, tzinfo=UTC))
    _add("c", datetime(2024, 5, 1, tzinfo=UTC))
    _add("d", datetime(2025, 3, 2, tzinfo=UTC))
    _add("e", datetime(2025, 3, 10, tzinfo=UTC))
    _add("f", datetime(2025, 3, 28, tzinfo=UTC))
    _add("g", datetime(2025, 6, 1, tzinfo=UTC), status=PostStatus.DRAFT)
    db_session.commit()
    yield create_delivery_app()


def test_archive_years_lists_years_with_counts_descending(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/archive/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # 2025 has 3 posts (drafts excluded); 2024 has 3.
    assert "2025" in body
    assert "2024" in body
    assert body.index("2025") < body.index("2024")
    # Counts excluding drafts.
    assert "3 posts" in body


def test_archive_year_lists_months_with_counts(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/archive/2024/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "January" in body
    assert "May" in body
    # Two posts in Jan, one in May.
    assert "2 posts" in body
    assert "1 post" in body and "1 posts" not in body


def test_archive_year_with_no_posts_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/archive/1999/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


def test_archive_month_lists_posts_in_chronological_order(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/archive/2025/03/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    # Oldest first within a month.
    assert body.index("D") < body.index("E") < body.index("F")


def test_archive_month_with_no_posts_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/archive/2024/07/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


def test_archive_month_out_of_range_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/archive/2025/13/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


def test_archive_non_integer_segments_404(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    assert (
        client.get("/posts/archive/abc/", headers={"Host": "blog.example.com"}).status_code == 404
    )
    assert (
        client.get("/posts/archive/2025/abc/", headers={"Host": "blog.example.com"}).status_code
        == 404
    )


def test_archive_excludes_drafts_from_counts(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/archive/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    # 2025 should show 3 (the draft for 2025-06 doesn't count).
    assert "3 posts" in body
    # No "1 post" line for 2025-06 (the draft).
    resp2 = delivery_app.test_client().get(
        "/posts/archive/2025/", headers={"Host": "blog.example.com"}
    )
    assert "June" not in resp2.data.decode()


def test_archive_etag_round_trip_304(delivery_app: Flask) -> None:
    """Conditional GET against the archive returns 304 unchanged."""
    client = delivery_app.test_client()
    first = client.get("/posts/archive/", headers={"Host": "blog.example.com"})
    etag = first.headers["ETag"]
    second = client.get(
        "/posts/archive/",
        headers={"Host": "blog.example.com", "If-None-Match": etag},
    )
    assert second.status_code == 304
