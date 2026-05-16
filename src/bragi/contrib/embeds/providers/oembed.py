"""Generic oEmbed provider with an explicit host allowlist.

Unlike full oEmbed discovery (which would fetch any URL and look
for a `<link rel="alternate" type="application/json+oembed">`),
this provider has a fixed map of trusted host -> endpoint pairs.
Reasons:

- Discovery means fetching arbitrary pages from author-supplied
  URLs at save time. That's a save-amplification attack surface
  worth avoiding when the only operator-visible benefit is
  "fewer config edits when new providers appear."
- The set of providers actually worth embedding in a personal CMS
  is small and slow-changing. New entries land here in code
  review, which is the right place for them.

When a host isn't in the map, `matches()` returns False and the
directive falls back to the pending link card. The rerender CLI
won't be able to recover a host that's still not in the map; that's
intentional, the URL stays rendered as a link.
"""

from __future__ import annotations

from urllib.parse import urlparse

import requests

from bragi.contrib.embeds.providers.base import EmbedError

# host (without scheme) -> oEmbed JSON endpoint. Subdomains aren't
# matched: add `www.example.com` and `example.com` separately
# when both appear in the wild (rare for oEmbed-publishing hosts).
_ALLOWLIST: dict[str, str] = {
    "vimeo.com": "https://vimeo.com/api/oembed.json",
    "www.vimeo.com": "https://vimeo.com/api/oembed.json",
    "soundcloud.com": "https://soundcloud.com/oembed",
    "www.soundcloud.com": "https://soundcloud.com/oembed",
    # Mastodon servers publish oEmbed at `{instance}/api/oembed`.
    # We don't list every instance; users wanting to embed from a
    # specific instance add it here. Most-popular instances:
    "mastodon.social": "https://mastodon.social/api/oembed",
    "fosstodon.org": "https://fosstodon.org/api/oembed",
    "hachyderm.io": "https://hachyderm.io/api/oembed",
}


class GenericOEmbedProvider:
    """Allowlisted oEmbed fallback for the long tail of providers."""

    name = "oembed"

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in _ALLOWLIST

    def render(self, url: str, *, timeout: float, user_agent: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        endpoint = _ALLOWLIST.get(host)
        if endpoint is None:
            # `matches()` is the contract gate; reaching here means
            # the registry was inconsistent or the host was edited
            # mid-render. Treat as a soft failure.
            raise EmbedError(f"oembed: unknown host {host!r}")

        try:
            resp = requests.get(
                endpoint,
                params={"url": url, "format": "json"},
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise EmbedError(f"oembed network error for {url!r}: {exc}") from exc

        if resp.status_code != 200:
            raise EmbedError(f"oembed returned {resp.status_code} for {url!r}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise EmbedError(f"oembed returned non-JSON for {url!r}") from exc

        html = payload.get("html")
        if not isinstance(html, str) or not html.strip():
            raise EmbedError(f"oembed returned empty html for {url!r}")

        host_slug = host.replace(".", "-")
        return f'<div class="bragi-embed bragi-embed--{host_slug}">{html}</div>'
