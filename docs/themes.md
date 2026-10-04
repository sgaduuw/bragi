# Building a theme

[Back to README](../README.md)

A theme is a plain Python package that registers a `ThemeSpec` via
the `register_theme` hook on the `bragi.plugins` entry-point group.
Same surface the in-tree `theme_default` / `theme_minimal` /
`theme_serif` / `theme_terminal` use; nothing internal-only.

**Distribution name.** Follow the `bragi-theme-<slug>` convention
(e.g. `bragi-theme-coral`). It keeps third-party packages
greppable on PyPI and signals theme-package shape without further
inspection. The Python import name is independent (`coral_theme`,
`bragi_theme_coral`, whatever you like); only the distribution
name follows the convention.

**Package layout.**

```
bragi-theme-coral/
├── pyproject.toml
├── README.md
└── coral_theme/
    ├── __init__.py
    ├── plugin.py
    ├── templates/
    │   └── delivery/
    │       └── base.html
    └── static/                # optional
        └── theme.css
```

**`plugin.py` (the whole file).**

```python
from __future__ import annotations

from pathlib import Path

import jinja2

from bragi.api import ThemeSpec, hookimpl


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="coral",
        display_name="Coral",
        template_loader=jinja2.PackageLoader("coral_theme", "templates"),
        # Omit static_dir only if your theme has no static assets.
        static_dir=Path(__file__).parent / "static",
    )
```

**`pyproject.toml` entry-point declaration.**

```toml
[project.entry-points."bragi.plugins"]
coral_theme = "coral_theme.plugin"
```

The entry-point name (`coral_theme` above) must be unique across
every plugin installed in the deployment; bragi's runtime fails
loud on collision (#188). Pick a name that includes your slug so
the `bragi plugins list` output (#190) reads naturally.

**Required template: `delivery/base.html`.** Bragi resolves
`delivery/base.html` against your theme first (via
`ThemeAwareLoader`) for every Site that selected your slug. Your
template must preserve the block surface every content-type
template extends:

| Block | Purpose |
|---|---|
| `title` | `<title>` content |
| `meta` | description / canonical / robots meta tags |
| `feed_links` | Atom `<link rel="alternate">` |
| `social_meta` | Open Graph + Twitter Card meta (content templates override) |
| `jsonld` | JSON-LD `<script>` (content templates override) |
| `content` | the page body |

Plus the Jinja globals plugins register: `pygments_css_url`,
`webmention_endpoint_url`, etc. Easiest path: copy
`bragi.contrib.theme_default`'s `delivery/base.html` as your
starting scaffold and restyle from there.

**Optional templates: anything under `delivery/`.** A theme that
ships `delivery/post.html` shadows the post plugin's
default, etc. Override only the templates you actually want to
change; the rest fall through to the plugin's own
`templates/delivery/`. This includes `delivery/error.html` (the
404 / 410 / 500 page): the core default extends your
`delivery/base.html`, so error pages are branded out of the box;
ship your own only to change the error markup itself.

**Static assets.** If `static_dir` is set, the delivery app
serves your files at `/theme/<slug>/static/<path>`. Reference
them from your templates with that URL:

```html
<link rel="stylesheet" href="/theme/coral/static/theme.css">
```

The path is reserved; `bragi.contrib.themes` owns the
blueprint that serves it.

**Automatic light / dark.** The in-tree themes all use the
`@media (prefers-color-scheme: dark)` pattern with CSS custom
properties. Recommended:

```html
<meta name="color-scheme" content="light dark">
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --fg: #222222;
    /* ... */
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d0d0d;
      --fg: #f3f4f6;
      /* ... */
    }
  }
</style>
```

Choose a palette for your theme. The built-in themes keep these rules
in their external `static/theme.css` files.

**Installing.** Install your package into the same Python
environment as bragi (the `admin` and `delivery` containers, or
`uv add` in a dev tree):

```sh
pip install bragi-theme-coral
```

Restart both apps; the entry-point group is read at process
boot. Once installed, your slug appears in the admin theme
picker on the site-edit form, and `bragi plugins list` reports
your distribution name + version under "origin".

**Activating.** Per-Site selection via the admin site-edit
form, or set `Site.theme = "coral"` in the DB. NULL means "use
the bundled default theme"; an unknown slug falls back to
default with a logged warning rather than 500ing the page.

**Disabling a bundled theme.** Remove its entry under
`[project.entry-points."bragi.plugins"]` in `pyproject.toml`, then build
and install that modified Bragi package in every app and task environment.
Restart the processes to reload plugin registration. This applies to any
bundled plugin.

The production Dockerfiles install a published `bragi-cms` wheel from PyPI.
Rebuilding them after editing local source does not include your changes.
For a customized deployment, adapt the image build to install your modified
wheel instead, and use those images for admin, delivery, and background tasks.
