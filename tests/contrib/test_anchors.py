"""Tests for the heading-anchor html transform."""

from __future__ import annotations

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.anchors.transform import add_heading_anchors, slugify

# --------------------------- slugify ---------------------------


def test_slugify_basic_words() -> None:
    assert slugify("Hello World") == "hello-world"


def test_slugify_lowercases() -> None:
    assert slugify("ALL CAPS") == "all-caps"


def test_slugify_strips_punctuation_and_collapses_hyphens() -> None:
    assert slugify("C++ tips!!") == "c-tips"


def test_slugify_strips_leading_trailing_hyphens() -> None:
    assert slugify("--hi--") == "hi"


def test_slugify_handles_accents_via_nfkd() -> None:
    assert slugify("Naïve approach") == "naive-approach"


def test_slugify_returns_empty_for_no_sluggable_chars() -> None:
    assert slugify("!!!") == ""
    assert slugify("") == ""


# --------------------------- transform ---------------------------


def test_transform_injects_id_on_h1() -> None:
    out = add_heading_anchors("<h1>Hello world</h1>")
    assert out == '<h1 id="hello-world">Hello world</h1>'


def test_transform_injects_id_on_h2_through_h6() -> None:
    src = "<h2>Two</h2><h3>Three</h3><h6>Six</h6>"
    out = add_heading_anchors(src)
    assert 'id="two"' in out
    assert 'id="three"' in out
    assert 'id="six"' in out


def test_transform_skips_headings_with_existing_id() -> None:
    src = '<h2 id="custom">Title</h2>'
    out = add_heading_anchors(src)
    assert out == src


def test_transform_dedups_within_one_document() -> None:
    src = "<h2>Setup</h2><h2>Setup</h2><h2>Setup</h2>"
    out = add_heading_anchors(src)
    assert 'id="setup"' in out
    assert 'id="setup-2"' in out
    assert 'id="setup-3"' in out


def test_transform_dedup_respects_existing_explicit_ids() -> None:
    """If a heading already has `id="setup"`, the next auto-id-derived
    `setup` should bump to `setup-2`."""
    src = '<h2 id="setup">First</h2><h2>Setup</h2>'
    out = add_heading_anchors(src)
    # The first heading's id is unchanged.
    assert '<h2 id="setup">First</h2>' in out
    # The second one couldn't take 'setup' so it became 'setup-2'.
    assert 'id="setup-2"' in out


def test_transform_strips_inner_tags_for_slug() -> None:
    src = "<h2>Hello <code>world</code></h2>"
    out = add_heading_anchors(src)
    assert 'id="hello-world"' in out
    # Inner markup is preserved verbatim in the output text.
    assert "<code>world</code>" in out


def test_transform_skips_empty_slug() -> None:
    """A heading like `<h2>!!!</h2>` slugifies to "" and gets no id."""
    src = "<h2>!!!</h2>"
    out = add_heading_anchors(src)
    assert out == src


def test_transform_preserves_existing_attributes() -> None:
    """Other attributes (class, data-*) survive the rewrite."""
    src = '<h2 class="big" data-x="y">Header</h2>'
    out = add_heading_anchors(src)
    assert 'class="big"' in out
    assert 'data-x="y"' in out
    assert 'id="header"' in out


def test_transform_unaffected_by_nested_html_in_long_doc() -> None:
    """Non-heading markup between headings is untouched."""
    src = (
        "<h1>Intro</h1>"
        "<p>Some paragraph.</p>"
        "<pre><code>x = 1</code></pre>"
        "<h2>Next</h2>"
    )
    out = add_heading_anchors(src)
    assert "<p>Some paragraph.</p>" in out
    assert "<pre><code>x = 1</code></pre>" in out
    assert 'id="intro"' in out
    assert 'id="next"' in out


# --------------------------- plugin wiring ---------------------------


def test_plugin_registers_html_transform_in_delivery_app() -> None:
    app = create_delivery_app()
    names = app.extensions["html_transforms"].names()
    assert "heading-anchors" in names


def test_pygments_then_anchors_runs_in_that_order() -> None:
    """Priority 50 (highlight) precedes priority 200 (anchors)."""
    app = create_delivery_app()
    names = app.extensions["html_transforms"].names()
    assert names.index("pygments-highlight") < names.index("heading-anchors")
