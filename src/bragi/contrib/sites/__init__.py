"""Sites plugin.

Provides admin tooling for the core `Site` model. A `Site` row
is mandatory before any content can be authored (the post admin's
new-post view picks the first Site), so seeding one is the very
first step on any fresh install.

The `Site` SQLAlchemy model itself lives in
`bragi.core.models.site`; this plugin only contributes a CLI
group (`cms site ...`) for creating and listing rows.
"""
