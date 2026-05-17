"""Tests for theme_default's welcome-page fallback at `/`.

When neither `Site.home_page_id` nor any plugin's `resolve_home`
hookimpl claims `/`, `bragi.contrib.theme_default`'s `trylast`
impl renders a "Welcome to <site>" stub. The fallback guarantees
the home dispatcher always has a Response to return, so the
public site never 404s on `/` regardless of configuration.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, Response
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """A site with neither home_page_id nor a POST_INDEX page."""
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="naked",
        hostname="naked.example.com",
        title="Naked Site",
        canonical_url="https://naked.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.commit()
    yield create_delivery_app()


def test_welcome_stub_renders_when_nothing_claims_root(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/", headers={"Host": "naked.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Welcome to Naked Site" in body


def test_welcome_stub_is_noindex(delivery_app: Flask) -> None:
    """Crawlers shouldn't index a transient placeholder."""
    resp = delivery_app.test_client().get("/", headers={"Host": "naked.example.com"})
    body = resp.data.decode()
    assert 'name="robots"' in body
    assert "noindex" in body


def test_welcome_stub_uses_no_store_cache(delivery_app: Flask) -> None:
    """The misconfigured state evaporates the instant the admin sets home."""
    resp = delivery_app.test_client().get("/", headers={"Host": "naked.example.com"})
    assert resp.headers.get("Cache-Control") == "no-store"


def test_welcome_stub_is_trylast() -> None:
    """The theme_default impl runs last so other impls preempt it."""
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    impls = {impl.plugin.__name__: impl for impl in pm.hook.resolve_home.get_hookimpls()}
    assert "bragi.contrib.theme_default.plugin" in impls
    assert impls["bragi.contrib.theme_default.plugin"].trylast is True


def test_higher_priority_impl_preempts_welcome_stub() -> None:
    """A plugin registered at default priority wins over the welcome stub.

    Smoke for the "third-party theme can replace welcome" extension
    path: anything that's not trylast claims `/` first.
    """
    import pluggy

    from bragi.api import hookimpl
    from bragi.plugins import create_plugin_manager

    class CustomWelcome:
        @hookimpl
        def resolve_home(self, site: object) -> Response:
            return Response("custom welcome", mimetype="text/html")

    pm = create_plugin_manager()
    pm.register(CustomWelcome(), name="custom-welcome-test")
    try:
        # firstresult=True: the highest-priority impl returning non-None wins.
        result = pm.hook.resolve_home(site=object())
    finally:
        pm.unregister(name="custom-welcome-test")
    assert isinstance(result, Response)
    assert result.get_data(as_text=True) == "custom welcome"
    # Bind unused pluggy import to silence ruff F401.
    _ = pluggy.HookspecMarker
