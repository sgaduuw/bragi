"""Contract-regression + behaviour tests for the admin_notices hookspec.

Layered:
- This file covers the contract-regression and the dismiss/snooze routes.
- tests/unit/test_admin_notices_* cover pure logic.
- tests/integration/test_admin_notices_e2e covers full render paths.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.admin_notice_dismissal import AdminNoticeDismissal
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole
from tests.conftest import csrf_token

PASSWORD = "correct-horse-battery-staple"
EDITOR_EMAIL = "editor@example.com"


def test_admin_notice_dataclass_fields_stable() -> None:
    """AdminNotice's field set is part of the public stability contract."""
    from bragi.api import AdminNotice

    fields = {f.name for f in dataclasses.fields(AdminNotice)}
    assert fields == {
        "key",
        "severity",
        "title",
        "body",
        "cta_label",
        "cta_endpoint",
        "cta_endpoint_kwargs",
        "dismissible",
    }


def test_admin_notice_dataclass_is_frozen() -> None:
    from bragi.api import AdminNotice

    notice = AdminNotice(key="t.test", severity="info", title="Hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        notice.title = "Goodbye"  # type: ignore[misc]


def test_admin_notices_hookspec_signature_stable() -> None:
    """admin_notices accepts exactly one parameter: site.

    Goes through pluggy's introspection rather than importing from
    bragi.hookspecs directly (the latter violates the plugin-boundary
    convention that plugin authors don't import that module).
    """
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    assert list(pm.hook.admin_notices.spec.argnames) == ["site"]


def test_claims_root_route_hookspec_signature_stable() -> None:
    """claims_root_route accepts exactly one parameter: site."""
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    assert list(pm.hook.claims_root_route.spec.argnames) == ["site"]


def test_welcome_fallback_hookimpl_returns_notice_when_site_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _is_welcome_fallback returns True, the hookimpl emits the
    sites.welcome_fallback action_required notice."""
    from bragi.contrib.admin_notices import plugin as adn_plugin

    class FakeSite:
        id = 7
        home_page_id = None

    monkeypatch.setattr(
        "bragi.contrib.admin_notices.plugin._is_welcome_fallback",
        lambda site: True,
    )
    notices = adn_plugin.admin_notices(site=FakeSite())
    assert len(notices) == 1
    n = notices[0]
    assert n.key == "sites.welcome_fallback"
    assert n.severity == "action_required"
    assert n.dismissible is False
    assert n.cta_endpoint == "site_admin.edit_site"
    assert n.cta_endpoint_kwargs == {"site_id": 7}


def test_welcome_fallback_hookimpl_returns_empty_when_site_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bragi.contrib.admin_notices import plugin as adn_plugin

    class FakeSite:
        id = 8
        home_page_id = 42

    monkeypatch.setattr(
        "bragi.contrib.admin_notices.plugin._is_welcome_fallback",
        lambda site: False,
    )
    assert adn_plugin.admin_notices(site=FakeSite()) == []


# ---------------------------------------------------------------------------
# Dismiss / snooze admin routes
# ---------------------------------------------------------------------------
# Auth pattern: same as test_post_admin.py and test_site_prefixed_routes.py.
# - `patched_session_locals` rewires `bragi.core.db.SessionLocal._factory`
#   to the test engine's factory, covering all call sites at once.
# - An editor user is pre-seeded with a local credential so cookie-based
#   login works. The editor has an explicit UserSiteRole on the site.
# - CSRF is handled by submitting `_csrf_token` in the POST form data;
#   the token is fetched via `csrf_token(client)` which hits /auth/login
#   (always a live page on the admin app) to seed the session.


@pytest.fixture
def admin_app_notices(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app pre-seeded with one site and one editor user."""
    del db_session_factory  # provided to ensure db_engine is shared; unused directly

    editor = User(email=EDITOR_EMAIL, display_name="Editor", is_active=True)
    db_session.add(editor)
    db_session.flush()
    db_session.add(LocalCredential(user_id=editor.id, password_hash=hash_password(PASSWORD)))

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=editor.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))
    db_session.commit()

    yield create_admin_app()


def _login_editor(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EDITOR_EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_dismiss_route_creates_permanent_dismissal_row(
    admin_app_notices: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """POST /admin/sites/<slug>/notices/<key>/dismiss writes a row with
    dismissed_until=NULL and returns 204 No Content for htmx requests."""
    client = admin_app_notices.test_client()
    _login_editor(client)
    token = csrf_token(client)

    resp = client.post(
        "/admin/sites/blog/notices/zelda.rom_required/dismiss",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 204

    with db_session_factory() as db:
        row = db.execute(
            select(AdminNoticeDismissal).where(
                AdminNoticeDismissal.notice_key == "zelda.rom_required"
            )
        ).scalar_one()
    assert row.dismissed_until is None


def test_snooze_route_writes_future_timestamp(
    admin_app_notices: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """POST /admin/sites/<slug>/notices/<key>/snooze with hours=4 writes a
    dismissed_until timestamp in the future and returns 204 for htmx."""
    from bragi.core.time import naive_utcnow

    client = admin_app_notices.test_client()
    _login_editor(client)
    token = csrf_token(client)
    before = naive_utcnow()

    resp = client.post(
        "/admin/sites/blog/notices/zelda.rom_required/snooze",
        data={"_csrf_token": token, "hours": "4"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 204

    with db_session_factory() as db:
        row = db.execute(
            select(AdminNoticeDismissal).where(
                AdminNoticeDismissal.notice_key == "zelda.rom_required"
            )
        ).scalar_one()
    assert row.dismissed_until is not None
    # The timestamp must be strictly after the call's start time.
    assert row.dismissed_until > before


def test_snooze_rejects_invalid_hours(admin_app_notices: Flask) -> None:
    """POSTing snooze with hours=99 (not in the allowed set) returns 400."""
    client = admin_app_notices.test_client()
    _login_editor(client)
    token = csrf_token(client)

    resp = client.post(
        "/admin/sites/blog/notices/zelda.rom_required/snooze",
        data={"_csrf_token": token, "hours": "99"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 400


def test_dismiss_is_idempotent(
    admin_app_notices: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Re-dismissing the same notice updates the existing row instead of
    raising a unique-constraint violation."""
    client = admin_app_notices.test_client()
    _login_editor(client)

    for _ in range(2):
        token = csrf_token(client)
        resp = client.post(
            "/admin/sites/blog/notices/zelda.rom_required/dismiss",
            data={"_csrf_token": token},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 204

    with db_session_factory() as db:
        rows = list(
            db.execute(
                select(AdminNoticeDismissal).where(
                    AdminNoticeDismissal.notice_key == "zelda.rom_required"
                )
            ).scalars()
        )
    # Exactly one row, not two.
    assert len(rows) == 1
    assert rows[0].dismissed_until is None
