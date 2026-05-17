"""YouTube embed provider.

Two render modes (controlled by `embed_youtube_mode`):

- `click-to-load` (default, privacy-preserving): renders a static
  thumbnail wrapped in a button. No network call to Google until
  the reader clicks Play. A small delegated click handler (added
  once per page by `transforms.inject_youtube_cto_script`)
  replaces the wrapper with the nocookie iframe on click.

- `iframe`: renders the youtube-nocookie iframe directly. No
  static thumbnail, autoplays only if the reader interacts (per
  browser policy), but contacts Google on page load.

Either way, the iframe source is `youtube-nocookie.com` (not
`youtube.com`) so the cookie set on read is limited and never
contains an existing Google ad-tracking identifier.

Title fetch via oEmbed is best-effort (used for the
click-to-load `aria-label`); a 404 / timeout falls back to a
generic "Play video" label rather than raising.
"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import parse_qs, urlparse

from bragi.contrib.embeds.providers.base import EmbedError
from bragi.core.http import SafeHTTPError, safe_get
from bragi.settings import settings

# Hosts owned by YouTube. Matched case-insensitively.
_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)

# Video-id constraint per YouTube's documented shape: 11 chars,
# url-safe base64 alphabet. Validating defends against an attacker
# pasting a URL whose "id" injects markup once interpolated.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"


def _extract_id(url: str) -> str | None:
    """Pull the 11-char video id out of any YouTube URL shape.

    Returns None on a URL the provider can't recognise (which the
    directive surfaces as a `bragi-embed--pending` fallback, not a
    crash).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host == "youtu.be":
        # https://youtu.be/VIDEOID
        candidate = path.lstrip("/").split("/", 1)[0]
    elif "/watch" in path:
        # https://www.youtube.com/watch?v=VIDEOID
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    elif "/embed/" in path:
        # https://www.youtube.com/embed/VIDEOID
        candidate = path.split("/embed/", 1)[1].split("/", 1)[0]
    elif "/shorts/" in path:
        # https://www.youtube.com/shorts/VIDEOID
        candidate = path.split("/shorts/", 1)[1].split("/", 1)[0]
    else:
        return None

    return candidate if _ID_RE.match(candidate) else None


def _fetch_title(url: str, *, timeout: float, user_agent: str) -> str | None:
    """Look up the video title via YouTube's oEmbed endpoint.

    Best-effort: failure returns None so the caller can fall back
    to a generic aria-label. Never raises `EmbedError`.
    """
    try:
        # `safe_get` re-validates any 3xx returned by the YouTube
        # endpoint so a misconfigured / compromised provider can't
        # pivot us into RFC 1918.
        resp = safe_get(
            _OEMBED_ENDPOINT,
            params={"url": url, "format": "json"},
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        title = resp.json().get("title")
        return title if isinstance(title, str) and title.strip() else None
    except (SafeHTTPError, ValueError, Exception):  # noqa: BLE001 - best effort
        return None


class YouTubeProvider:
    """Renders YouTube URLs as click-to-load or iframe embeds."""

    name = "youtube"

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in _HOSTS

    def render(self, url: str, *, timeout: float, user_agent: str) -> str:
        video_id = _extract_id(url)
        if video_id is None:
            # Surfaces as pending fallback so the rerender CLI can
            # have another look (no point retrying a structurally
            # broken URL, but the directive treats both shapes
            # uniformly).
            raise EmbedError(f"could not extract YouTube id from {url!r}")

        mode = settings.embed_youtube_mode
        if mode == "iframe":
            return _render_iframe(video_id)

        # Default: click-to-load. Title fetch is best-effort; we
        # commit the embed even if the oEmbed call fails so a
        # transient YouTube outage doesn't block the author save.
        title = _fetch_title(url, timeout=timeout, user_agent=user_agent)
        return _render_click_to_load(video_id, title)


def _render_iframe(video_id: str) -> str:
    """Direct nocookie iframe. Same shape lite-youtube ships in
    its expanded state, with a 16:9 aspect-ratio wrapper for
    responsive layout."""
    src = f"https://www.youtube-nocookie.com/embed/{video_id}"
    return (
        '<div class="bragi-embed bragi-embed--youtube">'
        f'<iframe src="{src}" '
        'title="YouTube video" '
        'loading="lazy" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
        'gyroscope; picture-in-picture; web-share" '
        "allowfullscreen></iframe>"
        "</div>"
    )


def _render_click_to_load(video_id: str, title: str | None) -> str:
    """Static thumbnail + Play button. The delegated click handler
    injected by `transforms.inject_youtube_cto_script` swaps this
    wrapper for the nocookie iframe on click.

    `hqdefault.jpg` is the highest-quality thumbnail YouTube
    serves on `i.ytimg.com` without authentication. The `<noscript>`
    fallback links straight to youtube.com so readers without JS
    aren't left with a dead Play button.
    """
    safe_title = escape(title) if title else "Play video"
    aria = f"Play: {safe_title}" if title else "Play video"
    thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    watch = f"https://www.youtube.com/watch?v={video_id}"
    return (
        '<div class="bragi-embed bragi-embed--youtube bragi-embed--youtube-cto" '
        f'data-video-id="{video_id}">'
        f'<button type="button" aria-label="{escape(aria)}" '
        'data-embed-action="load">'
        f'<img src="{thumb}" alt="" loading="lazy" decoding="async">'
        '<span class="bragi-embed-play" aria-hidden="true">&#9654;</span>'
        "</button>"
        f'<noscript><a href="{watch}" rel="noopener noreferrer" target="_blank">'
        f"{safe_title}</a></noscript>"
        "</div>"
    )
