"""Internal links resolved at delivery time (#117).

Lets authors write `[text](post:my-slug)` or `[text](post:42)` and
have those links follow a target's slug rename without re-editing
or re-rendering the source body.

Two halves:

- A markdown-it link-renderer override (`markdown_ext.py`) catches
  `[text](<prefix>:<key>)` shapes at parse time, resolves the key
  via the matching `ContentTypeSpec.resolve_internal_link`, and
  emits the persisted anchor shape
  `<a href="<slug-at-save>" data-bragi-link="<prefix>:<id>">`.
- A Jinja filter (`delivery.py`) the post / page templates pipe
  `body_html` through at render time. The filter scans for
  `data-bragi-link` attributes, batch-resolves IDs to current
  slugs via the same resolvers, and rewrites the `href` if the
  target's slug has changed since save.

Same-site only by construction; the delivery filter pulls
`g.site.id` from the active request. Cross-site resolution is a
deferred v2 concern.

The HTTP cache on a source post is NOT invalidated by a target
slug rename. The slug-change auto-301 the redirects plugin
already inserts catches CDN-cached stale renders until the
Cache-Control window expires; no fanout or backlinks table is
required for correctness.
"""
