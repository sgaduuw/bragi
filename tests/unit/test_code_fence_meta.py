"""Pure parsing of fenced-code info-string metadata."""

from __future__ import annotations

from bragi.contrib.highlight.meta import parse_fence_meta


def test_language_only() -> None:
    assert parse_fence_meta("python") == ("python", None, [], False)


def test_empty() -> None:
    assert parse_fence_meta("") == ("", None, [], False)


def test_filename() -> None:
    lang, filename, hl, linenos = parse_fence_meta('python title="app.py"')
    assert (lang, filename, hl, linenos) == ("python", "app.py", [], False)


def test_hl_lines_expand_ranges() -> None:
    _, _, hl, _ = parse_fence_meta("python {1,3-5,8}")
    assert hl == [1, 3, 4, 5, 8]


def test_huge_range_is_ignored_not_expanded() -> None:
    """An absurd author-supplied range must not expand to billions of
    ints and hang the save (DoS guard)."""
    _, _, hl, _ = parse_fence_meta("python {1-2000000000}")
    assert hl == []
    # A sane range still works.
    _, _, hl2, _ = parse_fence_meta("python {5-8}")
    assert hl2 == [5, 6, 7, 8]


def test_linenos_flag() -> None:
    _, _, _, linenos = parse_fence_meta("python linenos")
    assert linenos is True
    # A language that merely contains the substring must NOT trip it.
    _, _, _, linenos2 = parse_fence_meta("linenoscript")
    assert linenos2 is False


def test_all_together_order_independent() -> None:
    lang, filename, hl, linenos = parse_fence_meta('yaml linenos {2} title="play.yml"')
    assert lang == "yaml"
    assert filename == "play.yml"
    assert hl == [2]
    assert linenos is True
