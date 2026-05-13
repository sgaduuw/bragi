"""Request middleware wired by the app factories.

Stack order (delivery app):
    1. site_resolver   Host header to Site row (cached)
    2. auth            session lookup, CSRF
    3. redirects       resolve_redirect chain + table
    4. analytics       async event sink

Admin app uses the same site_resolver + auth, no redirects (admin
URLs are static), and the analytics sink runs admin-side too with
a different event_type.

Module files land here as the corresponding features ship.
"""
