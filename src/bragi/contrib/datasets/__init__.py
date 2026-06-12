"""Datasets plugin: DuckDB-backed dataset sources (#42).

Site-level registry of uploaded data files, an admin explore
console with saved queries, and a `::: dataset :::` markdown
directive baking tables / Vega-Lite charts / scalar values into
`body_html` at save time. See the spec at
`_claude/specs/2026-06-12-datasets-design.md`.
"""
