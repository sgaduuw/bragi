"""Tests for the `cms export` CLI (#145).

Covers the Hugo-shaped tree shape, frontmatter fidelity, the
attachment manifest + bytes copy, the redirect CSV, and the
round-trip property: import a synthetic Hugo tree, export, then
import the export back and assert no new rows are created.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.cli import cms
from bragi.contrib.import_hugo.importer import apply as hugo_apply
from bragi.core.models.attachment import Attachment
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.storage import storage_path_for
from tests.conftest import make_test_user


@pytest.fixture
def populated(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Session:
    """A site with two posts, one page, one attachment, two redirects."""
    del patched_session_locals
    monkeypatch.setattr("bragi.core.storage.settings.attachments_root", str(tmp_path / "blobs"))

    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()

    tag = Tag(site_id=site.id, slug="python", label="Python")
    db_session.add(tag)
    db_session.flush()

    post_a = Post(
        site_id=site.id,
        slug="hello",
        title="Hello, world",
        body_markdown="# Hi\n\nThis is the body.\n",
        body_html="",
        body_excerpt="",
        author_id=owner.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, 8, 0, tzinfo=UTC),
        meta_description="A first post",
        source_id="content/posts/hello.md",
    )
    post_a.tags = [tag]
    post_b = Post(
        site_id=site.id,
        slug="draft-post",
        title="Draft",
        body_markdown="WIP\n",
        body_html="",
        body_excerpt="",
        author_id=owner.id,
        status=PostStatus.DRAFT,
        source_id="content/posts/draft-post.md",
    )
    db_session.add_all([post_a, post_b])

    page = Page(
        site_id=site.id,
        slug="about",
        title="About",
        body_markdown="About this site.\n",
        body_html="",
        body_excerpt="",
        author_id=owner.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(page)

    attachment = Attachment(
        site_id=site.id,
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=4,
        storage_key="a" * 64,
        width=100,
        height=80,
        alt_text="A photo",
    )
    db_session.add(attachment)
    db_session.flush()

    # Write the bytes to disk so the exporter can copy them.
    blob_path = storage_path_for("blog", attachment.storage_key)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"\xff\xd8\xff\xe0")

    db_session.add_all(
        [
            Redirect(
                site_id=site.id,
                source_path="/old-hello/",
                target="/posts/hello/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.IMPORT_HUGO,
            ),
            Redirect(
                site_id=site.id,
                source_path="/gone/",
                target="/",
                status_code=410,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            ),
        ]
    )
    db_session.commit()
    return db_session


def test_export_writes_posts_pages_attachments_redirects(
    populated: Session, tmp_path: Path
) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    result = runner.invoke(cms, ["export", "--output", str(out)])
    assert result.exit_code == 0, result.output

    site_root = out / "blog"
    assert (site_root / "content/posts/hello.md").is_file()
    assert (site_root / "content/posts/draft-post.md").is_file()
    assert (site_root / "content/pages/about.md").is_file()
    assert (site_root / "static/attachments/attachments.csv").is_file()
    assert (site_root / "static/attachments" / ("a" * 64)).is_file()
    assert (site_root / "redirects.csv").is_file()


def test_export_post_frontmatter_includes_tags_date_aliases(
    populated: Session, tmp_path: Path
) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out)])

    body = (out / "blog/content/posts/hello.md").read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "title: Hello, world" in body
    assert "date: 2026-05-14T08:00:00+00:00" in body
    assert "description: A first post" in body
    assert "tags:\n  - Python" in body
    assert "aliases:\n  - /old-hello/" in body
    # Body follows frontmatter intact.
    assert body.endswith("# Hi\n\nThis is the body.\n")


def test_export_draft_carries_draft_true_and_no_date(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out)])

    body = (out / "blog/content/posts/draft-post.md").read_text(encoding="utf-8")
    assert "draft: true" in body
    assert "date:" not in body


def test_export_page_carries_kind(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out)])

    body = (out / "blog/content/pages/about.md").read_text(encoding="utf-8")
    assert "kind: static" in body


def test_export_attachment_bytes_copied(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out)])

    blob = out / "blog/static/attachments" / ("a" * 64)
    assert blob.read_bytes() == b"\xff\xd8\xff\xe0"


def test_export_attachment_manifest_has_metadata(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out)])

    with (out / "blog/static/attachments/attachments.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["filename"] == "photo.jpg"
    assert rows[0]["content_type"] == "image/jpeg"
    assert rows[0]["alt_text"] == "A photo"
    assert rows[0]["width"] == "100"


def test_export_redirects_csv_has_both_rows(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out)])

    with (out / "blog/redirects.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    paths = {r["source_path"] for r in rows}
    assert {"/old-hello/", "/gone/"} == paths
    by_path = {r["source_path"]: r for r in rows}
    assert by_path["/gone/"]["status_code"] == "410"


def test_export_filters_by_site_slug(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    result = runner.invoke(cms, ["export", "--site", "blog", "--output", str(out)])
    assert result.exit_code == 0
    assert (out / "blog").is_dir()


def test_export_unknown_site_exits_nonzero(populated: Session, tmp_path: Path) -> None:
    out = tmp_path / "export"
    runner = CliRunner()
    result = runner.invoke(cms, ["export", "--site", "nope", "--output", str(out)])
    assert result.exit_code != 0
    assert "No site" in result.output


def test_export_is_deterministic(populated: Session, tmp_path: Path) -> None:
    """Second export of an unchanged DB yields byte-identical files."""
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    runner = CliRunner()
    runner.invoke(cms, ["export", "--output", str(out1)])
    runner.invoke(cms, ["export", "--output", str(out2)])

    files1 = sorted(p.relative_to(out1) for p in out1.rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(out2) for p in out2.rglob("*") if p.is_file())
    assert files1 == files2
    for rel in files1:
        assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes(), rel


def test_export_round_trips_through_hugo_import(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hugo source → import → export → re-import is a no-op for posts.

    No new rows are created on the second import; the existing
    rows are updated in place (idempotent on `(site_id, source_id)`).
    """
    del patched_session_locals
    monkeypatch.setattr("bragi.core.storage.settings.attachments_root", str(tmp_path / "blobs"))

    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()

    # Build a minimal Hugo source tree.
    source = tmp_path / "src"
    (source / "content/posts").mkdir(parents=True)
    (source / "config.toml").write_text("baseURL = '/'\n")
    (source / "content/posts/hello.md").write_text(
        "---\n"
        "title: Hello\n"
        "date: 2026-05-14T08:00:00+00:00\n"
        "tags:\n"
        "  - Python\n"
        "---\n"
        "# Hi\n\nBody.\n",
        encoding="utf-8",
    )
    (source / "content/posts/draft.md").write_text(
        "---\ntitle: Draft\ndraft: true\n---\nWIP.\n", encoding="utf-8"
    )

    # First import: 2 posts created.
    result = hugo_apply(source, site, {"author_id": owner.id})
    assert result.counts.get("posts_created", 0) == 2

    # Export.
    out = tmp_path / "export"
    runner = CliRunner()
    invoked = runner.invoke(cms, ["export", "--output", str(out)])
    assert invoked.exit_code == 0, invoked.output

    # Re-import the export. Should update both posts in place; no
    # new rows created.
    re_imported = hugo_apply(out / "blog", site, {"author_id": owner.id})
    assert re_imported.counts.get("posts_created", 0) == 0
    assert re_imported.counts.get("posts_updated", 0) == 2

    with db_session.begin_nested():
        post_count = db_session.execute(select(Post).where(Post.site_id == site.id)).scalars().all()
    assert len(post_count) == 2
