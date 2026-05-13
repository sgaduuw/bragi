"""Post content type plugin.

Registers Post as a content type via `register_content_type`. The
post plugin will eventually own:
- The `ContentTypeSpec` for Post (lands here)
- An admin Blueprint for list / edit / delete (next commit)
- Public delivery views and templates (subsequent commits)

The Post SQLAlchemy model itself lives in `bragi.core.models.post`
so alembic autogenerate has a single source of truth for schema.
"""
