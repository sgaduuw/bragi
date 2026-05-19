"""bragi.contrib.theme_minimal: a lean, content-first theme.

Sibling to `theme_default`: same hookspec surface
(`register_theme`), same template layout
(`templates/delivery/base.html`), inlined CSS with automatic
light / dark mode via `prefers-color-scheme`. The shape is
"system fonts, narrow column, no chrome" so a content-only site
feels like reading a markdown file. No `static_dir`; ship is
container-only and CSS lives inline so admins don't need to
mount an extra static path through their reverse proxy to pick
the theme up.
"""
