"""Content-type registry, publishing logic, slug helpers.

The `ContentTypeRegistry` collects every `ContentTypeSpec`
registered by plugins. Views resolve a path to a spec and call
its `render`. Slug-change auto-redirect insertion is wired here
on the publishing side.
"""
