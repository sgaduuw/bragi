"""Builders for the JSON-LD activities the site emits.

`note_for_post` and `create_for_note` together produce the
Create + Note pair Mastodon expects in its timeline.

The IDs we mint here are stable URLs under `/actor/notes/...` and
`/actor/activities/...`. They aren't directly served (we don't
need a per-activity endpoint for v1; followers consume their
inbox copy), but they need to be unique so dedup on the receiver
side works.
"""

from __future__ import annotations

import re
from typing import Any

from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.time import naive_utcnow

# Public collection IRI per spec; signals "public-addressed" so
# Mastodon shows the post on the actor's profile and federates it.
PUBLIC_AUDIENCE = "https://www.w3.org/ns/activitystreams#Public"


def actor_url(site: Site) -> str:
    """Canonical absolute URL of the site's actor."""
    canonical = site.base_url
    return f"{canonical}/actor"


def post_canonical_url(site: Site, path: str) -> str:
    """Combine the site canonical with a relative post path."""
    return f"{site.base_url}{path}"


def note_for_post(site: Site, post: Post, post_path: str) -> dict[str, Any]:
    """Build the `Note` object for a Create activity.

    `content` is HTML (Mastodon renders it inline). We use the
    post's stored `body_html` rendered with a brief excerpt
    wrapper so the timeline shows a short preview with a link
    back to the canonical URL.
    """
    note_id = f"{actor_url(site)}/notes/{post.id}"
    content_html = _short_html(post)
    # `published_at` is stored naive UTC; on a not-yet-published post
    # the fallback uses the current moment in the same shape.
    published = (post.published_at or naive_utcnow()).isoformat() + "Z"
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": note_id,
        "type": "Note",
        "attributedTo": actor_url(site),
        "content": content_html,
        "published": published,
        "url": post_canonical_url(site, post_path),
        "to": [PUBLIC_AUDIENCE],
        "cc": [f"{actor_url(site)}/followers"],
    }


def create_for_note(site: Site, note: dict[str, Any]) -> dict[str, Any]:
    """Wrap a Note in a Create activity addressed to followers + public."""
    activity_id = f"{note['id']}/activity"
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id,
        "type": "Create",
        "actor": actor_url(site),
        "object": note,
        "published": note["published"],
        "to": note["to"],
        "cc": note["cc"],
    }


def _short_html(post: Post) -> str:
    """Short timeline preview: excerpt + link back to the full post.

    Mastodon strips most HTML and links, but `<p>` and `<a>` come
    through, so a short paragraph with a "Read more" anchor is the
    safe shape. Falls back to the title when no excerpt exists.
    """
    excerpt = (post.body_excerpt or "").strip() or (post.title or "").strip()
    excerpt_escaped = _escape_html(excerpt)
    return f"<p>{excerpt_escaped}</p>"


def _escape_html(text: str) -> str:
    """Minimal HTML escape; Mastodon sanitises but better safe."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def actor_document(
    site: Site,
    public_key_pem: str,
    key_id: str,
    *,
    summary: str = "",
    icon_url: str | None = None,
) -> dict[str, Any]:
    """The actor JSON-LD document published at `/actor`.

    `preferredUsername` is the slug-derived handle Mastodon shows
    after the `@` (so `@blog@blog.example.com` references this
    actor). The `Service` type is intentional: a Site is an
    automated publisher, not a real Person; some receivers render
    a different badge for Service actors.

    `summary` and `icon_url` come from the site owner's account
    profile (bio + avatar); the caller resolves them so this stays
    a pure builder. Both degrade to an empty/absent field when the
    owner has no profile set, matching the pre-profile behaviour.
    `icon_url` is stored via the account-profile editor, which
    validates the scheme (http/https) before persisting.
    """
    canonical = site.base_url
    handle = _sanitise_handle(site.slug)
    doc: dict[str, Any] = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "id": actor_url(site),
        "type": "Service",
        "preferredUsername": handle,
        "name": site.title or handle,
        "summary": summary or "",
        "url": canonical or actor_url(site),
        "inbox": f"{actor_url(site)}/inbox",
        "outbox": f"{actor_url(site)}/outbox",
        "followers": f"{actor_url(site)}/followers",
        "publicKey": {
            "id": key_id,
            "owner": actor_url(site),
            "publicKeyPem": public_key_pem,
        },
    }
    if icon_url:
        doc["icon"] = {"type": "Image", "url": icon_url}
    return doc


def webfinger_document(site: Site) -> dict[str, Any]:
    """WebFinger JRD mapping `acct:<handle>@<host>` to the actor URL."""
    handle = _sanitise_handle(site.slug)
    host = site.hostname or ""
    subject = f"acct:{handle}@{host}"
    return {
        "subject": subject,
        "aliases": [actor_url(site)],
        "links": [
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_url(site),
            },
        ],
    }


_HANDLE_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitise_handle(slug: str) -> str:
    """Strip the slug down to a WebFinger-safe handle.

    Mastodon's preferredUsername must match `[A-Za-z0-9_]+`;
    hyphens (common in slugs) become underscores so a slug like
    `my-blog` works as `@my_blog@host`.
    """
    cleaned = _HANDLE_RE.sub("_", slug or "actor")
    return cleaned or "actor"
