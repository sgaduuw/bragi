"""bragi.contrib.webmentions (#147): indieweb mention protocol.

Two surfaces:

- **Inbox** (`POST /webmentions` on the delivery app): accepts
  source + target, validates the source actually links to the
  target (per W3C Webmention §3.2.1), parses an h-card subset
  for author display, persists pending and awaits admin
  approval.
- **Outbox** (`on_post_published` queues; `bragi webmentions send-pending`
  ships): scans the rendered post HTML for external links,
  performs endpoint discovery on each target, and POSTs the
  mention. Idempotent: re-running send-pending is a no-op on
  already-sent rows.

Discovery (so external senders can find our inbox):
`<link rel="webmention" href="https://example.com/webmentions">`
injected into the delivery `<head>` via a template global.

The full microformats2 spec is OUT of v1; we use a small h-card-
only regex extractor for the author byline. Pulling `mf2py` (or
parsing the entire mf2 surface) lands when there's a concrete
mention with structured content we want to display richly.
"""
