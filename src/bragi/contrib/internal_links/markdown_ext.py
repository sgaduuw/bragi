"""Markdown-it link-renderer override for typed-prefix internal links.

Author syntax:

    [text](post:my-slug)   # resolve by current slug at save time
    [text](post:42)        # resolve by id at save time
    [text](page:about)     # any registered ContentTypeSpec with
                           # `internal_link_prefix` opts in.

At save time the override rewrites the `<a href=...>` open tag
into the persisted shape:

    <a href="/posts/my-slug/" data-bragi-link="post:42">

The `href` is the current slug-path; `data-bragi-link` carries the
stable typed id. The delivery-time Jinja filter (delivery.py)
keeps the `href` in sync if the target's slug changes after save.

Unresolved keys (typo, deleted target, or outside an app/request
context) emit a "broken link" shape with no `href` and an extra
class so themes can style it:

    <a class="bragi-link--broken" data-bragi-link="post:my-slug">

Plain external links (`https://...`, `mailto:...`, `#anchor`,
relative paths) are left untouched: the override only fires when
the href matches a registered `<prefix>:<key>` shape.
"""

from __future__ import annotations

from html import escape
from typing import Any

from flask import current_app, g, has_app_context
from markdown_it import MarkdownIt

__all__ = ["configure_internal_links"]


def _resolve(prefix: str, key: str) -> tuple[int | None, str | None]:
    """Look up `prefix:key` in the active app's registry, scoped to
    the current site. Returns `(entity_id, href)` on a hit, or
    `(None, None)` when the prefix isn't registered, the resolver
    returns None, or context (app / site) is missing.

    A missing context returns `(None, None)` rather than raising:
    the typical save path always has both, but tests that exercise
    `render_markdown` outside a request context shouldn't crash.
    """
    if not prefix or not key:
        return None, None
    if not has_app_context():
        return None, None
    registry = current_app.extensions.get("registry")
    if registry is None:
        return None, None
    spec = registry.content_type_for_link_prefix(prefix)
    if spec is None or spec.resolve_internal_link is None:
        return None, None
    site = g.get("site") if g else None
    if site is None or not getattr(site, "id", None):
        return None, None
    resolution = spec.resolve_internal_link(key, int(site.id))
    if resolution is None:
        return None, None
    return resolution.entity_id, resolution.href


def _emit_resolved_open(prefix: str, entity_id: int, href: str) -> str:
    return f'<a href="{escape(href)}" data-bragi-link="{escape(f"{prefix}:{entity_id}")}">'


def _emit_broken_open(prefix: str, key: str) -> str:
    return f'<a class="bragi-link--broken" data-bragi-link="{escape(f"{prefix}:{key}")}">'


def _is_internal_prefix_href(href: str) -> tuple[str, str] | None:
    """A `prefix:key` shape qualifies only when the prefix matches a
    registered content-type. This guards against false positives
    like `mailto:`, `tel:`, `javascript:`, `http:` slipping into
    the override path.

    Returns `(prefix, key)` if the registry has a resolver for the
    prefix, else None. Done as a registry probe (not a hardcoded
    allow-list) so third-party content types automatically light
    up without touching this module.
    """
    if ":" not in href:
        return None
    prefix, _, key = href.partition(":")
    if not prefix or not key:
        return None
    if not has_app_context():
        return None
    registry = current_app.extensions.get("registry")
    if registry is None:
        return None
    spec = registry.content_type_for_link_prefix(prefix)
    if spec is None:
        return None
    return prefix, key


def configure_internal_links(md: MarkdownIt) -> None:
    """Install the link_open renderer override on `md` in place.

    `register_markdown_extension` returns this callable; the app
    factory invokes it once per process during boot. The override
    only modifies the anchor open tag; the link text and closing
    tag (`link_close`) come through the default path unchanged.

    Non-internal links (anything whose href prefix isn't a
    registered content-type prefix) fall through to
    `renderer.renderToken`, the same path markdown-it-py uses
    when no rule is installed. That preserves any default
    behaviour (title attribute, future link options) without us
    having to reconstruct it.
    """

    def _link_open(tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        token = tokens[idx]
        href = token.attrGet("href") or ""
        detected = _is_internal_prefix_href(href)
        if detected is None:
            # markdown-it-py's narrowest renderer Protocol doesn't
            # advertise `renderToken`, but every concrete renderer
            # bragi instantiates carries it (it's how markdown-it
            # falls back when a rule is None). `env` is forwarded
            # so any other plugin keeping per-render state (e.g. the
            # embeds-budget counter) keeps seeing it.
            return md.renderer.renderToken(tokens, idx, options, env)  # type: ignore[attr-defined,no-any-return]
        prefix, key = detected
        entity_id, resolved_href = _resolve(prefix, key)
        if entity_id is None or resolved_href is None:
            return _emit_broken_open(prefix, key)
        return _emit_resolved_open(prefix, entity_id, resolved_href)

    md.renderer.rules["link_open"] = _link_open  # type: ignore[attr-defined]
