"""bragi.contrib.callouts: note / tip / info / warning / danger admonitions.

Adds `::: <type> ... :::` markdown containers (note, tip, info, warning,
danger) whose inner body is parsed as markdown. An optional custom title
follows the type on the opening line (`::: warning Heads up`); it defaults
to the capitalized type name. Renders to a themeable
`<aside class="callout callout--<type>">` with stable class hooks.

Plugin boundary: imports only from `bragi.api`, `bragi.core.*`, and the
markdown-it stack. No cross-imports to sibling `bragi.contrib.*` plugins.
"""
