"""Page content type: nested static content with parent_id links.

Companion to `bragi.contrib.post`. Pages are addressed by their
full slash-joined slug path (`/about/team/`) rather than a flat
`/posts/<slug>/`, so the delivery side mounts a catch-all route
that walks the tree.
"""
