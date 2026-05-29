"""Auto-derived public navigation from the page tree.

Exposes `site_nav_tree()` as a Jinja global that themes read to
render a header nav (or a custom surface). The tree pulls every
published page with `show_in_nav=True`, capped at one level of
children, sorted by `(menu_order, title)`, and drops the page
matching `g.site.home_page_id` (because `/` already routes there
via the brand link).

Plugin boundary (see `_claude/CLAUDE.md` Conventions): imports
from `bragi.api`, `bragi.core`, `bragi.core.models` only. The
`url_for_page` global referenced by the shipped partial is
registered at runtime by `bragi.contrib.page`; the dependency is
declared at the template-globals layer, not via Python import.
"""
