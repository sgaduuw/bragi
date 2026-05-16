"""Embed provider registry.

A Provider is a small object that knows how to turn a URL into
an embeddable HTML snippet. The registry is consulted by the
markdown directive at save time and by the rerender CLI when
backfilling pending embeds.

First-matching provider wins, so order matters: YouTube and
Bluesky come before the generic oEmbed fallback so their
purpose-built renderers (click-to-load, official Bluesky
blockquote) handle their hosts rather than the catch-all.

Plugin authors can later contribute providers via a dedicated
`register_embed_provider` hook; v1 keeps the registry private to
this plugin to avoid speccing a contract before there's a second
implementer.
"""

from __future__ import annotations

from bragi.contrib.embeds.providers.base import EmbedError, Provider
from bragi.contrib.embeds.providers.bluesky import BlueskyProvider
from bragi.contrib.embeds.providers.oembed import GenericOEmbedProvider
from bragi.contrib.embeds.providers.youtube import YouTubeProvider

# Order matters: specific hosts first, generic oEmbed last.
_REGISTRY: list[Provider] = [
    YouTubeProvider(),
    BlueskyProvider(),
    GenericOEmbedProvider(),
]


def lookup(url: str) -> Provider | None:
    """First provider whose `matches(url)` returns True; else None."""
    for provider in _REGISTRY:
        if provider.matches(url):
            return provider
    return None


__all__ = [
    "BlueskyProvider",
    "EmbedError",
    "GenericOEmbedProvider",
    "Provider",
    "YouTubeProvider",
    "lookup",
]
