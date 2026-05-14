"""Anchors plugin hook implementations."""

from __future__ import annotations

from bragi.api import hookimpl
from bragi.contrib.anchors.transform import add_heading_anchors
from bragi.core.render.transforms import TransformRegistry


@hookimpl
def register_html_transform(registry: TransformRegistry) -> None:
    """Register the heading-anchor injector.

    Priority 200 puts this AFTER the Pygments highlighter (50)
    so the structural rewrite happens on the highlighter's
    output. Headings inside a code block (rare) are still
    irrelevant because Pygments wraps code in `<div>` / `<span>`
    rather than `<h*>`.
    """
    registry.add(add_heading_anchors, name="heading-anchors", priority=200)
