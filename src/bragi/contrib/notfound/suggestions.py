"""Suggest a likely fix for a detected 404.

Pure logic: the admin view gathers content candidates (one query
each for Post and Page) and hands them here; this module does the
matching and returns at most one suggestion. URL construction (live
URLs, edit links) is the view's job, pre-computed onto each
`Candidate`, so this stays free of Flask / SQLAlchemy and unit-
testable.

Match order, best first:

1. Exact leaf-slug match against a PUBLISHED item living at a
   different URL. This is the "right content, wrong path" case
   (e.g. a hit on `/foo/` when the post lives at `/blog/foo/`):
   high-confidence, actionable redirect.
2. Exact leaf-slug match against an ARCHIVED item. Informational:
   "this existed but you archived it"; an archived item has no live
   URL, so it is not a one-click redirect target.
3. Fuzzy match (difflib) against published slugs. Catches typos and
   renames; best-effort actionable redirect.

ponytail: leaf-slug matching only (cheap, covers rename / typo /
wrong-prefix). Full path-similarity or an audit-log deep-match
against hard-deleted content is deferred until it is wanted.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# difflib cutoff for the fuzzy pass: 0.0 (anything) .. 1.0 (identical).
# 0.7 keeps "old-slug" -> "new-slug" style near-misses while rejecting
# unrelated words.
_FUZZY_CUTOFF = 0.7


@dataclass(frozen=True)
class Candidate:
    """One piece of content the 404 path might have meant.

    `url` is the live public path (None when not published / no
    public URL). `edit_url` is the admin edit link (for the archived
    informational case). `archived` marks archived content.
    """

    slug: str
    title: str
    url: str | None
    edit_url: str | None
    archived: bool


@dataclass(frozen=True)
class Suggestion:
    """A proposed fix. `kind` is "redirect" (actionable, target is a
    live URL on `candidate.url`) or "archived" (informational)."""

    kind: str  # "redirect" | "archived"
    candidate: Candidate


def _leaf(path: str) -> str:
    """Last non-empty path segment (the slug-shaped part)."""
    return path.strip("/").split("/")[-1] if path.strip("/") else ""


def suggest(path: str, candidates: list[Candidate]) -> Suggestion | None:
    leaf = _leaf(path)
    if not leaf:
        return None

    published = [c for c in candidates if not c.archived and c.url]

    # 1. Exact published match at a different URL (wrong-prefix case).
    for c in published:
        if c.slug == leaf and c.url != path:
            return Suggestion("redirect", c)

    # 2. Exact archived match (informational).
    for c in candidates:
        if c.archived and c.slug == leaf:
            return Suggestion("archived", c)

    # 3. Fuzzy published match (typo / rename).
    by_slug = {c.slug: c for c in published}
    close = difflib.get_close_matches(leaf, list(by_slug), n=1, cutoff=_FUZZY_CUTOFF)
    if close:
        winner = by_slug[close[0]]
        if winner.url != path:
            return Suggestion("redirect", winner)

    return None
