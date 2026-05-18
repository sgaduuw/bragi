"""Bluesky embed provider.

Bluesky publishes an oEmbed endpoint at
`https://embed.bsky.app/oembed?url=<post-url>` that returns a
`<blockquote class="bluesky-embed">` plus a script tag pointing
at `embed.bsky.app/static/embed.js`. The blockquote is fully
self-contained: text content lives in the markup itself, so even
without the script reading the post is fine.

The script enhances the blockquote with thread context (avatar,
handle, like/repost counts) once loaded. Including it requires a
single network call to `embed.bsky.app` per reader, comparable to
Twitter's old `<blockquote class="twitter-tweet">` flow.
"""

from __future__ import annotations

from urllib.parse import urlparse

from bragi.contrib.embeds.providers.base import EmbedError
from bragi.core.http import SafeHTTPError, safe_get

_OEMBED_ENDPOINT = "https://embed.bsky.app/oembed"


class BlueskyProvider:
    """Renders bsky.app post URLs via the official oEmbed endpoint."""

    name = "bluesky"

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "bsky.app" or host.endswith(".bsky.app")

    def render(self, url: str, *, timeout: float, user_agent: str) -> str:
        try:
            # Route through `safe_get` so any 3xx the bsky endpoint
            # returns is re-validated against the public-IP guard
            # before we follow it.
            resp = safe_get(
                _OEMBED_ENDPOINT,
                params={"url": url, "format": "json"},
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
        except SafeHTTPError as exc:
            raise EmbedError(f"bluesky oembed blocked by SSRF guard for {url!r}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - render fallbacks downstream
            raise EmbedError(f"bluesky oembed network error for {url!r}: {exc}") from exc

        if resp.status_code != 200:
            raise EmbedError(f"bluesky oembed returned {resp.status_code} for {url!r}")

        try:
            html = resp.json().get("html")
        except ValueError as exc:
            raise EmbedError(f"bluesky oembed returned non-JSON for {url!r}") from exc

        if not isinstance(html, str) or not html.strip():
            raise EmbedError(f"bluesky oembed returned empty html for {url!r}")

        # Wrap so delivery CSS can target the embed uniformly.
        # Bluesky's snippet is already self-contained; the wrapper
        # only adds the project's marker class.
        return f'<div class="bragi-embed bragi-embed--bluesky">{html}</div>'
