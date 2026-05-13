"""Tests for the sites plugin CLI."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.sites.cli import site_group
from bragi.core.models.site import Site


@pytest.fixture(autouse=True)
def _patch_session_local(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    monkeypatch.setattr("bragi.contrib.sites.cli.SessionLocal", db_session_factory)


def test_site_create_persists_row(db_session_factory: sessionmaker[Session]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        [
            "create",
            "--slug",
            "blog",
            "--hostname",
            "blog.example.com",
            "--title",
            "Blog",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Created site blog" in result.output

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()

    assert site.hostname == "blog.example.com"
    assert site.title == "Blog"
    assert site.locale == "en"
    assert site.timezone == "UTC"
    assert site.canonical_url == "https://blog.example.com"
    assert site.active is True


def test_site_create_normalises_case(db_session_factory: sessionmaker[Session]) -> None:
    """Slug and hostname go in lower-case so hostname lookups in the
    site_resolver middleware (which also lower-cases Host) match."""
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        [
            "create",
            "--slug",
            "MixedCase",
            "--hostname",
            "Blog.Example.COM",
            "--title",
            "Blog",
        ],
    )

    assert result.exit_code == 0, result.output
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "mixedcase")).scalar_one()
    assert site.hostname == "blog.example.com"


def test_site_create_honours_overrides(db_session_factory: sessionmaker[Session]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        [
            "create",
            "--slug",
            "blog-nl",
            "--hostname",
            "blog.example.nl",
            "--title",
            "Blog NL",
            "--locale",
            "nl",
            "--timezone",
            "Europe/Amsterdam",
            "--canonical-url",
            "https://example.nl/blog",
        ],
    )

    assert result.exit_code == 0, result.output
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog-nl")).scalar_one()
    assert site.locale == "nl"
    assert site.timezone == "Europe/Amsterdam"
    assert site.canonical_url == "https://example.nl/blog"


def test_site_create_rejects_duplicate_slug(db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="blog",
                hostname="one.example.com",
                title="One",
                canonical_url="https://one.example.com",
            )
        )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        site_group,
        [
            "create",
            "--slug",
            "blog",
            "--hostname",
            "two.example.com",
            "--title",
            "Two",
        ],
    )

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_site_create_rejects_duplicate_hostname(db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="one",
                hostname="blog.example.com",
                title="One",
                canonical_url="https://blog.example.com",
            )
        )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        site_group,
        [
            "create",
            "--slug",
            "two",
            "--hostname",
            "blog.example.com",
            "--title",
            "Two",
        ],
    )

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_site_list_empty(db_session_factory: sessionmaker[Session]) -> None:
    runner = CliRunner()
    result = runner.invoke(site_group, ["list"])
    assert result.exit_code == 0
    assert "(no sites)" in result.output


def test_site_list_shows_rows(db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="aaa",
                hostname="aaa.example.com",
                title="First",
                canonical_url="https://aaa.example.com",
                active=True,
            )
        )
        db.add(
            Site(
                slug="bbb",
                hostname="bbb.example.com",
                title="Second",
                canonical_url="https://bbb.example.com",
                active=False,
            )
        )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(site_group, ["list"])
    assert result.exit_code == 0
    assert "aaa" in result.output
    assert "bbb" in result.output
    assert "[inactive]" in result.output


def test_sites_plugin_registers_cli(db_session_factory: sessionmaker[Session]) -> None:
    """`flask --app bragi.apps.admin cms site ...` should resolve."""
    from bragi.apps.admin import create_admin_app

    app = create_admin_app()
    cms_group = app.cli.commands["cms"]
    assert "site" in cms_group.commands  # type: ignore[attr-defined]
