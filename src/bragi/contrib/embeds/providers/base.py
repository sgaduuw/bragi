"""Provider protocol + shared error type for the embeds plugin.

Kept in its own module so the concrete provider modules
(`youtube.py`, `bluesky.py`, `oembed.py`) can import from here
without circular-import worries through `providers/__init__.py`.
"""

from __future__ import annotations

from typing import Protocol


class EmbedError(Exception):
    """Raised when a provider can't render a URL.

    Wraps both network failures (timeout, non-200) and content
    issues (unsupported URL shape, missing fields in the provider
    response). The directive catches `EmbedError` and falls back
    to the pending link card; everything else propagates so
    bugs aren't silently hidden.
    """


class Provider(Protocol):
    """Renders one or more URL families into embeddable HTML.

    `name` is a short stable identifier used in tests and log
    lines (e.g. `"youtube"`, `"bluesky"`, `"oembed:vimeo"`).
    """

    name: str

    def matches(self, url: str) -> bool:
        """True if this provider claims `url`. Fast host check;
        no network."""

    def render(self, url: str, *, timeout: float, user_agent: str) -> str:
        """Return an HTML snippet for `url`, or raise `EmbedError`.

        Implementations doing HTTP must respect `timeout` (seconds,
        per call) and send `user_agent` in the request headers.
        """
