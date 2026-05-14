"""Tests for `bragi.contrib.post`.

Plugin-only smoke: load the PluginManager, confirm the Post
ContentTypeSpec is registered, and exercise its lightweight
attributes. DB-backed and view-level tests land with the admin
Blueprint commit.
"""

from __future__ import annotations

from bragi.api import ContentTypeSpec
from bragi.core.models.post import Post
from bragi.plugins import create_plugin_manager


def _post_spec() -> ContentTypeSpec:
    pm = create_plugin_manager()
    specs = pm.hook.register_content_type()
    spec = next((s for s in specs if s.name == "post"), None)
    assert spec is not None, "post content type not registered"
    return spec


def test_post_plugin_registers_content_type() -> None:
    """The post plugin registers a ContentTypeSpec for Post."""
    spec = _post_spec()
    assert isinstance(spec, ContentTypeSpec)
    assert spec.label == "Post"
    assert spec.label_plural == "Posts"
    assert spec.model is Post
    assert spec.json_ld_type == "BlogPosting"
    assert spec.feed_eligible is True
    assert spec.sitemap_eligible is True


def test_post_url_for_returns_canonical_path() -> None:
    """`url_for` produces the conventional `/posts/<slug>/` path."""
    spec = _post_spec()

    class StubPost:
        slug = "hello-world"

    assert spec.url_for(StubPost()) == "/posts/hello-world/"


def test_post_edit_fields_cover_authoring_basics() -> None:
    """`admin_edit_fields` includes the fields needed to author a post."""
    spec = _post_spec()
    field_names = {f.name for f in spec.admin_edit_fields}
    assert {"title", "slug", "body_markdown", "status"} <= field_names
