"""Unit tests for the 404 suggestion engine (pure, no app / DB)."""

from __future__ import annotations

from bragi.contrib.notfound.blocklist import is_blocklisted
from bragi.contrib.notfound.suggestions import Candidate, suggest


def _pub(slug: str, url: str) -> Candidate:
    return Candidate(
        slug=slug, title=slug.title(), url=url, edit_url=f"/edit/{slug}", archived=False
    )


def _arch(slug: str) -> Candidate:
    return Candidate(
        slug=slug, title=slug.title(), url=None, edit_url=f"/edit/{slug}", archived=True
    )


def test_exact_published_match_at_different_url_suggests_redirect() -> None:
    # Hit /foo/ but the post lives at /blog/foo/: high-confidence redirect.
    s = suggest("/foo/", [_pub("foo", "/blog/foo/")])
    assert s is not None
    assert s.kind == "redirect"
    assert s.candidate.url == "/blog/foo/"


def test_exact_published_match_at_same_url_is_not_suggested() -> None:
    # The path already equals the content URL: nothing to redirect to.
    assert suggest("/blog/foo/", [_pub("foo", "/blog/foo/")]) is None


def test_archived_exact_match_is_informational() -> None:
    s = suggest("/gone/", [_arch("gone")])
    assert s is not None
    assert s.kind == "archived"
    assert s.candidate.url is None


def test_published_exact_beats_archived_exact() -> None:
    s = suggest("/foo/", [_arch("foo"), _pub("foo", "/blog/foo/")])
    assert s is not None
    assert s.kind == "redirect"


def test_fuzzy_published_match_catches_typo() -> None:
    s = suggest("/introducton/", [_pub("introduction", "/introduction/")])
    assert s is not None
    assert s.kind == "redirect"
    assert s.candidate.slug == "introduction"


def test_no_match_below_cutoff_returns_none() -> None:
    assert suggest("/wildly-different/", [_pub("introduction", "/introduction/")]) is None


def test_empty_or_root_path_returns_none() -> None:
    assert suggest("/", [_pub("x", "/x/")]) is None
    assert suggest("", [_pub("x", "/x/")]) is None


def test_blocklist_matches_case_insensitively_and_globs() -> None:
    patterns = ["*.php", "/wp-admin/*", "/.env"]
    assert is_blocklisted("/x.php", patterns)
    assert is_blocklisted("/X.PHP", patterns)  # case-folded
    assert is_blocklisted("/wp-admin/setup.php", patterns)
    assert is_blocklisted("/.env", patterns)
    assert not is_blocklisted("/real-post/", patterns)
    assert not is_blocklisted("/.well-known/security.txt", patterns)
