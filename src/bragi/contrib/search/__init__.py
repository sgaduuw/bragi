"""Default search backend: SQLite FTS5.

Ships two contentless FTS5 virtual tables (`posts_fts`,
`pages_fts`) keyed by the corresponding content table's primary
key. Lifecycle hooks (post / page publish, update, delete) keep
the index in sync; the public `/search?q=...` route serves
paginated, per-site results with bm25 ranking and FTS5-rendered
snippets.

A future Meilisearch / Tantivy / Elasticsearch backend can drop
in via `register_search_backend` without touching the call
sites; resolution lives in `bragi.core.registry.Registry`.
"""
