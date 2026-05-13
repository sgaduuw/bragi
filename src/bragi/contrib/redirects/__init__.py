"""Redirects plugin.

Owns the redirects subsystem at the application layer: the
canonical `resolve_redirect` hookimpl (table lookup) and, in
follow-up commits, the admin Blueprint, hit-count tracking,
broken-target nightly job, and the `on_post_updated` hookimpl
that auto-inserts a 301 when a Post's slug changes.

The `Redirect` SQLAlchemy model itself lives in
`bragi.core.models.redirect` so alembic autogenerate has a single
source of truth for schema.

Currently supported match types: `exact`. `prefix` and `regex`
land here in follow-up commits.
"""
