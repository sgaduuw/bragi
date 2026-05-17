"""Tests for the WordPress (WXR) importer (#39)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.import_wordpress.importer import apply, detect, plan
from bragi.contrib.import_wordpress.loader import load_export, looks_like_wordpress
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import seed_blog_index

# ============================================================
# fixture builder
# ============================================================


_WXR_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:wp="http://wordpress.org/export/1.2/"
     xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/">
<channel>
  <title>Example Blog</title>
  <link>https://blog.example.com</link>
  <description>A WordPress export.</description>
  <language>en-US</language>
  <wp:base_blog_url>https://blog.example.com</wp:base_blog_url>
  <wp:author>
    <wp:author_id>1</wp:author_id>
    <wp:author_login>ada</wp:author_login>
    <wp:author_email>ada@example.com</wp:author_email>
    <wp:author_display_name>Ada Lovelace</wp:author_display_name>
  </wp:author>
  <wp:author>
    <wp:author_id>2</wp:author_id>
    <wp:author_login>grace</wp:author_login>
    <wp:author_email>grace@example.com</wp:author_email>
    <wp:author_display_name>Grace Hopper</wp:author_display_name>
  </wp:author>
  <wp:category>
    <wp:term_id>10</wp:term_id>
    <wp:category_nicename>news</wp:category_nicename>
    <wp:cat_name>News</wp:cat_name>
  </wp:category>
  <wp:tag>
    <wp:term_id>20</wp:term_id>
    <wp:tag_slug>ada-stuff</wp:tag_slug>
    <wp:tag_name>Ada Stuff</wp:tag_name>
  </wp:tag>
"""

_WXR_FOOTER = "\n</channel>\n</rss>\n"


def _item(
    *,
    post_id: str,
    post_type: str = "post",
    slug: str,
    title: str = "Title",
    body: str = "<p>Body</p>",
    excerpt: str = "",
    status: str = "publish",
    creator: str = "ada",
    link: str | None = None,
    parent: str | int = 0,
    tags: list[tuple[str, str]] | None = None,
    categories: list[tuple[str, str]] | None = None,
    comments: int = 0,
    date_gmt: str = "2026-05-14 12:00:00",
) -> str:
    """Render one `<item>` block for a WXR export."""
    link = link or f"https://blog.example.com/{slug}/"
    cat_xml = "".join(
        f'<category domain="post_tag" nicename="{slug_}">{name}</category>'
        for slug_, name in (tags or [])
    ) + "".join(
        f'<category domain="category" nicename="{slug_}">{name}</category>'
        for slug_, name in (categories or [])
    )
    comments_xml = "".join(
        f"<wp:comment><wp:comment_id>{i}</wp:comment_id></wp:comment>" for i in range(comments)
    )
    return f"""
  <item>
    <title>{title}</title>
    <link>{link}</link>
    <pubDate>Thu, 14 May 2026 12:00:00 +0000</pubDate>
    <dc:creator>{creator}</dc:creator>
    <guid isPermaLink="false">https://blog.example.com/?p={post_id}</guid>
    <description></description>
    <content:encoded><![CDATA[{body}]]></content:encoded>
    <excerpt:encoded><![CDATA[{excerpt}]]></excerpt:encoded>
    <wp:post_id>{post_id}</wp:post_id>
    <wp:post_date>{date_gmt}</wp:post_date>
    <wp:post_date_gmt>{date_gmt}</wp:post_date_gmt>
    <wp:post_name>{slug}</wp:post_name>
    <wp:status>{status}</wp:status>
    <wp:post_parent>{parent}</wp:post_parent>
    <wp:post_type>{post_type}</wp:post_type>
    {cat_xml}
    {comments_xml}
  </item>"""


def _make_export(tmp_path: Path, items: list[str]) -> Path:
    """Write a WXR XML file with the given <item> blocks and return its path."""
    p = tmp_path / "wp-export.xml"
    p.write_text(_WXR_HEADER + "".join(items) + _WXR_FOOTER)
    return p


# ============================================================
# loader / detect
# ============================================================


def test_detect_recognises_wxr_file(tmp_path: Path) -> None:
    p = _make_export(tmp_path, [_item(post_id="1", slug="a")])
    assert detect(p) is True


def test_detect_rejects_unrelated_xml(tmp_path: Path) -> None:
    p = tmp_path / "feed.xml"
    p.write_text("<?xml version='1.0'?><rss><channel><title>Plain RSS</title></channel></rss>")
    assert detect(p) is False


def test_detect_recognises_directory_with_one_xml(tmp_path: Path) -> None:
    _make_export(tmp_path, [_item(post_id="1", slug="a")])
    assert detect(tmp_path) is True


def test_looks_like_wordpress_short_circuits_for_missing_file(tmp_path: Path) -> None:
    assert looks_like_wordpress(tmp_path / "nope.xml") is False


def test_load_export_raises_on_broken_xml(tmp_path: Path) -> None:
    p = tmp_path / "broken.xml"
    p.write_text("<rss><channel>")  # unclosed
    with pytest.raises(ValueError):
        load_export(p)


# ============================================================
# plan
# ============================================================


def test_plan_counts_posts_pages_and_redirects(tmp_path: Path) -> None:
    p = _make_export(
        tmp_path,
        [
            _item(post_id="1", slug="alpha", status="publish"),
            _item(post_id="2", slug="beta", status="draft"),
            _item(post_id="3", slug="about", post_type="page", status="publish"),
        ],
    )
    result = plan(p)
    assert result.counts["posts"] == 2
    assert result.counts["pages"] == 1
    # 1 published post + 1 published page = 2 redirects.
    assert result.redirects == 2


def test_plan_warns_on_shortcodes_and_comments(tmp_path: Path) -> None:
    p = _make_export(
        tmp_path,
        [
            _item(
                post_id="1",
                slug="post",
                body="<p>Hi</p>[gallery ids='1,2,3'] [caption]X[/caption]",
                comments=4,
            )
        ],
    )
    result = plan(p)
    warnings = " ".join(result.warnings)
    assert "[gallery] dropped" in warnings
    assert "[caption] dropped" in warnings
    assert "4 comments dropped" in warnings


# ============================================================
# apply (full round-trip)
# ============================================================


@pytest.fixture
def site_id(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[int]:
    # Importer needs at least one user as the fallback author. The
    # first user doubles as the site owner.
    ada = User(email="ada@example.com", display_name="Ada", is_active=True)
    grace = User(email="grace@example.com", display_name="Grace", is_active=True)
    db_session.add_all([ada, grace])
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=ada.id,
    )
    db_session.add(site)
    db_session.commit()
    # Importer redirect targets now resolve through `post_url_for`
    # / `page_url_for`; both need the site to carry a POST_INDEX
    # page. Seed `slug="posts"` so existing `/posts/<slug>/`
    # assertions in this file keep matching. Also patch the url
    # helper's SessionLocal so it hits the same in-memory DB the
    # test session writes to.
    seed_blog_index(db_session, site, slug="posts")
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    yield site.id


def _detached_site(db_session_factory: sessionmaker[Session], site_id: int) -> Site:
    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        db.expunge(site)
        return site


def test_apply_creates_post_with_markdown_body(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            _item(
                post_id="1",
                slug="hello-world",
                title="Hello World",
                body="<h2>Hi</h2><p>Body <strong>bold</strong>.</p>",
                status="publish",
            )
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    assert result.counts["posts"] == 1
    assert result.counts["posts_created"] == 1
    assert result.counts["posts_updated"] == 0

    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert post.title == "Hello World"
    assert post.slug == "hello-world"
    assert post.status == PostStatus.PUBLISHED
    assert "## Hi" in post.body_markdown
    assert "**bold**" in post.body_markdown
    assert post.source_id == "1"


def test_apply_creates_page_with_parent(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            _item(post_id="10", slug="about", post_type="page", title="About"),
            _item(
                post_id="11",
                slug="team",
                post_type="page",
                title="Team",
                parent="10",
            ),
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        about = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        team = db.execute(select(Page).where(Page.slug == "team")).scalar_one()
    assert team.parent_id == about.id
    assert about.status == PageStatus.PUBLISHED


def test_apply_inserts_permalink_redirects(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            _item(post_id="1", slug="alpha", link="https://blog.example.com/2024/05/alpha/"),
            # WP served this page at `/wp/about-us/`; bragi serves
            # it at `/about/`. The slug differs from the legacy
            # path so a 301 is needed; if they matched, no row
            # would be emitted (a no-op same-path redirect is
            # useless and `_maybe_emit_redirect` skips it).
            _item(
                post_id="2",
                slug="about",
                post_type="page",
                link="https://blog.example.com/wp/about-us/",
            ),
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    assert result.redirects_inserted == 2
    with db_session_factory() as db:
        rows = db.execute(select(Redirect)).scalars().all()
    by_source = {r.source_path: r for r in rows}
    # Posts resolve via the site's post_index page (seeded as
    # `slug="posts"` by the fixture); pages via the canonical
    # page URL (`/about/` for a root page).
    assert by_source["/2024/05/alpha/"].target == "/posts/alpha/"
    assert by_source["/wp/about-us/"].target == "/about/"
    for r in rows:
        assert r.source == RedirectSource.IMPORT_WORDPRESS
        assert r.status_code == 301


def test_apply_is_idempotent_via_source_id(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running apply on the same export updates rows, not duplicates."""
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    items = [
        _item(post_id="1", slug="alpha", title="Alpha"),
        _item(post_id="2", slug="about", post_type="page", title="About"),
    ]
    p = _make_export(tmp_path, items)
    site = _detached_site(db_session_factory, site_id)

    apply(p, site, {})
    # Tweak title in the export, re-apply.
    items_v2 = [
        _item(post_id="1", slug="alpha", title="Alpha v2"),
        _item(post_id="2", slug="about", post_type="page", title="About v2"),
    ]
    p.write_text(_WXR_HEADER + "".join(items_v2) + _WXR_FOOTER)
    result2 = apply(p, site, {})
    assert result2.counts["posts_created"] == 0
    assert result2.counts["posts_updated"] == 1
    assert result2.counts["pages_created"] == 0
    assert result2.counts["pages_updated"] == 1

    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        # `select(Page)` returns both the imported page AND the
        # POST_INDEX page the fixture seeds. Filter to the
        # imported "about" page so the test reads what it claims.
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
    assert post.title == "Alpha v2"
    assert page.title == "About v2"


def test_apply_collapses_categories_with_prefix(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Categories land in Tag with a `category:` slug prefix."""
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            _item(
                post_id="1",
                slug="alpha",
                tags=[("ada-stuff", "Ada Stuff")],
                categories=[("news", "News")],
            )
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        slugs = sorted(t.slug for t in post.tags)
    assert slugs == ["ada-stuff", "category:news"]
    # Display labels are preserved without prefix on the category.
    with db_session_factory() as db:
        cat = db.execute(select(Tag).where(Tag.slug == "category:news")).scalar_one()
    assert cat.label == "News"


def test_apply_strips_shortcodes_from_body(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            _item(
                post_id="1",
                slug="alpha",
                body="<p>Before</p>[gallery ids='1,2'] inner [/gallery]<p>After</p>",
            )
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    assert any("[gallery] dropped" in w for w in result.warnings)
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert "[gallery" not in post.body_markdown
    assert "Before" in post.body_markdown
    assert "After" in post.body_markdown


def test_apply_resolves_author_by_email(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [_item(post_id="1", slug="alpha", creator="grace")],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        grace = db.execute(select(User).where(User.email == "grace@example.com")).scalar_one()
    assert post.author_id == grace.id


def test_apply_warns_on_comments_attachments_and_shortcodes(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_wordpress.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            _item(
                post_id="1",
                slug="alpha",
                body="<p>Hi</p>[caption]X[/caption]",
                comments=2,
            ),
            _item(
                post_id="2",
                slug="image-1",
                post_type="attachment",
                title="image-1",
                body="",
            ),
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    warnings = " ".join(result.warnings)
    assert "[caption] dropped" in warnings
    assert "2 comments dropped" in warnings
    assert "attachment(s) skipped" in warnings


def test_plugin_registers_via_entry_point() -> None:
    """The importer is discoverable via the bragi.plugins entry point."""
    from bragi.contrib.import_wordpress.plugin import register_importer

    spec = register_importer()
    assert spec.name == "wordpress"
    assert callable(spec.detect)
    assert callable(spec.plan)
    assert callable(spec.apply)
