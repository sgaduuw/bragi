"""`Site.base_url` strips the trailing slash so URL joins don't double up.

The stored `canonical_url` may carry a trailing slash (operators type
"https://host/" freely); every absolute-URL builder concatenates
`base + "/path"`, so the property has to hand back a slash-free origin
or the result gains a stray "//" (the "///" canonical bug on the home
blog index).
"""

from __future__ import annotations

from bragi.core.models.site import Site


def _site(canonical: str) -> Site:
    return Site(slug="s", hostname="h.example", title="T", canonical_url=canonical, owner_user_id=1)


def test_strips_single_trailing_slash() -> None:
    assert _site("https://example.com/").base_url == "https://example.com"


def test_strips_multiple_trailing_slashes() -> None:
    assert _site("https://example.com///").base_url == "https://example.com"


def test_leaves_bare_origin_untouched() -> None:
    assert _site("https://example.com").base_url == "https://example.com"


def test_empty_stays_empty_and_falsy() -> None:
    assert _site("").base_url == ""
    assert not _site("").base_url
