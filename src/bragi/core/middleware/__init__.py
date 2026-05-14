"""Request middleware wired by the app factories.

Stack order (delivery app):
    1. site_resolver   Host header -> Site row, attached to g.site
    2. auth            session lookup, CSRF              (lands later)
    3. redirects       resolve_redirect hook + table     (404 handler)
    4. analytics       async event sink                  (lands later)

Admin app uses the same site_resolver and (when auth_local ships)
the auth guard. Admin does not install the redirect handler since
admin URLs are statically defined.
"""
