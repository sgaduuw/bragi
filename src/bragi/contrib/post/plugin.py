"""Post plugin hook implementations.

The Post type's admin / delivery surfaces land in follow-up
commits; this module is the registration point.
"""

from __future__ import annotations

from typing import Any

from bragi.api import ContentTypeSpec, FieldSpec, hookimpl
from bragi.core.models.post import Post

POST_EDIT_FIELDS: list[FieldSpec] = [
    FieldSpec(name="title", label="Title", field_type="text", required=True),
    FieldSpec(name="slug", label="Slug", field_type="text", required=True),
    FieldSpec(name="subtitle", label="Subtitle", field_type="text"),
    FieldSpec(name="body_markdown", label="Body", field_type="markdown"),
    FieldSpec(name="status", label="Status", field_type="text"),
    FieldSpec(name="published_at", label="Published at", field_type="datetime"),
    FieldSpec(name="meta_title", label="Meta title", field_type="text"),
    FieldSpec(name="meta_description", label="Meta description", field_type="text"),
    FieldSpec(name="noindex", label="No-index", field_type="text"),
]


def _url_for_post(post: Any) -> str:
    """Canonical public URL for a Post. Site context is implicit in
    the delivery request (request.site)."""
    return f"/posts/{post.slug}/"


def _render_post(post: Any, _request: Any) -> str:
    """Stub render until templates and the renderer pipeline land.

    Returns minimal HTML so the delivery side can demonstrate the
    plugin lookup works. Real rendering will go through the
    `register_html_transform` pipeline and a Jinja template.
    """
    return f"<h1>{post.title}</h1>\n{post.body_html}"


@hookimpl
def register_content_type() -> ContentTypeSpec:
    """Register Post as a content type."""
    return ContentTypeSpec(
        name="post",
        label="Post",
        label_plural="Posts",
        model=Post,
        url_for=_url_for_post,
        render=_render_post,
        admin_list_columns=["title", "status", "published_at"],
        admin_edit_fields=POST_EDIT_FIELDS,
        json_ld_type="BlogPosting",
        feed_eligible=True,
        sitemap_eligible=True,
    )
