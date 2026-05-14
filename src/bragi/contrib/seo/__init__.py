"""SEO endpoints plugin.

Owns three small public endpoints on the delivery app:

- `/sitemap.xml`: enumerates published posts of the resolved
  site, filtered by `ContentTypeSpec.sitemap_eligible`.
- `/robots.txt`: per-site default policy (allow-all) plus a
  `Sitemap:` line. A per-site override knob is a follow-up.
- `/.well-known/security.txt`: RFC 9116 contact + expiry. Only
  served when `Settings.security_contact` is set; otherwise the
  endpoint returns 404 (no fake values).

RSS/Atom feeds live alongside these (`/feed.xml`); see
`bragi.contrib.seo.feed` for that route.
"""
