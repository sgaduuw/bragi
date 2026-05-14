"""Themes plugin: blueprint + CLI for the theme registry (#40).

The theme contract itself (`ThemeSpec`, `register_theme` hookspec,
`Registry.themes` / `Registry.theme(slug)`, `ThemeAwareLoader`)
lives in core because the loader has to be wired at app boot
before any plugin can override a template. This contrib package
ships the consumer surface that does NOT need to live in core:

- `/theme/<slug>/static/<path>` delivery blueprint serving theme
  static assets when the theme set a `static_dir`.
- `cms theme list` CLI subcommand for sanity-checking which
  themes the running process discovered.

There is no in-tree default theme; v1 ships the contract only.
Sites with `theme=NULL` render exactly as before. A third-party
`bragi-theme-foo` package slots in via the `register_theme`
hookspec (entry-point group `bragi.plugins`, same as every other
plugin) without touching this package.
"""
