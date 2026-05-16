"""bragi.contrib.embeds: external content embeds.

Adds a `::: embed URL :::` markdown directive that resolves the
URL at save time, dispatches to a provider (YouTube, Bluesky,
generic oEmbed), and inlines the resolved HTML into body_html.

Failed save-time renders fall back to a `bragi-embed--pending`
link card; the `cms embeds rerender-pending` CLI (invoked
periodically by the task-runner sidecar) retries them with a
patient timeout.

Plugin boundary: imports only from `bragi.api`, `bragi.core.*`,
and `bragi.settings`. No cross-imports to sibling
`bragi.contrib.*` plugins.
"""
