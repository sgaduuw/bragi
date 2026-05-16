"""bragi's in-tree default theme.

Ships the site shell (`delivery/base.html`) that every public page
extends. Registered via `register_theme` under slug `"default"`,
same hook surface a third-party `bragi-theme-foo` package would
use. This is the one in-tree consumer that keeps the `ThemeSpec`
contract honest: every change to the theme contract has to land
here too, so the abstraction doesn't drift away from real use.

`ThemeAwareLoader` falls back to slug `"default"` when a Site's
`theme` is NULL, so disabling this package (comment its line in
`pyproject.toml`'s `bragi.plugins` group) is the supported way to
test the "no theme installed" path.
"""
