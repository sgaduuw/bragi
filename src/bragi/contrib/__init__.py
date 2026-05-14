"""Built-in plugins for bragi.

Each subpackage here is a plugin registered via the `bragi.plugins`
entry-point group in `pyproject.toml`. Built-ins use the same
hookspec contract third parties do; there is no internal fast path.

Planned day-one built-ins (each lands as a separate package and
commit):
    post / page / tags     content types
    auth_github            GitHub OAuth via Authlib
    auth_local             bootstrap local password auth
    redirects              the resolve_redirect subsystem
    import_hugo            Hugo content tree importer
    import_ghost           Ghost JSON export importer
    analytics              server-side pageview / event sink
    code_highlight         Pygments transform_markdown impl
    seo                    sitemap.xml, robots.txt, security.txt
    editor_tiptap          admin editor frontend bundle

Plugin boundary rule: each `bragi/contrib/X/` may import only from
`bragi.api`, `bragi.core.models`, and `bragi.core` utilities. Never
from a sibling `bragi.contrib.*`.
"""
