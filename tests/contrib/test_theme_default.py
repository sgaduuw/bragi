"""Tests for the in-tree default theme (#111).

Covers:
- `register_theme` returns one ThemeSpec under slug `"default"`.
- The packaged template loader actually resolves `delivery/base.html`.
- The delivery app auto-registers the theme via the entry-point group
  (i.e. the wiring in `pyproject.toml` is correct).
- A site with `Site.theme=NULL` renders through the default theme's
  shell, exercising the `ThemeAwareLoader` NULL -> "default" fallback.
"""

from __future__ import annotations

import jinja2
import pytest
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.theme_default import plugin as theme_default_plugin
from bragi.core.models.site import Site
from tests.conftest import make_test_user


def test_register_theme_returns_default_spec() -> None:
    spec = theme_default_plugin.register_theme()
    assert spec.slug == "default"
    assert spec.display_name == "Default"
    assert spec.static_dir is None


def test_packaged_loader_finds_base_template() -> None:
    """The PackageLoader is rooted correctly: `delivery/base.html` resolves."""
    spec = theme_default_plugin.register_theme()
    env = jinja2.Environment(loader=spec.template_loader)
    src, _name, _uptodate = spec.template_loader.get_source(env, "delivery/base.html")
    assert "<!DOCTYPE html>" in src
    assert "{% block content %}" in src


@pytest.fixture
def site_with_null_theme(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> None:
    """One Site with theme=NULL on the in-memory DB."""
    owner = make_test_user(db_session)
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
            theme=None,
            owner_user_id=owner.id,
        )
    )
    db_session.commit()


def test_delivery_app_registers_default_theme(site_with_null_theme: None) -> None:
    """Entry-point wiring: `create_delivery_app` discovers theme_default
    and the registry exposes it under slug 'default'."""
    app = create_delivery_app()
    spec = app.extensions["registry"].theme("default")
    assert spec is not None
    assert spec.display_name == "Default"


def test_null_theme_renders_through_default_shell(site_with_null_theme: None) -> None:
    """A Site with theme=NULL renders `delivery/base.html` via the
    in-tree default theme. Confirms the NULL -> "default" fallback in
    ThemeAwareLoader and the entry-point wiring."""
    app = create_delivery_app()

    @app.route("/__base_smoke")
    def _smoke() -> str:
        from flask import render_template

        return render_template(
            "delivery/base.html",
            site=None,
            meta_description=None,
            canonical_url=None,
            noindex=False,
        )

    client = app.test_client()
    resp = client.get("/__base_smoke", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"<!DOCTYPE html>" in resp.data


def test_theme_default_ships_image_size_classes() -> None:
    """The picker / bubble menu writes size-* and align-* classes
    that the theme CSS must style. theme_default ships the baseline;
    the other in-tree themes (terminal / minimal / serif) inherit
    by copy-paste in this PR.
    """
    from importlib.resources import files

    css = files("bragi.contrib.theme_default.templates.delivery").joinpath("base.html").read_text()
    for cls in (
        "img.size-small",
        "img.size-medium",
        "img.size-full",
        "img.align-left",
        "img.align-right",
        "img.align-center",
    ):
        assert cls in css, f"theme_default missing rule for {cls}"
