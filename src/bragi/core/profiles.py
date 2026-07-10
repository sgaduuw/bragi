"""Author-profile view model, shared across delivery surfaces.

A `User` carries the profile fields (display name, bio, pronouns, location,
avatar URL, rel="me" links, edited via `bragi.contrib.account_profile`). This
module turns a `User` into a render-ready view + a schema.org Person document,
so the profile page, the post-author h-card byline, and (Phase 3) the
ActivityPub actor all derive the same shape without re-implementing it or
crossing the contrib boundary.

`links` are the already-validated `{label, url}` dicts stored on the user
(validated through `ProfileLink` at save time), so no re-validation is needed
here; templates read `link.label` / `link.url` (Jinja item-access).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from bragi.core.models.user import User
from bragi.core.render.markdown import make_excerpt, render_markdown

# Plain-text bio cap for the structured surfaces (JSON-LD `description`,
# ActivityPub `summary`). The bio is authored as markdown (it is the profile
# narrative), but those two fields want a short, tag-free summary, so a long
# narrative is stripped + trimmed rather than dumped in full.
_BIO_TEXT_MAX = 500


class ProfileView(NamedTuple):
    display_name: str
    bio: str | None  # raw markdown source (truthiness / editing)
    bio_html: str | None  # rendered markdown, for the h-card `p-note`
    bio_text: str | None  # plain-text excerpt, for JSON-LD / AP summary
    pronouns: str | None
    location: str | None
    avatar_url: str | None
    links: list[dict[str, Any]]  # [{label, url}], validated on save


def profile_view(user: User | None) -> ProfileView | None:
    """Assemble a `ProfileView` from a user, or None when there is no user."""
    if user is None:
        return None
    links = user.profile_links if isinstance(user.profile_links, list) else []
    bio = user.bio
    return ProfileView(
        display_name=user.display_name,
        bio=bio,
        bio_html=render_markdown(bio) if bio else None,
        bio_text=make_excerpt(bio, max_chars=_BIO_TEXT_MAX) if bio else None,
        pronouns=user.pronouns,
        location=user.location,
        avatar_url=user.avatar_url,
        links=links,
    )


def profile_jsonld(profile: ProfileView, url: str | None) -> dict[str, Any]:
    """A schema.org Person document for `profile` (JSON-LD friendly)."""
    doc: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": profile.display_name,
    }
    if profile.bio_text:
        doc["description"] = profile.bio_text
    if profile.avatar_url:
        doc["image"] = profile.avatar_url
    if url:
        doc["url"] = url
    same_as = [link["url"] for link in profile.links if isinstance(link, dict) and link.get("url")]
    if same_as:
        doc["sameAs"] = same_as
    return doc
