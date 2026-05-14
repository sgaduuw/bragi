"""Ghost importer plugin for bragi.

Ghost exports content as a single JSON file under
`db[0].data.posts` (plus `users`, `tags`, `posts_tags` linkages).
Bodies arrive as HTML (rendered from Ghost's internal lexical /
mobiledoc representation); the importer converts that to markdown
via `markdownify` so bragi's markdown-source-of-truth invariant
holds.

Idempotency: Ghost post ids land in `Post.source_id`. Re-running
the importer updates rows in place rather than duplicating.
"""
