"""Delivery-time href rewriter for internal links.

Registered as a Jinja filter (`internal_link_rewrite`) so the
post / page delivery templates can pipe `body_html` through it
on every public render:

    {{ post.body_html | internal_link_rewrite | safe }}

For each `<a data-bragi-link="prefix:id" ...>` in the body the
filter:

1. Asks `registry.content_type_for_link_prefix(prefix).resolve_internal_link(id, site_id)`
   for the current canonical href.
2. If the resolver returns a fresh href, rewrites the anchor's
   `href` to it. If the resolver returns None (target deleted),
   strips `href` and stamps the `bragi-link--broken` class.
3. If the persisted anchor was already a broken shape (no href,
   class set) and the resolver now finds the target (e.g. a
   draft was published, a typoed slug was renamed back to the
   key the author wrote), un-marks it: adds the `href` and
   removes the broken class.

Idempotent: a second pass over filter output is a no-op (modulo
attribute order, which is irrelevant to browsers).

The filter is HTML-shape coupled (`<a ...>` open tags with a
`data-bragi-link` attribute) and uses regex over the body. That
matches the established pattern in bragi.contrib.highlight,
bragi.contrib.anchors, and bragi.contrib.embeds.rerender; the
shapes are entirely under our control because the markdown
extension produces them.
"""

from __future__ import annotations

import re
from html import escape
from typing import TYPE_CHECKING

from flask import current_app, g, has_app_context
from markupsafe import Markup

if TYPE_CHECKING:
    from bragi.api import InternalLinkResolution

# Match an `<a ... data-bragi-link="..." ...>` open tag. We require
# the `data-bragi-link` attribute to be present so plain anchors
# never enter the rewrite path.
_INTERNAL_A_RE = re.compile(
    r'<a\b(?P<attrs>[^>]*?data-bragi-link="[^"]+"[^>]*)>',
    re.IGNORECASE,
)

# Attribute extractor: simple `name="value"` form. Bragi's emitter
# always uses double-quoted attributes; if a future extension
# emits single-quoted or unquoted, expand this. Until then, KISS.
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

_BROKEN_CLASS = "bragi-link--broken"


def _parse_attrs(attrs_str: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in _ATTR_RE.finditer(attrs_str)}


def _serialise_attrs(attrs: dict[str, str]) -> str:
    """Re-emit attributes in a stable, deterministic order so the
    filter is idempotent across runs (attribute order is irrelevant
    to browsers, but tests want it stable)."""
    parts = []
    for key in sorted(attrs):
        parts.append(f'{key}="{escape(attrs[key])}"')
    return " ".join(parts)


def _resolve(prefix: str, key: str, site_id: int) -> InternalLinkResolution | None:
    registry = current_app.extensions.get("registry")
    if registry is None:
        return None
    spec = registry.content_type_for_link_prefix(prefix)
    if spec is None or spec.resolve_internal_link is None:
        return None
    result = spec.resolve_internal_link(key, site_id)
    return result  # type: ignore[no-any-return]


def _rewrite_anchor(match: re.Match[str], site_id: int) -> str:
    attrs = _parse_attrs(match.group("attrs"))
    marker = attrs.get("data-bragi-link", "")
    if ":" not in marker:
        # Malformed marker; leave the tag verbatim.
        return match.group(0)

    prefix, _, key = marker.partition(":")
    resolution = _resolve(prefix, key, site_id)

    classes = {c for c in attrs.get("class", "").split() if c}

    if resolution is None:
        # Target no longer resolves: keep the marker, drop href if
        # any, stamp broken class.
        attrs.pop("href", None)
        classes.add(_BROKEN_CLASS)
    else:
        attrs["href"] = resolution.href
        # Persist the canonical `prefix:<id>` form so a key that
        # was an at-save-time slug (`post:my-slug`) hardens into
        # the id form (`post:42`) after the first delivery pass.
        # Pure idempotency win on subsequent renders.
        attrs["data-bragi-link"] = f"{prefix}:{resolution.entity_id}"
        classes.discard(_BROKEN_CLASS)

    if classes:
        attrs["class"] = " ".join(sorted(classes))
    else:
        attrs.pop("class", None)

    return "<a " + _serialise_attrs(attrs) + ">"


def internal_link_rewrite(html: str) -> Markup:
    """Jinja filter: rewrite `data-bragi-link` anchors against the
    current state of the database for the active site.

    No-op fast paths:
    - Empty body
    - No `data-bragi-link=` substring (cheap check before regex)
    - Outside a request context (no site to scope against)
    - No resolved site on `g.site` (e.g. a 404 chain with no
      matched Host)

    Returns Markup so the caller's `| safe` is unnecessary, but
    the established convention in the post/page templates is to
    chain `| safe` after every body_html filter; leave that
    chain alone so future filters compose without surprises.
    """
    if not html or "data-bragi-link=" not in html:
        return Markup(html or "")
    if not has_app_context():
        return Markup(html)
    site = g.get("site") if g else None
    if site is None or not getattr(site, "id", None):
        return Markup(html)
    site_id = int(site.id)
    rewritten = _INTERNAL_A_RE.sub(lambda m: _rewrite_anchor(m, site_id), html)
    return Markup(rewritten)
