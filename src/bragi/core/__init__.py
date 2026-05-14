"""Shared, non-plugin code for bragi.

Layout:
    models/     SQLAlchemy declarative models (single source of truth)
    middleware/ site resolver, auth, redirects, analytics
    render/     markdown setup + transform registries
    auth/       login service, password hashing, permissions
    content/    content-type registry, publishing, slug helpers
    db.py       engine, session factory, SQLite PRAGMA setup
    htmx.py     HX-Request dispatch helpers for views
    seo.py      title / meta / canonical / og + JSON-LD helpers
"""
