"""Site-level profile links (social / `rel="me"` section).

A per-site, owner-curated list of profile links (GitHub, Mastodon,
LinkedIn, ...) rendered in the public chrome (the default theme's
footer) as `rel="me"` + schema.org `sameAs` anchors. This is the
*identity* case: the links are the site owner's own profiles, so
`rel="me"` doubles as Mastodon profile verification.

Storage is a JSON list under `Site.extra_settings["profile_links"]`
(no table, no migration); each entry validates through the shared
`bragi.api.ProfileLink` model (label + URL). The list is edited via
a site-scoped admin page and exposed to delivery templates as the
`profile_links()` Jinja global, rendered by
`templates/delivery/_profile_links.html`.

Plugin boundary (see `_claude/CLAUDE.md` Conventions): imports from
`bragi.api`, `bragi.core`, `bragi.core.models` only. Never from a
sibling `bragi.contrib.*`.

Design: `_claude/specs/2026-06-27-profile-links-site-section-design.md`.
"""
