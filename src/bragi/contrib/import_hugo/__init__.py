"""Hugo importer plugin for bragi.

Hugo content is a tree of markdown files with TOML / YAML / JSON
frontmatter under `content/`. The importer walks that tree,
parses each file's frontmatter, and creates Post rows. Every
entry in a post's `aliases:` list becomes a 301 Redirect row,
keeping legacy URLs alive after the migration.

The original markdown body is the source of truth for bragi too,
so the conversion is straight passthrough (no markdownify
required).
"""
