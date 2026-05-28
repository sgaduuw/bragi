"""Tests for the sites plugin CLI."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.sites.cli import site_group
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.user import User
from tests.conftest import make_test_user


@pytest.fixture(autouse=True)
def _patch_session_local(patched_session_locals: sessionmaker[Session]) -> None:
    """Bind every SessionLocal in this module to the test factory.

    Pre-proxy this used to enumerate `bragi.contrib.sites.cli` and
    `bragi.core.audit` (the transfer subcommand emits an audit row,
    which opens its own SessionLocal); since the proxy refactor the
    shared `patched_session_locals` fixture covers both via one
    `_factory` rebind. The autouse wrapper stays so the module's
    tests don't all need to declare the dependency individually.
    """


@pytest.fixture(autouse=True)
def _seed_superuser(db_session_factory: sessionmaker[Session]) -> User:
    """Pre-seed a superuser so `cms site create` (which defaults the
    owner to the first superuser) succeeds without an explicit
    --owner flag. Tests that directly construct `Site(...)` also use
    this user's id as `owner_user_id`."""
    with db_session_factory() as db:
        sup = make_test_user(db, email="_super@example.com", is_superuser=True)
        db.commit()
        # Re-fetch so the attribute access after session-close is fine.
        return db.get(User, sup.id)  # type: ignore[return-value]


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


def test_site_create_rejects_duplicate_slug(
    db_session_factory: sessionmaker[Session], _seed_superuser: User
) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="blog",
                hostname="one.example.com",
                title="One",
                canonical_url="https://one.example.com",
                owner_user_id=_seed_superuser.id,
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


def test_site_create_rejects_duplicate_hostname(
    db_session_factory: sessionmaker[Session], _seed_superuser: User
) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="one",
                hostname="blog.example.com",
                title="One",
                canonical_url="https://blog.example.com",
                owner_user_id=_seed_superuser.id,
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


def test_site_list_shows_rows(
    db_session_factory: sessionmaker[Session], _seed_superuser: User
) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="aaa",
                hostname="aaa.example.com",
                title="First",
                canonical_url="https://aaa.example.com",
                active=True,
                owner_user_id=_seed_superuser.id,
            )
        )
        db.add(
            Site(
                slug="bbb",
                hostname="bbb.example.com",
                title="Second",
                canonical_url="https://bbb.example.com",
                active=False,
                owner_user_id=_seed_superuser.id,
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
    """`flask --app bragi.apps.admin site ...` should resolve.

    Plugin commands are now registered directly on `app.cli` (the
    `cms` wrapper group was removed in the bragi CLI entry-point
    refactor), so `site` is a top-level `app.cli` command.
    """
    from bragi.apps.admin import create_admin_app

    app = create_admin_app()
    assert app.cli.get_command(None, "site") is not None, (
        "The `site` command must be reachable from app.cli directly; "
        "plugin commands are no longer nested under a `cms` group."
    )


def test_site_extra_settings_defaults_to_empty_dict(
    db_session_factory: sessionmaker[Session], _seed_superuser: User
) -> None:
    with db_session_factory() as db:
        db.add(
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
                owner_user_id=_seed_superuser.id,
            )
        )
        db.commit()
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    assert site.extra_settings == {}


def test_site_extra_settings_in_place_mutation_persists(
    db_session_factory: sessionmaker[Session], _seed_superuser: User
) -> None:
    """`MutableDict.as_mutable` tracks in-place sets without reassignment."""
    with db_session_factory() as db:
        db.add(
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
                owner_user_id=_seed_superuser.id,
            )
        )
        db.commit()
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings["robots_txt_override"] = "User-agent: *\nDisallow: /private/\n"
        site.extra_settings["analytics_enabled"] = False
        db.commit()
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    assert site.extra_settings["robots_txt_override"].startswith("User-agent:")
    assert site.extra_settings["analytics_enabled"] is False


# ============================================================
# alias subcommands (#25)
# ============================================================


def _seed_blog(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        owner = make_test_user(db)
        site = Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=owner.id,
        )
        db.add(site)
        db.commit()
        return site.id


def test_alias_add_persists_row(db_session_factory: sessionmaker[Session]) -> None:
    _seed_blog(db_session_factory)
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["alias", "add", "--site", "blog", "--hostname", "www.blog.example.com"],
    )
    assert result.exit_code == 0, result.output
    with db_session_factory() as db:
        alias = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == "www.blog.example.com")
        ).scalar_one()
    assert alias.site_id is not None


def test_alias_add_rejects_unknown_site(db_session_factory: sessionmaker[Session]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["alias", "add", "--site", "nope", "--hostname", "x.example.com"],
    )
    assert result.exit_code == 1
    assert "No site with slug" in result.output


def test_alias_add_rejects_existing_canonical_hostname(
    db_session_factory: sessionmaker[Session],
) -> None:
    _seed_blog(db_session_factory)
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["alias", "add", "--site", "blog", "--hostname", "blog.example.com"],
    )
    assert result.exit_code == 1
    assert "canonical hostname" in result.output


def test_alias_add_rejects_duplicate_alias(db_session_factory: sessionmaker[Session]) -> None:
    site_id = _seed_blog(db_session_factory)
    with db_session_factory() as db:
        db.add(SiteAlias(site_id=site_id, hostname="www.blog.example.com"))
        db.commit()
    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["alias", "add", "--site", "blog", "--hostname", "www.blog.example.com"],
    )
    assert result.exit_code == 1
    assert "already an alias" in result.output


def test_alias_list_filters_by_site(db_session_factory: sessionmaker[Session]) -> None:
    site_id = _seed_blog(db_session_factory)
    with db_session_factory() as db:
        db.add(SiteAlias(site_id=site_id, hostname="www.blog.example.com"))
        db.add(SiteAlias(site_id=site_id, hostname="legacy.example.com"))
        db.commit()
    runner = CliRunner()
    result = runner.invoke(site_group, ["alias", "list", "--site", "blog"])
    assert result.exit_code == 0
    assert "www.blog.example.com" in result.output
    assert "legacy.example.com" in result.output


def test_alias_remove(db_session_factory: sessionmaker[Session]) -> None:
    site_id = _seed_blog(db_session_factory)
    with db_session_factory() as db:
        db.add(SiteAlias(site_id=site_id, hostname="www.blog.example.com"))
        db.commit()
    runner = CliRunner()
    result = runner.invoke(site_group, ["alias", "remove", "--hostname", "www.blog.example.com"])
    assert result.exit_code == 0
    with db_session_factory() as db:
        remaining = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == "www.blog.example.com")
        ).scalar_one_or_none()
    assert remaining is None


# ============================================================
# `cms site transfer` (P1 / #77)
# ============================================================


def test_site_transfer_flips_owner(db_session_factory: sessionmaker[Session]) -> None:
    """Happy path: the named user becomes the site's new owner_user_id."""
    site_id = _seed_blog(db_session_factory)
    # Seed the destination user.
    with db_session_factory() as db:
        new_owner = make_test_user(db, email="new-owner@example.com")
        db.commit()
        new_owner_id = new_owner.id

    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["transfer", "--site", "blog", "--to", "new-owner@example.com"],
    )
    assert result.exit_code == 0, result.output
    assert "Transferred ownership" in result.output

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.id == site_id)).scalar_one()
    assert site.owner_user_id == new_owner_id


def test_site_transfer_unknown_site_errors(db_session_factory: sessionmaker[Session]) -> None:
    """Targeting a non-existent slug exits 1 with a friendly message."""
    # Need a real user to exist so the slug-lookup is the failing step.
    with db_session_factory() as db:
        make_test_user(db, email="someone@example.com")
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["transfer", "--site", "ghost", "--to", "someone@example.com"],
    )
    assert result.exit_code == 1
    assert "No site with slug" in result.output


def test_site_transfer_unknown_email_errors(db_session_factory: sessionmaker[Session]) -> None:
    """Targeting a non-existent user email exits 1 with a friendly message."""
    _seed_blog(db_session_factory)

    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["transfer", "--site", "blog", "--to", "nobody@example.com"],
    )
    assert result.exit_code == 1
    assert "No user with email" in result.output


def test_site_transfer_same_owner_is_noop(db_session_factory: sessionmaker[Session]) -> None:
    """Transferring to the existing owner emits a 'no change' message
    and leaves the row untouched (no audit row emitted either)."""
    site_id = _seed_blog(db_session_factory)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.id == site_id)).scalar_one()
        current_owner = db.execute(select(User).where(User.id == site.owner_user_id)).scalar_one()
        owner_email = current_owner.email
        owner_id_before = site.owner_user_id

    runner = CliRunner()
    result = runner.invoke(
        site_group,
        ["transfer", "--site", "blog", "--to", owner_email],
    )
    assert result.exit_code == 0, result.output
    assert "no change" in result.output

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.id == site_id)).scalar_one()
    assert site.owner_user_id == owner_id_before
