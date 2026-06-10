"""Default Pillow-backed image processor.

`PillowImageProcessor` is the `ImageProcessorSpec` value
registered by `bragi.contrib.attachments`. Its Phase 1 job is
probing uploaded bytes for width / height / format so the
Attachment row records them on save.

A plugin that wants libvips (faster on large images, better
quality on resampling) registers its own `ImageProcessorSpec`
from `register_image_processor`; resolution order is the first
processor whose `can_process(content_type)` returns True, so a
libvips plugin that claims `image/*` will take precedence over
Pillow without any per-call wiring at the call site.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

from bragi.api import ImageMetadata, ImageProcessorSpec

LOG = logging.getLogger(__name__)

# Pillow's default `MAX_IMAGE_PIXELS` is ~178 megapixels and the
# bomb-error threshold is twice that. With a 20 MiB upload cap a
# craft pixel-stream can comfortably stay below the bomb threshold
# while still allocating gigabytes when expanded. 50 megapixels
# covers anything a personal-blog author legitimately uploads
# (8K is ~33 MP) without leaving headroom for an attacker.
Image.MAX_IMAGE_PIXELS = 50_000_000

_PILLOW_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/avif",
    }
)


def _can_process(content_type: str) -> bool:
    return content_type.lower() in _PILLOW_CONTENT_TYPES


def _probe(data: bytes) -> ImageMetadata | None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            # Apply EXIF orientation so width / height reflect the
            # visually-correct dimensions of the displayed image, not
            # the raw pixel buffer. Portrait phone shots ship raw
            # pixels landscape with an Orientation tag; without this
            # transpose, `width` and `height` come back swapped and
            # the rendition ladder picks the wrong target widths.
            oriented = ImageOps.exif_transpose(img) or img
            return ImageMetadata(width=oriented.width, height=oriented.height, format=img.format)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        # Truncated upload, wrong content-type guess, or a format
        # Pillow can't decode. Return None so the upload still
        # lands; admin can fix the dimensions later.
        LOG.info("Pillow probe failed: %s", exc)
        return None


def _resize(data: bytes, target_width: int) -> bytes | None:
    """Rescale `data` to fit `target_width`, preserving aspect ratio.

    Returns None if the source can't be decoded or is already at
    or below `target_width` (no point storing a duplicate). The
    output format mirrors the input so a PNG stays a PNG, a JPEG
    stays a JPEG: re-encoding to a different format here would
    leak through `Attachment.content_type` and break the
    `<picture>` srcset contract.
    """
    if target_width <= 0:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            save_format = img.format or "PNG"
            # Apply EXIF orientation so renditions match how the
            # original visually displays; without this step a portrait
            # phone photo with Orientation=6 in EXIF comes out rotated
            # 90° because the browser's auto-rotate is consumed at
            # display time on the original, not at resample time here.
            oriented = ImageOps.exif_transpose(img) or img
            if oriented.width <= target_width:
                # Don't upscale; the source already covers this slot.
                return None
            # Preserve aspect ratio. thumbnail() rescales in place
            # and is high-quality (LANCZOS by default in Pillow 10+).
            target_height = round(oriented.height * target_width / oriented.width)
            resized = oriented.copy()
            resized.thumbnail((target_width, target_height))
            out = io.BytesIO()
            # JPEGs default to quality=75; bump to 85 for sharper
            # renditions at the cost of a few KB. Other formats
            # use Pillow defaults.
            save_kwargs: dict[str, int | bool] = {}
            if save_format == "JPEG":
                save_kwargs["quality"] = 85
                save_kwargs["optimize"] = True
            resized.save(out, format=save_format, **save_kwargs)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        LOG.info("Pillow resize to %dw failed: %s", target_width, exc)
        return None


PillowImageProcessor = ImageProcessorSpec(
    name="pillow",
    can_process=_can_process,
    probe=_probe,
    resize=_resize,
)


# AVIF support: importing the plugin registers the codec with Pillow
# at process startup. The dep is pinned in pyproject.toml so absence
# is a deploy error; we WARN on missing-import so an operator sees
# the misconfiguration on startup rather than discovering it via
# stuck-pending AVIF renditions hours later. `pillow-avif-plugin`
# ships no stubs / `py.typed`, hence the mypy ignore.
try:
    import pillow_avif  # type: ignore[import-untyped]  # noqa: F401  -- registers PIL.AvifImagePlugin
except ImportError:
    LOG.warning(
        "pillow_avif not installed; AVIF rendition encoding will fail. Install pillow-avif-plugin."
    )


_PIL_FORMAT_BY_TARGET = {
    "avif": "AVIF",
    "webp": "WEBP",
}


def resize_and_encode(
    data: bytes,
    *,
    target_width: int,
    target_format: str,
    source_content_type: str | None = None,
) -> bytes | None:
    """Rescale `data` to `target_width` and encode as `target_format`.

    `target_format` is one of `'avif'`, `'webp'`, `'original'`. The
    `'original'` case re-encodes in the source's own format. When
    `source_content_type` is supplied (the canonical Attachment
    `content_type` the caller stored on upload), it takes
    precedence over Pillow's in-bytes format probe so a JPEG
    stored under `image/jpeg` re-encodes as JPEG (with the RGBA →
    RGB downconvert) even if the bytes were mislabelled on the
    way in. Without an explicit content type, we fall back to
    whatever Pillow infers from the bytes.

    Quality knobs come from `Settings`:
    `attachment_rendition_quality_{jpeg,webp,avif}`.

    Animated GIFs are intentionally treated as stills: Pillow's
    default `save()` writes the first frame and drops the rest.
    Same applies to multi-frame WebP. v1 scope; if animation
    fidelity matters, add an `is_animated` branch with
    `save_all=True` later.

    Returns `None` if:
    - The source can't be decoded.
    - The source's width is already <= `target_width` (no upscaling).
    - The encoder fails for the chosen format.

    Aspect ratio is preserved (Pillow `thumbnail()`, LANCZOS).
    JPEG output downconverts an RGBA source to RGB so the encoder
    doesn't refuse the alpha channel.
    """
    from bragi.settings import settings as _settings

    if target_width <= 0:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            # Apply EXIF orientation so renditions match how the
            # original visually displays; see _resize for the full
            # rationale. Captures img.format before the transpose
            # because exif_transpose returns a fresh Image without
            # the format attribute.
            source_format = img.format
            oriented = ImageOps.exif_transpose(img) or img
            if oriented.width <= target_width:
                return None
            target_height = round(oriented.height * target_width / oriented.width)
            resized = oriented.copy()
            resized.thumbnail((target_width, target_height))

            if target_format == "original":
                # The Attachment's stored `content_type` is bragi's
                # canonical truth for what the file is; Pillow's
                # in-bytes probe is the fallback for callers that
                # don't have a content_type to hand.
                save_format = _format_for_content_type(source_content_type) or source_format
            else:
                save_format = _PIL_FORMAT_BY_TARGET.get(target_format)
            if save_format is None:
                return None

            save_kwargs: dict[str, int | bool] = {}
            if save_format == "JPEG":
                save_kwargs["quality"] = _settings.attachment_rendition_quality_jpeg
                save_kwargs["optimize"] = True
                if resized.mode in ("RGBA", "LA", "P"):
                    resized = resized.convert("RGB")
            elif save_format == "WEBP":
                save_kwargs["quality"] = _settings.attachment_rendition_quality_webp
            elif save_format == "AVIF":
                save_kwargs["quality"] = _settings.attachment_rendition_quality_avif

            out = io.BytesIO()
            resized.save(out, format=save_format, **save_kwargs)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        LOG.info(
            "Pillow resize_and_encode to %dw %s failed: %s",
            target_width,
            target_format,
            exc,
        )
        return None


def _format_for_content_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    mapping = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/gif": "GIF",
        "image/webp": "WEBP",
        "image/tiff": "TIFF",
        "image/bmp": "BMP",
        "image/avif": "AVIF",
    }
    return mapping.get(content_type.lower())
