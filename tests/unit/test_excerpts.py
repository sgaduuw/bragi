"""Tests for make_excerpt (the new render-then-strip behaviour) +
rebuild_excerpts (the bulk-rebuild helper)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from bragi.core.models.post import Post, PostStatus
from bragi.core.render.excerpts import rebuild_excerpts
from bragi.core.render.markdown import make_excerpt

# ============================================================
# make_excerpt: the rendering bug fix
# ============================================================


def test_make_excerpt_drops_backslash_escapes() -> None:
    """The Ghost importer's markdownify over-escapes (`1\\.9`,
    `HTTP\\-103`). The excerpt should consume those escapes via
    the markdown renderer."""
    raw = r"HAProxy 1\.9 introduces HTTP\-103 Early Hints."
    out = make_excerpt(raw)
    assert "1.9" in out
    assert r"1\.9" not in out
    assert "HTTP-103" in out
    assert r"HTTP\-103" not in out


def test_make_excerpt_resolves_link_syntax_to_label_only() -> None:
    """Markdown link syntax `[label](url)` becomes just the label
    text; the URL is dropped (excerpt is plain text)."""
    raw = "See [rfc8297](https://tools.ietf.org/html/rfc8297) for details."
    out = make_excerpt(raw)
    assert "rfc8297" in out
    assert "tools.ietf.org" not in out
    assert "[rfc8297]" not in out
    assert "](" not in out


def test_make_excerpt_handles_inline_code_and_bold() -> None:
    raw = "Use `make_excerpt` to get a **plain-text** preview."
    out = make_excerpt(raw)
    assert "make_excerpt" in out
    assert "plain-text" in out
    assert "**" not in out
    assert "`" not in out


def test_make_excerpt_collapses_block_structure_to_plain_text() -> None:
    raw = "# Heading\n\nFirst paragraph.\n\nSecond paragraph."
    out = make_excerpt(raw)
    # Heading text survives, joined to the body with whitespace.
    assert "Heading" in out
    assert "First paragraph" in out
    # No leftover # marker.
    assert not out.startswith("#")


def test_make_excerpt_respects_max_chars_truncation() -> None:
    raw = "word " * 200  # ~1000 chars
    out = make_excerpt(raw, max_chars=50)
    assert len(out) <= 53  # 50 chars + "..."
    assert out.endswith("...")


def test_make_excerpt_empty_input_returns_empty_string() -> None:
    assert make_excerpt("") == ""
    assert make_excerpt("   ") == ""


# ============================================================
# rebuild_excerpts: the bulk-rebuild helper
# ============================================================


def test_rebuild_excerpts_recomputes_post_excerpts(
    db_session: Session,
) -> None:
    """A post with a stale (broken) excerpt gets its excerpt
    recomputed on rebuild."""
    from bragi.core.models.site import Site
    from bragi.core.models.user import User

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
    post = Post(
        site_id=site.id,
        author_id=user.id,
        title="x",
        slug="x",
        body_markdown=r"HAProxy 1\.9 was released.",
        body_html="<p>HAProxy 1.9 was released.</p>",
        body_excerpt=r"HAProxy 1\.9 was released.",  # stale broken value
        status=PostStatus.PUBLISHED,
    )
    db_session.add(post)
    db_session.commit()

    counts = rebuild_excerpts(db_session)
    db_session.commit()

    assert counts["posts_scanned"] == 1
    assert counts["posts_changed"] == 1
    db_session.refresh(post)
    assert post.body_excerpt == "HAProxy 1.9 was released."


def test_rebuild_excerpts_dry_run_does_not_persist(
    db_session: Session,
) -> None:
    from bragi.core.models.site import Site
    from bragi.core.models.user import User

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
    post = Post(
        site_id=site.id,
        author_id=user.id,
        title="x",
        slug="x",
        body_markdown="Hello world.",
        body_html="<p>Hello world.</p>",
        body_excerpt="stale",
        status=PostStatus.PUBLISHED,
    )
    db_session.add(post)
    db_session.commit()

    counts = rebuild_excerpts(db_session, dry_run=True)
    db_session.commit()

    assert counts["posts_changed"] == 1
    db_session.refresh(post)
    assert post.body_excerpt == "stale"  # unchanged


def test_rebuild_excerpts_skips_already_correct_rows(
    db_session: Session,
) -> None:
    from bragi.core.models.site import Site
    from bragi.core.models.user import User

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
    body = "Already correct."
    post = Post(
        site_id=site.id,
        author_id=user.id,
        title="x",
        slug="x",
        body_markdown=body,
        body_html=f"<p>{body}</p>",
        body_excerpt=make_excerpt(body),
        status=PostStatus.PUBLISHED,
    )
    db_session.add(post)
    db_session.commit()

    counts = rebuild_excerpts(db_session)
    assert counts["posts_scanned"] == 1
    assert counts["posts_changed"] == 0


def test_rebuild_excerpts_filters_by_site_id(
    db_session: Session,
) -> None:
    from bragi.core.models.site import Site
    from bragi.core.models.user import User

    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site_a = Site(
        slug="a",
        hostname="a.example.com",
        title="A",
        canonical_url="https://a.example.com",
        owner_user_id=user.id,
    )
    site_b = Site(
        slug="b",
        hostname="b.example.com",
        title="B",
        canonical_url="https://b.example.com",
        owner_user_id=user.id,
    )
    db_session.add_all([site_a, site_b])
    db_session.flush()
    db_session.add(
        Post(
            site_id=site_a.id,
            author_id=user.id,
            title="a",
            slug="a-post",
            body_markdown=r"a 1\.9",
            body_html="<p>a 1.9</p>",
            body_excerpt=r"a 1\.9",
            status=PostStatus.PUBLISHED,
        )
    )
    db_session.add(
        Post(
            site_id=site_b.id,
            author_id=user.id,
            title="b",
            slug="b-post",
            body_markdown=r"b 1\.9",
            body_html="<p>b 1.9</p>",
            body_excerpt=r"b 1\.9",
            status=PostStatus.PUBLISHED,
        )
    )
    db_session.commit()

    counts = rebuild_excerpts(db_session, site_id=site_a.id)
    assert counts["posts_scanned"] == 1
    assert counts["posts_changed"] == 1
