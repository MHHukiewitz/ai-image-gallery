"""Prepare uploaded images for a vision model: smaller, JPEG, and easy to embed in a request."""

import base64
import io

from PIL import Image, ImageOps

from gallery.domain.errors import AnalyzerError

VISION_MAX_SIDE = 1024
VISION_JPEG_QUALITY = 85
VISION_MIME_TYPE = "image/jpeg"
# How small a JPEG may be decoded: libjpeg scales by 1/2, 1/4 or 1/8 and never below this, so the
# final downscale still starts from at least the target size. Twice the target would be safer for
# sharpness, but a 4032 x 3024 phone photo would then never be halved: 3024 // 2048 is 1.
_DRAFT_SIDE = VISION_MAX_SIDE


def normalize_image(data: bytes) -> tuple[str, bytes]:
    """Downscale to `VISION_MAX_SIDE` on the long side and re-encode as JPEG.

    Returns `(mime_type, jpeg_bytes)`. Smaller images are not enlarged. Raises a non-retryable
    `AnalyzerError` when the bytes aren't an image Pillow can read.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            # For a JPEG, decode at 1/2, 1/4 or 1/8 scale instead of full size. This has to come
            # before the pixels load, and the transpose and convert below each copy them.
            source.draft("RGB", (_DRAFT_SIDE, _DRAFT_SIDE))
            image = ImageOps.exif_transpose(source)
            image = _flatten_to_rgb(image)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise AnalyzerError("The image could not be read", retryable=False) from error
    image.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=VISION_JPEG_QUALITY)
    return VISION_MIME_TYPE, out.getvalue()


def image_data_url(mime_type: str, data: bytes) -> str:
    """Encode bytes as a `data:` URL, the form OpenAI-compatible APIs accept for images."""
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Convert to RGB, putting transparent areas on white rather than the black JPEG would give."""
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if not has_alpha:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background
