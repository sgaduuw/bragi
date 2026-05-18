"""Post-side delivery shim.

Posts no longer own URL routes; the page plugin's dispatcher in
`bragi.contrib.page.delivery` resolves `<post_index_url>/<post-slug>/`
and renders via its `render_post` helper. The post Blueprint
defined here exists for two reasons:

1. Its `template_folder` exposes `templates/delivery/post.html`
   to the app-wide Jinja loader chain (Flask scans every
   registered Blueprint's template folders). Without it the
   page plugin's dispatcher would fail to render individual
   posts. The Blueprint registers zero routes.
2. The `SessionLocal` re-export below preserves existing test
   fixtures that `monkeypatch.setattr("bragi.contrib.post
   .delivery.SessionLocal", ...)`.
"""

from __future__ import annotations

from flask import Blueprint

from bragi.core.db import SessionLocal

bp = Blueprint(
    "post_templates",
    __name__,
    template_folder="templates",
)


__all__ = ["SessionLocal", "bp"]
