"""Grid thumbnails: every upload gets a square WebP, so the grid never depends on image shape."""

from io import BytesIO

from PIL import Image, ImageOps

from gallery.domain.errors import InvalidImageError

THUMBNAIL_SIZE = 300
_WEBP_QUALITY = 80


def make_thumbnail(data: bytes, size: int = THUMBNAIL_SIZE) -> bytes:
    """Return a `size` x `size` WebP of the upright image, center-cropped to fill the square.

    Transparency is shown on white, because WebP viewers differ on what lies behind it. Raises
    InvalidImageError when `data` doesn't decode.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            # A 12 MP photo is 36 MB of pixels, and the work below makes two more copies. For a
            # JPEG, libjpeg can decode at 1/2, 1/4 or 1/8 scale, but only if asked before the pixels
            # load. Twice the target keeps the resize sharp. PNG has no such shortcut: draft is a
            # no-op there.
            opened.draft("RGB", (2 * size, 2 * size))
            # Cameras store rotation in EXIF, not in the pixels, so turn the picture upright first.
            upright = _to_8_bit(ImageOps.exif_transpose(opened))
            # Resize before flattening: the work then happens on 300 x 300 pixels, not on the
            # full image. Pillow resizes RGBA with premultiplied alpha, so edges stay clean.
            mode = "RGBA" if upright.has_transparency_data else "RGB"
            square = ImageOps.fit(upright.convert(mode), (size, size), Image.Resampling.LANCZOS)
        if square.mode == "RGBA":
            white = Image.new("RGBA", square.size, (255, 255, 255, 255))
            square = Image.alpha_composite(white, square)
        output = BytesIO()
        square.convert("RGB").save(output, format="WEBP", quality=_WEBP_QUALITY)
    except Image.DecompressionBombError as error:
        raise InvalidImageError("too_many_pixels", "The image is too large to decode.") from error
    except (OSError, SyntaxError, ValueError) as error:
        raise InvalidImageError("corrupt", "The image can't be decoded.") from error
    return output.getvalue()


def _to_8_bit(image: Image.Image) -> Image.Image:
    """Scale 16-bit grayscale down to 8 bits, as browsers do.

    Pillow's own conversion clips every value above 255, which turns the picture white.
    """
    if image.mode.startswith("I;16"):
        return image.point(lambda value: value * (1 / 256)).convert("L")
    return image
