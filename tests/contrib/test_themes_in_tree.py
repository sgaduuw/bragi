"""Tests for the in-tree theme set (#126).

Asserts that `theme_default`, `theme_minimal`, `theme_serif`, and
`theme_terminal` all register through the live `bragi.plugins`
entry-point group with the slugs and display names operators see
in the admin theme picker, that each ships a resolvable
`delivery/base.html`, and that every theme's CSS carries a
`prefers-color-scheme: dark` block (the swap-path validation
that was the headline ask for #126: auto light/dark across the
whole in-tree set).
"""

from __future__ import annotations

from collections.abc import Iterator

import jinja2
import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.site import Site
from bragi.core.models.user import User

# Headline catalog. Every in-tree theme that ships under the
# `bragi.plugins` entry-point group with a `theme_*` name MUST
# appear here so the swap-path and dark-mode invariants stay
# enforced across additions. Pairs are `(slug, display_name)`.
IN_TREE_THEMES: list[tuple[str, str]] = [
    ("default", "Default"),
    ("minimal", "Minimal"),
    ("serif", "Serif"),
    ("terminal", "Terminal"),
]


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    # One unthemed Site so the loader can resolve any registered
    # theme by slug without needing a Site.theme value.
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
    )
    db_session.commit()
    yield create_delivery_app()


@pytest.mark.parametrize(("slug", "display_name"), IN_TREE_THEMES)
def test_in_tree_theme_registers_with_expected_slug_and_label(
    delivery_app: Flask, slug: str, display_name: str
) -> None:
    """Each in-tree theme appears in the live Registry under the
    slug the admin dropdown selects from and the label operators
    see."""
    registry = delivery_app.extensions["registry"]
    spec = registry.theme(slug)
    assert spec is not None, f"theme {slug!r} not registered"
    assert spec.display_name == display_name


@pytest.mark.parametrize(("slug", "_label"), IN_TREE_THEMES)
def test_in_tree_theme_resolves_delivery_base_template(
    delivery_app: Flask, slug: str, _label: str
) -> None:
    """The theme's `delivery/base.html` is reachable through the
    theme's own `PackageLoader`. If a future refactor moves the
    template path or renames the file, this test catches it
    before `ThemeAwareLoader` 500s a Site that picked the theme.
    """
    spec = delivery_app.extensions["registry"].theme(slug)
    assert spec is not None
    source, _filename, _uptodate = spec.template_loader.get_source(
        delivery_app.jinja_env, "delivery/base.html"
    )
    # Sanity: real HTML, with the blocks the content templates
    # depend on. A theme that drops the `content` block silently
    # is a broken theme.
    assert "<html" in source.lower()
    assert "{% block content %}" in source


@pytest.mark.parametrize(("slug", "_label"), IN_TREE_THEMES)
def test_in_tree_theme_ships_dark_mode_css(delivery_app: Flask, slug: str, _label: str) -> None:
    """Every in-tree theme must carry a `prefers-color-scheme:
    dark` block. The headline ask of #126 was auto light/dark,
    so a theme that ships only light-mode CSS is a regression
    against the contract operators expect from the in-tree set.
    """
    spec = delivery_app.extensions["registry"].theme(slug)
    assert spec is not None
    source, _filename, _uptodate = spec.template_loader.get_source(
        delivery_app.jinja_env, "delivery/base.html"
    )
    assert "prefers-color-scheme: dark" in source, (
        f"theme {slug!r}'s delivery/base.html is missing a " f"`prefers-color-scheme: dark` block"
    )
    assert 'name="color-scheme"' in source, (
        f"theme {slug!r}'s delivery/base.html is missing the "
        f"`<meta name=color-scheme>` hint that tells the browser "
        f"both modes are available before CSS arrives"
    )


@pytest.mark.parametrize(("slug", "_label"), IN_TREE_THEMES)
def test_in_tree_theme_uses_package_loader_not_dict_or_filesystem(
    delivery_app: Flask, slug: str, _label: str
) -> None:
    """In-tree themes must ship their templates inside the wheel,
    not via a filesystem path or in-memory dict. A theme that
    accidentally returned a `DictLoader` (e.g. a test stub
    leaking into production) would still pass the other tests
    but break under `docker run --read-only` or any container
    layout where the loader's path isn't valid.
    """
    spec = delivery_app.extensions["registry"].theme(slug)
    assert spec is not None
    assert isinstance(spec.template_loader, jinja2.PackageLoader)
