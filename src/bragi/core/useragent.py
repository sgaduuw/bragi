"""Cheap User-Agent classifier.

Returns one of `'browser'`, `'bot'`, `'feed-reader'`, `'other'`.
The lookup is a small substring table; precision is intentionally
modest because the goal is to filter bot traffic out of pageview
counts, not to fingerprint the visitor.

Order matters: feed-readers commonly include the substrings that
would match `browser`, and bots commonly include both. Match
feed-readers first, bots second, browsers last.
"""

from __future__ import annotations

_FEED_READERS: tuple[str, ...] = (
    "feedly",
    "feedreader",
    "feedbin",
    "feedburner",
    "newsblur",
    "miniflux",
    "newsbeuter",
    "newsboat",
    "inoreader",
    "rss",
    "atom",
)

_BOTS: tuple[str, ...] = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "duckduckbot",
    "yandex",
    "baidu",
    "applebot",
    "semrush",
    "ahrefs",
    "petalbot",
    "mj12",
    "facebookexternalhit",
    "twitterbot",
    "linkedinbot",
)

_BROWSERS: tuple[str, ...] = (
    "firefox",
    "chrome",
    "safari",
    "edg",
    "opera",
    "vivaldi",
    "brave",
    "mozilla",  # last resort; many UAs include 'Mozilla/5.0 ...'
)


def classify(user_agent: str | None) -> str:
    """Bucket a User-Agent string into one of four classes."""
    if not user_agent:
        return "other"
    ua = user_agent.lower()

    if any(m in ua for m in _FEED_READERS):
        return "feed-reader"
    if any(m in ua for m in _BOTS):
        return "bot"
    if any(m in ua for m in _BROWSERS):
        return "browser"
    return "other"
