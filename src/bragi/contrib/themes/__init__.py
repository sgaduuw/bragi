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

The in-tree default theme lives in `bragi.contrib.theme_default`
and registers slug `"default"`. A site with `Site.theme=NULL`
resolves through that slug; sites that picked a third-party
theme (`Site.theme="<slug>"`) resolve through whichever
`bragi-theme-foo` package shipped that slug via `register_theme`
(entry-point group `bragi.plugins`, same as every other plugin).
This package only owns the *consumer* surfaces; it does not ship
or own any specific theme.
"""
