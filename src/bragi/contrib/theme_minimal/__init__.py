"""bragi.contrib.theme_minimal: a lean, content-first theme.

Sibling to `theme_default`: same hookspec surface
(`register_theme`), same template layout
(`templates/delivery/base.html`), automatic light / dark mode
via `prefers-color-scheme`. The shape is "system fonts, narrow
column, no chrome" so a content-only site feels like reading a
markdown file. Ships `static/resume.css` via `static_dir` for
resume-page styling.
"""
