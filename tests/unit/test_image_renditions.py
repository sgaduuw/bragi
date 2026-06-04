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


def _make_jpeg_with_exif_orientation(raw_width: int, raw_height: int, orientation: int) -> bytes:
    """Build a JPEG whose raw pixels are `raw_width` x `raw_height`
    and whose EXIF Orientation tag is set to `orientation`. Used to
    pin the portrait-phone-photo bug: raw pixels stored landscape +
    Orientation=6 means the image is intended to display portrait.
    """
    exif = Image.Exif()
    exif[0x0112] = orientation  # 0x0112 = ExifTags.TAGS["Orientation"]
    src = Image.new("RGB", (raw_width, raw_height), "blue")
    out = io.BytesIO()
    src.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def test_probe_reports_exif_corrected_dimensions() -> None:
    """A phone shot stored sideways (raw 200x100) with Orientation=6
    should probe as portrait (100x200), matching what the browser
    shows on the original."""
    from bragi.core.image_processor import _probe

    data = _make_jpeg_with_exif_orientation(raw_width=200, raw_height=100, orientation=6)
    meta = _probe(data)
    assert meta is not None
    assert meta.width == 100
    assert meta.height == 200


def test_resize_and_encode_honours_exif_orientation() -> None:
    """Resizing a portrait image (raw 200x100 + Orientation=6) to
    target_width=50 should produce a 50x100 portrait rendition --
    NOT a 50x25 landscape one."""
    data = _make_jpeg_with_exif_orientation(raw_width=200, raw_height=100, orientation=6)
    out = resize_and_encode(
        data,
        target_width=50,
        target_format="original",
        source_content_type="image/jpeg",
    )
    assert out is not None
    with Image.open(io.BytesIO(out)) as img:
        assert img.width == 50
        assert img.height == 100  # portrait, not 25


def test_resize_and_encode_skips_when_oriented_source_smaller_than_target() -> None:
    """A portrait phone shot (raw 200x100, Orientation=6, oriented
    width=100) should NOT generate a rendition for target_width=150
    -- that would be upscaling. Without the EXIF fix, the raw 200
    width would compare against 150 and a (wrong-orientation)
    rendition would be generated."""
    data = _make_jpeg_with_exif_orientation(raw_width=200, raw_height=100, orientation=6)
    out = resize_and_encode(
        data,
        target_width=150,
        target_format="original",
        source_content_type="image/jpeg",
    )
    assert out is None


def test_resize_strips_exif_orientation_from_output() -> None:
    """After applying the EXIF transpose to the working pixels, the
    output should have NO Orientation tag (or Orientation=1). If we
    accidentally kept the tag, browsers would re-rotate the
    already-rotated bytes and end up double-rotated."""
    # Raw 400x100 with Orientation=6 -> oriented 100x400 (portrait).
    # target_width=50 is strictly less than oriented.width=100, so a
    # rendition WILL be produced.
    data = _make_jpeg_with_exif_orientation(raw_width=400, raw_height=100, orientation=6)
    out = resize_and_encode(
        data,
        target_width=50,
        target_format="original",
        source_content_type="image/jpeg",
    )
    assert out is not None
    with Image.open(io.BytesIO(out)) as img:
        exif = img.getexif()
        # 0x0112 is the Orientation tag. Either absent or 1 (normal).
        assert exif.get(0x0112, 1) == 1
