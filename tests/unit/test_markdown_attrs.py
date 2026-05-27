"""Tests for markdown image attribute (curly-brace) syntax.

`{.class}` after a markdown image lands as the rendered <img>'s
class attribute. We use this for the size + alignment classes
(`size-small`, `align-center`, etc.); the parser doesn't validate,
unknown classes pass through.
"""

from __future__ import annotations

from bragi.core.render.markdown import _build_renderer


def test_image_attrs_single_class() -> None:
    md = _build_renderer()
    out = md.render("![alt](/attachments/x){.size-medium}")
    assert 'class="size-medium"' in out
    assert 'src="/attachments/x"' in out
    assert 'alt="alt"' in out


def test_image_attrs_multiple_classes() -> None:
    md = _build_renderer()
    out = md.render("![alt](/attachments/x){.size-medium .align-center}")
    assert 'class="size-medium align-center"' in out


def test_image_attrs_unknown_classes_pass_through() -> None:
    """The parser doesn't validate; the picker controls the
    vocabulary. Unknown classes still land on the <img>."""
    md = _build_renderer()
    out = md.render("![alt](/attachments/x){.totally-made-up}")
    assert 'class="totally-made-up"' in out


def test_image_without_attrs_renders_unchanged() -> None:
    """No brace block → no class attribute. Regression net for
    pre-existing posts that have plain markdown images."""
    md = _build_renderer()
    out = md.render("![alt](/attachments/x)")
    assert "class=" not in out
    assert 'src="/attachments/x"' in out
