"""Unit tests for the multi-format rendition helper."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bragi.core.image_processor import resize_and_encode


def _make_png_bytes(width: int, height: int, color: str = "red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


def _make_png_rgba_bytes(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (255, 0, 0, 128)).save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("target_format", ["original", "webp", "avif"])
def test_resize_and_encode_round_trips(target_format: str) -> None:
    src = _make_png_bytes(64, 32)
    out = resize_and_encode(src, target_width=32, target_format=target_format)
    assert out is not None
    with Image.open(io.BytesIO(out)) as img:
        assert img.width == 32
        assert img.height == 16


def test_resize_and_encode_skips_when_source_smaller_than_target() -> None:
    src = _make_png_bytes(32, 16)
    out = resize_and_encode(src, target_width=64, target_format="webp")
    assert out is None


def test_resize_and_encode_rgba_downconverts_for_jpeg() -> None:
    src = _make_png_rgba_bytes(64, 32)
    out = resize_and_encode(
        src,
        target_width=32,
        target_format="original",
        source_content_type="image/jpeg",
    )
    assert out is not None
    with Image.open(io.BytesIO(out)) as img:
        assert img.mode == "RGB"


def test_resize_and_encode_returns_none_on_garbage_input() -> None:
    out = resize_and_encode(b"not an image at all", target_width=32, target_format="webp")
    assert out is None
