"""Tests for the themes plugin (#40).

Covers the ThemeSpec dataclass, the Registry hook, the
`ThemeAwareLoader`, the `/theme/<slug>/static/<path>` blueprint,
the admin site-edit dropdown, and the `cms theme list` CLI.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import jinja2
import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import ThemeSpec
from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.themes.cli import theme_group
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.registry import Registry
from bragi.core.themes import ThemeAwareLoader
from tests.conftest import csrf_token

# ============================================================
# ThemeSpec + Registry
# ============================================================


def test_theme_spec_minimal_fields() -> None:
    loader = jinja2.DictLoader({"x.html": "x"})
    spec = ThemeSpec(slug="minimal", display_name="Minimal", template_loader=loader)
    assert spec.slug == "minimal"
    assert spec.static_dir is None


def test_registry_add_and_lookup() -> None:
    registry = Registry()
    loader = jinja2.DictLoader({})
    registry.add_theme(ThemeSpec(slug="alpha", display_name="Alpha", template_loader=loader))
    registry.add_theme(ThemeSpec(slug="beta", display_name="Beta", template_loader=loader))
    assert registry.theme("alpha").slug == "alpha"
    assert registry.theme("beta").display_name == "Beta"
    assert registry.theme("ghost") is None  # unknown slug returns None, not raise


# ============================================================
# ThemeAwareLoader
# ============================================================


@pytest.fixture
def seeded_themed_site(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[None]:
    """One Site with theme='stub-theme' on the in-memory DB."""
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
            theme="stub-theme",
        )
    )
    db_session.commit()
    yield


def _install_stub_theme(app: Flask, slug: str = "stub-theme") -> None:
    """Add a ThemeSpec that overrides `delivery/base.html` and a fixture template."""
    loader = jinja2.DictLoader(
        {
            "delivery/base.html": (
                "<!doctype html><html><body><main>THEMED_BASE"
                "{% block content %}{% endblock %}</main></body></html>"
            ),
            "themed_only.html": "FROM_THEME",
        }
    )
    spec = ThemeSpec(slug=slug, display_name="Stub", template_loader=loader)
    app.extensions["registry"].add_theme(spec)


def test_theme_loader_dispatches_when_site_has_theme(
    seeded_themed_site: None,
) -> None:
    app = create_delivery_app()
    _install_stub_theme(app)

    @app.route("/__themed")
    def _themed():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'delivery/base.html' %}{% block content %}.{% endblock %}"
        )

    client = app.test_client()
    resp = client.get("/__themed", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"THEMED_BASE" in resp.data


def test_theme_loader_falls_back_when_site_has_no_theme(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A site with theme=NULL renders with the default chain; the theme's
    override never fires."""
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
            theme=None,
        )
    )
    db_session.commit()

    app = create_delivery_app()
    _install_stub_theme(app)  # registered, but the site didn't opt in

    @app.route("/__test")
    def _test():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'delivery/base.html' %}{% block content %}.{% endblock %}"
        )

    client = app.test_client()
    resp = client.get("/__test", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"THEMED_BASE" not in resp.data


def test_theme_loader_falls_back_on_unknown_slug(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A site referencing an uninstalled theme slug must not 500; it
    falls back to the default chain so an operator can `pip uninstall`
    a theme without breaking existing sites."""
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
            theme="never-installed",
        )
    )
    db_session.commit()

    app = create_delivery_app()  # no theme registered

    @app.route("/__test")
    def _test():
        from flask import render_template_string

        return render_template_string(
            "{% extends 'delivery/base.html' %}{% block content %}.{% endblock %}"
        )

    client = app.test_client()
    resp = client.get("/__test", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200


def test_theme_loader_outside_request_context_is_safe() -> None:
    """`ThemeAwareLoader` must not raise when there's no Flask context.
    Tests that import the loader directly (e.g. unit tests) shouldn't
    have to push a request context to exercise it."""
    fallback = jinja2.DictLoader({"x.html": "x"})
    loader = ThemeAwareLoader(fallback)
    src, name, _ = loader.get_source(jinja2.Environment(), "x.html")
    assert src == "x"


# ============================================================
# /theme/<slug>/static/<path>
# ============================================================


@pytest.fixture
def themed_delivery_app_with_static(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
) -> Iterator[Flask]:
    """Delivery app with a theme that has a real static_dir on disk."""
    static_dir = tmp_path / "stub-theme-static"
    static_dir.mkdir()
    (static_dir / "site.css").write_text("body { color: rebeccapurple; }")

    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
        )
    )
    db_session.commit()

    app = create_delivery_app()
    app.extensions["registry"].add_theme(
        ThemeSpec(
            slug="stub-theme",
            display_name="Stub",
            template_loader=jinja2.DictLoader({}),
            static_dir=static_dir,
        )
    )
    yield app


def test_theme_static_serves_file(themed_delivery_app_with_static: Flask) -> None:
    client = themed_delivery_app_with_static.test_client()
    resp = client.get("/theme/stub-theme/static/site.css", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"rebeccapurple" in resp.data


def test_theme_static_404_unknown_slug(themed_delivery_app_with_static: Flask) -> None:
    client = themed_delivery_app_with_static.test_client()
    resp = client.get("/theme/ghost/static/site.css", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_theme_static_404_missing_file(themed_delivery_app_with_static: Flask) -> None:
    client = themed_delivery_app_with_static.test_client()
    resp = client.get(
        "/theme/stub-theme/static/missing.css",
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 404


def test_theme_static_404_theme_without_static_dir(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> None:
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
        )
    )
    db_session.commit()
    app = create_delivery_app()
    app.extensions["registry"].add_theme(
        ThemeSpec(slug="no-static", display_name="NS", template_loader=jinja2.DictLoader({}))
    )
    resp = app.test_client().get(
        "/theme/no-static/static/anything.css", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


# ============================================================
# Admin: theme dropdown
# ============================================================

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app_with_user(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
        )
    )
    db_session.commit()
    app = create_admin_app()
    app.extensions["registry"].add_theme(
        ThemeSpec(
            slug="stub-theme",
            display_name="Stub Theme",
            template_loader=jinja2.DictLoader({}),
        )
    )
    yield app


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_admin_edit_lists_themes_in_dropdown(
    admin_app_with_user: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app_with_user.test_client()
    _login(client)
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id)).scalar_one()
    resp = client.get(f"/admin/sites/{site_id}/edit")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Stub Theme" in body
    assert 'name="theme"' in body
    assert "Default (no theme)" in body


def test_admin_edit_persists_theme_slug(
    admin_app_with_user: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app_with_user.test_client()
    _login(client)
    with db_session_factory() as db:
        site = db.execute(select(Site)).scalar_one()
        site_id = site.id
    token = csrf_token(client, path="/")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "_csrf_token": token,
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "stub-theme",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        site = db.execute(select(Site)).scalar_one()
        assert site.theme == "stub-theme"


def test_admin_edit_clears_theme_when_empty(
    admin_app_with_user: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """An empty string in the form drops the theme back to NULL."""
    with db_session_factory() as db:
        site = db.execute(select(Site)).scalar_one()
        site.theme = "stub-theme"
        db.commit()
        site_id = site.id
    client = admin_app_with_user.test_client()
    _login(client)
    token = csrf_token(client, path="/")
    client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "_csrf_token": token,
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
        },
        follow_redirects=False,
    )
    with db_session_factory() as db:
        assert db.execute(select(Site.theme)).scalar_one() is None


def test_admin_edit_rejects_unknown_theme(
    admin_app_with_user: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app_with_user.test_client()
    _login(client)
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id)).scalar_one()
    token = csrf_token(client, path="/")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "_csrf_token": token,
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "never-installed",
        },
    )
    # Not a redirect: the form re-renders with the error flashed.
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "never-installed" in body or "Unknown theme" in body
    with db_session_factory() as db:
        assert db.execute(select(Site.theme)).scalar_one() is None


# ============================================================
# cms theme list CLI
# ============================================================


def test_cli_lists_no_themes(admin_app_with_user: Flask) -> None:
    """Pop the seeded stub theme out of the registry so we exercise the
    `nothing registered` branch of the CLI output."""
    admin_app_with_user.extensions["registry"].themes.clear()
    runner = admin_app_with_user.test_cli_runner()
    result = runner.invoke(args=["cms", "theme", "list"])
    assert result.exit_code == 0, result.output
    assert "No themes registered" in result.output


def test_cli_lists_registered_themes(admin_app_with_user: Flask) -> None:
    runner = admin_app_with_user.test_cli_runner()
    result = runner.invoke(args=["cms", "theme", "list"])
    assert result.exit_code == 0, result.output
    assert "Stub Theme" in result.output
    assert "stub-theme" in result.output


def test_theme_group_standalone() -> None:
    """`theme_group` is importable and lists `list` as a subcommand."""
    assert "list" in theme_group.commands
