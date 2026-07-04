"""404-triage plugin for bragi.

Records the public 404s the delivery app returns (minus a scanner
blocklist) into the `not_founds` table, and mounts a per-site admin
overview where the operator can act on each dead path: create a
redirect (301/302), mark it Gone (410), deep-link into new-page /
new-post with the slug pre-filled, or dismiss it.

Recording is a best-effort write on the delivery read path (same
posture as the redirect hit-bump), coalesced to one row per
(site_id, path). The blocklist keeps scanner noise out of the table
BEFORE the write; dismissed paths are marked `ignored` and neither
churn writes nor resurface.

This plugin never imports a sibling contrib plugin: it creates
redirects and content by deep-linking into their admin forms (by
endpoint name), so the contrib boundary stays intact.
"""
