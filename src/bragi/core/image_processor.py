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
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        # Truncated upload, wrong content-type guess, or a format
        # Pillow can't decode. Return None so the upload still
        # lands; admin can fix the dimensions later.
        LOG.info("Pillow probe failed: %s", exc)
        return None


PillowImageProcessor = ImageProcessorSpec(
    name="pillow",
    can_process=_can_process,
    probe=_probe,
)
