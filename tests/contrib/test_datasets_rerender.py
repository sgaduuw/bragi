"""Rerender pass: re-bake content when a dataset's bytes change (#42).

Rendering needs the full plugin-configured renderer, so these
tests build the admin app (which wires `register_markdown_extension`)
and call `rerender_for_dataset` inside its app context. They reuse
the seeded_dataset fixture shape from the directive tests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models import Dataset, Post
from bragi.core.models.post import PostStatus
from bragi.core.storage import storage_path_for
from bragi.settings import settings
from tests.conftest import make_test_site, make_test_user


@pytest.fixture
def admin_app(patched_session_locals: sessionmaker[Session]) -> Flask:
    del patched_session_locals
    from bragi.apps.admin import create_admin_app

    return create_admin_app()


@pytest.fixture
def seeded(db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "attachments_root", str(tmp_path / "uploads"))
    site = make_test_site(
        db_session,
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    user = make_test_user(db_session)

    def store_duckdb(values: str) -> tuple[str, int]:
        dbfile = tmp_path / f"src-{hashlib.sha256(values.encode()).hexdigest()[:8]}.duckdb"
        con = duckdb.connect(str(dbfile))
        con.execute("CREATE TABLE cpi (quarter VARCHAR, value DOUBLE)")
        con.execute(f"INSERT INTO cpi VALUES {values}")
        con.close()
        data = dbfile.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        dest = storage_path_for(site.slug, sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return sha, len(data)

    sha, size = store_duckdb("('2025Q1', 102.5)")
    ds = Dataset(
        site_id=site.id,
        slug="cpi",
        name="CPI",
        source_type="duckdb",
        storage_key=sha,
        size_bytes=size,
        content_sha=sha,
    )
    db_session.add(ds)
    db_session.commit()
    return site, user, ds, store_duckdb


def test_rerender_updates_referencing_post(admin_app: Flask, db_session: Session, seeded) -> None:
    site, user, ds, store_duckdb = seeded
    body = '::: dataset slug=cpi sql="SELECT value FROM cpi" format=scalar\n:::\n'
    with admin_app.app_context():
        from bragi.core.render.markdown import render_markdown

        post = Post(
            site_id=site.id,
            slug="p1",
            title="P1",
            body_markdown=body,
            body_html=render_markdown(body, env={"bragi_site_id": site.id}),
            author_id=user.id,
            status=PostStatus.PUBLISHED,
        )
        db_session.add(post)
        db_session.commit()
        assert "102.5" in post.body_html

        # Simulate re-upload: new bytes, same dataset row.
        new_sha, new_size = store_duckdb("('2025Q1', 999.9)")
        ds_row = db_session.get(Dataset, ds.id)
        ds_row.storage_key = new_sha
        ds_row.content_sha = new_sha
        ds_row.size_bytes = new_size
        db_session.commit()

        from bragi.contrib.datasets.rerender import rerender_for_dataset

        stats = rerender_for_dataset(site.id, "cpi")

    assert stats.rows_updated == 1
    db_session.expire_all()
    refreshed = db_session.execute(select(Post).where(Post.slug == "p1")).scalar_one()
    assert "999.9" in refreshed.body_html
    assert "102.5" not in refreshed.body_html


def test_rerender_skips_unrelated_content(admin_app: Flask, db_session: Session, seeded) -> None:
    site, user, _, _ = seeded
    with admin_app.app_context():
        from bragi.core.render.markdown import render_markdown

        post = Post(
            site_id=site.id,
            slug="plain",
            title="Plain",
            body_markdown="just text",
            body_html=render_markdown("just text"),
            author_id=user.id,
            status=PostStatus.PUBLISHED,
        )
        db_session.add(post)
        db_session.commit()

        from bragi.contrib.datasets.rerender import rerender_for_dataset

        stats = rerender_for_dataset(site.id, "cpi")
    assert stats.rows_scanned == 0


def test_rerender_dry_run_writes_nothing(admin_app: Flask, db_session: Session, seeded) -> None:
    site, user, ds, store_duckdb = seeded
    body = '::: dataset slug=cpi sql="SELECT value FROM cpi" format=scalar\n:::\n'
    with admin_app.app_context():
        from bragi.core.render.markdown import render_markdown

        post = Post(
            site_id=site.id,
            slug="p2",
            title="P2",
            body_markdown=body,
            body_html=render_markdown(body, env={"bragi_site_id": site.id}),
            author_id=user.id,
            status=PostStatus.PUBLISHED,
        )
        db_session.add(post)
        db_session.commit()
        new_sha, new_size = store_duckdb("('2025Q1', 7.0)")
        ds_row = db_session.get(Dataset, ds.id)
        ds_row.storage_key = new_sha
        ds_row.content_sha = new_sha
        ds_row.size_bytes = new_size
        db_session.commit()

        from bragi.contrib.datasets.rerender import rerender_for_dataset

        stats = rerender_for_dataset(site.id, "cpi", dry_run=True)

    assert stats.rows_updated == 1  # would-update count in dry-run
    db_session.expire_all()
    refreshed = db_session.execute(select(Post).where(Post.slug == "p2")).scalar_one()
    assert "102.5" in refreshed.body_html


def test_directive_pattern_does_not_match_hyphenated_sibling_slug() -> None:
    from bragi.contrib.datasets.rerender import _directive_pattern

    pattern = _directive_pattern("cpi")
    assert pattern.search("::: dataset slug=cpi q=x\n:::\n")
    assert pattern.search("::: dataset format=table slug=cpi\n:::\n")
    assert not pattern.search("::: dataset slug=cpi-extended q=x\n:::\n")
