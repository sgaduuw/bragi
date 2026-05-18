"""Tests for `bragi.core.registry` (#188).

The headline behaviour: each `add_*` method on `Registry` raises
`DuplicateRegistration` when a second spec claims the same
unique key. Pre-#188 the bare `.append()` silently shadowed the
later spec, so a third-party plugin reusing a built-in name
(`name="post"`, `slug="default"`, etc.) failed open with the
second registration dead and no signal.
"""

from __future__ import annotations

import jinja2
import pytest

from bragi.api import (
    AuthMethodSpec,
    ContentTypeSpec,
    FieldSpec,
    ImageMetadata,
    ImageProcessorSpec,
    ImporterSpec,
    NavItem,
    OAuthProviderSpec,
    SearchBackendSpec,
    StorageBackendSpec,
    ThemeSpec,
)
from bragi.core.registry import DuplicateRegistration, Registry


def _content_type_spec(name: str) -> ContentTypeSpec:
    return ContentTypeSpec(
        name=name,
        label=name.title(),
        label_plural=name.title() + "s",
        model=object,
        url_for=lambda _: None,
        render=lambda _r, _s: "",
        admin_list_columns=[],
        admin_edit_fields=[FieldSpec(name="x", label="X", field_type="text")],
    )


def _importer_spec(name: str) -> ImporterSpec:
    from bragi.api import ImportPlan, ImportResult

    return ImporterSpec(
        name=name,
        description="t",
        detect=lambda _p: False,
        plan=lambda _p: ImportPlan(counts={}, warnings=[]),
        apply=lambda _p, _s, _o: ImportResult(counts={}, warnings=[]),
    )


def _oauth_spec(name: str) -> OAuthProviderSpec:
    return OAuthProviderSpec(
        name=name,
        label=name.title(),
        authlib_client_factory=lambda: object(),
        fetch_user_info=lambda _t: None,  # type: ignore[arg-type,return-value]
    )


def _auth_method(name: str) -> AuthMethodSpec:
    return AuthMethodSpec(name=name, label=name.title(), login_view=lambda: "")


def _storage_spec(name: str) -> StorageBackendSpec:
    return StorageBackendSpec(
        name=name,
        store=lambda _s, _d: ("k", 0),
        read=lambda _s, _k: b"",
        remove=lambda _s, _k: None,
    )


def _image_spec(name: str) -> ImageProcessorSpec:
    return ImageProcessorSpec(
        name=name,
        can_process=lambda _ct: True,
        probe=lambda _b: ImageMetadata(width=1, height=1),
    )


def _search_spec(name: str) -> SearchBackendSpec:
    return SearchBackendSpec(
        name=name,
        index=lambda _scope, _eid, _f: None,
        remove=lambda _scope, _eid: None,
        search=lambda _sid, _q, _p, _ps: None,  # type: ignore[arg-type,return-value]
        reindex_all=lambda _sid: {},
    )


def _theme(slug: str) -> ThemeSpec:
    return ThemeSpec(slug=slug, display_name=slug.title(), template_loader=jinja2.DictLoader({}))


# ============================================================
# Dedup: each surface raises on key collision
# ============================================================


def test_add_content_type_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_content_type(_content_type_spec("post"))
    with pytest.raises(DuplicateRegistration) as exc:
        r.add_content_type(_content_type_spec("post"))
    assert exc.value.kind == "content_type"
    assert exc.value.key == "post"
    # The first registration wins (second is rejected outright).
    assert len(r.content_types) == 1


def test_add_importer_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_importer(_importer_spec("hugo"))
    with pytest.raises(DuplicateRegistration, match="importer"):
        r.add_importer(_importer_spec("hugo"))
    assert len(r.importers) == 1


def test_add_oauth_provider_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_oauth_provider(_oauth_spec("github"))
    with pytest.raises(DuplicateRegistration, match="oauth_provider"):
        r.add_oauth_provider(_oauth_spec("github"))


def test_add_auth_method_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_auth_method(_auth_method("local"))
    with pytest.raises(DuplicateRegistration, match="auth_method"):
        r.add_auth_method(_auth_method("local"))


def test_add_storage_backend_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_storage_backend(_storage_spec("local"))
    with pytest.raises(DuplicateRegistration, match="storage_backend"):
        r.add_storage_backend(_storage_spec("local"))


def test_add_image_processor_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_image_processor(_image_spec("pillow"))
    with pytest.raises(DuplicateRegistration, match="image_processor"):
        r.add_image_processor(_image_spec("pillow"))


def test_add_search_backend_raises_on_duplicate_name() -> None:
    r = Registry()
    r.add_search_backend(_search_spec("sqlite-fts5"))
    with pytest.raises(DuplicateRegistration, match="search_backend"):
        r.add_search_backend(_search_spec("sqlite-fts5"))


def test_add_search_backend_none_is_skipped() -> None:
    """A plugin returning None means "doesn't apply in this deployment"
    (e.g. the SQLite FTS5 backend under a Postgres URL). The dedup
    check must not break the None-as-skip contract."""
    r = Registry()
    r.add_search_backend(None)
    r.add_search_backend(None)  # second None doesn't raise either
    assert r.search_backends == []


def test_add_theme_raises_on_duplicate_slug() -> None:
    r = Registry()
    r.add_theme(_theme("default"))
    with pytest.raises(DuplicateRegistration) as exc:
        r.add_theme(_theme("default"))
    assert exc.value.kind == "theme"
    assert exc.value.key == "default"


# ============================================================
# Dedup: admin_nav uses `.endpoint` and checks intra-batch
# ============================================================


def test_add_admin_nav_raises_on_duplicate_endpoint() -> None:
    r = Registry()
    r.add_admin_nav([NavItem(label="Posts", endpoint="post.list")])
    with pytest.raises(DuplicateRegistration) as exc:
        r.add_admin_nav([NavItem(label="Articles", endpoint="post.list")])
    # Different label, same endpoint = duplicate nav target.
    assert exc.value.kind == "admin_nav"
    assert exc.value.key == "post.list"


def test_add_admin_nav_raises_on_intra_batch_duplicate() -> None:
    """A single hookimpl returning [a, a] should be caught, not
    leak the second copy into the nav."""
    r = Registry()
    item = NavItem(label="Posts", endpoint="post.list")
    with pytest.raises(DuplicateRegistration):
        r.add_admin_nav([item, item])
    # First item went in, second raised; partial state is documented.
    assert [n.endpoint for n in r.admin_nav] == ["post.list"]


def test_add_admin_nav_distinct_endpoints_accumulate() -> None:
    r = Registry()
    r.add_admin_nav([NavItem(label="Posts", endpoint="post.list")])
    r.add_admin_nav([NavItem(label="Pages", endpoint="page.list")])
    assert {n.endpoint for n in r.admin_nav} == {"post.list", "page.list"}


# ============================================================
# Non-collision sanity: distinct keys still accumulate cleanly
# ============================================================


def test_distinct_keys_across_surfaces_accumulate() -> None:
    r = Registry()
    r.add_content_type(_content_type_spec("post"))
    r.add_content_type(_content_type_spec("page"))
    r.add_importer(_importer_spec("hugo"))
    r.add_importer(_importer_spec("ghost"))
    r.add_theme(_theme("default"))
    r.add_theme(_theme("minimal"))
    assert {s.name for s in r.content_types} == {"post", "page"}
    assert {s.name for s in r.importers} == {"hugo", "ghost"}
    assert {s.slug for s in r.themes} == {"default", "minimal"}


def test_duplicate_registration_message_quotes_kind_and_key() -> None:
    r = Registry()
    r.add_theme(_theme("default"))
    with pytest.raises(DuplicateRegistration) as exc:
        r.add_theme(_theme("default"))
    msg = str(exc.value)
    assert "theme" in msg
    assert "'default'" in msg
    # The error nudges operators toward the plugins-list CLI for
    # triage (per #190); the link is in the message body.
    assert "cms plugins list" in msg
