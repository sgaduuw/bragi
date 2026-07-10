"""Profile links (social / `rel="me"` section) in the public footer.

Renders the site owner's account-profile links (GitHub, Mastodon,
LinkedIn, ...) in the public chrome (the default theme's footer) as
`rel="me"` + schema.org `sameAs` anchors. This is the *identity*
case: the links are the owner's own profiles, so `rel="me"` doubles
as Mastodon profile verification.

The links live on the owner `User` (`User.profile_links`) and are
edited on the account Profile page (`bragi.contrib.account_profile`);
this plugin is delivery-only and just renders them via the
`profile_links()` Jinja global + `templates/delivery/_profile_links.html`.
Consolidating rel=me onto the per-user profile means every site the
owner publishes shows the same verified links.

Plugin boundary (see `_claude/CLAUDE.md` Conventions): imports from
`bragi.api`, `bragi.core`, `bragi.core.models` only. Never from a
sibling `bragi.contrib.*`.

Design: `_claude/specs/2026-07-10-profiles-design.md` (phase 3).
"""
