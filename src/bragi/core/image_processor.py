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

from PIL import Image, UnidentifiedImageError

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
            return ImageMetadata(width=img.width, height=img.height, format=img.format)
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
            if img.width <= target_width:
                # Don't upscale; the source already covers this slot.
                return None
            # Preserve aspect ratio. thumbnail() rescales in place
            # and is high-quality (LANCZOS by default in Pillow 10+).
            target_height = round(img.height * target_width / img.width)
            resized = img.copy()
            resized.thumbnail((target_width, target_height))
            out = io.BytesIO()
            save_format = img.format or "PNG"
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
