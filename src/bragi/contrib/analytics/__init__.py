"""Analytics plugin.

Owns the `analytics_events` write path and the admin
dashboard. The hookspec `record_analytics_event` accepts the
dataclass from `bragi.api`; the hookimpl here translates that
into a row.

Bot traffic is filtered at emit time (the delivery-side
after_request hook skips when UA classifies as `bot`). The DB
keeps the row's `user_agent_class` so the dashboard can split
human vs feed-reader counts.

CONTEXT.md anticipates the events table moving to a monthly
rolling shape once volume justifies it. The writer is the
swap point.
"""
