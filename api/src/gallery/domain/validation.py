"""Upload validation: decide from the bytes alone whether a file is an image we accept.

Nothing here trusts the client. The format comes from sniffing the content, and the limits are
checked before the pixels are decoded, so a small file that claims a huge canvas is cheap to refuse.
No pixel is ever decoded here: that costs tens of megabytes for a phone photo, and the thumbnail
step decodes the image anyway (at reduced size for a JPEG).
"""

import struct
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import ExifTags, Image

from gallery.domain.errors import InvalidImageError

type ImageFormat = Literal["jpeg", "png"]

# Pillow's names for what it sniffed. A JPEG with several embedded pictures, as many phones write,
# is reported as "MPO". Browsers and every JPEG decoder still treat it as a JPEG.
_FORMATS: dict[str, ImageFormat] = {"JPEG": "jpeg", "MPO": "jpeg", "PNG": "png"}
_MIME_TYPES: dict[ImageFormat, str] = {"jpeg": "image/jpeg", "png": "image/png"}
_EXTENSIONS: dict[ImageFormat, str] = {"jpeg": "jpg", "png": "png"}
# The first bytes of every JPEG and PNG file. Only used to tell a damaged file from a foreign one.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SIGNATURES = (b"\xff\xd8\xff", _PNG_SIGNATURE)
# EXIF orientations 5 to 8 turn the picture a quarter turn, which swaps its width and height.
_QUARTER_TURN_ORIENTATIONS = frozenset({5, 6, 7, 8})


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """What an accepted upload turned out to be. Width and height are as displayed, upright."""

    format: ImageFormat
    mime_type: str
    extension: str
    width: int
    height: int


def inspect_image(data: bytes, *, max_bytes: int, max_pixels: int) -> ImageInfo:
    """Check that `data` is a decodable JPEG or PNG within the limits, and describe it.

    Only the header is read, so a file that is cut off or damaged after it passes here. It fails
    in the thumbnail step, which decodes every pixel. Raises InvalidImageError with the code
    `too_large`, `too_many_pixels`, `unsupported_type` or `corrupt`.
    """
    if len(data) > max_bytes:
        raise InvalidImageError("too_large", f"The file is larger than {max_bytes / 2**20:g} MB.")
    try:
        # Pillow only warns when an image looks like a decompression bomb. Make it an error so
        # that it can't slip through. The filter change isn't thread-safe, so it's a backstop:
        # the pixel limit in _inspect holds whether or not this filter is in place.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            return _inspect(data, max_pixels)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise _too_many_pixels(max_pixels) from error
    except Image.UnidentifiedImageError as error:
        if data.startswith(_SIGNATURES):
            # Pillow gives up on a header that is cut short, but the file is a damaged JPEG or PNG.
            raise InvalidImageError("corrupt", "The image can't be decoded.") from error
        raise InvalidImageError(
            "unsupported_type", "Only JPEG and PNG images are accepted."
        ) from error
    except (OSError, SyntaxError, ValueError) as error:
        # Pillow raises these for damaged data, such as a truncated file or a bad chunk.
        raise InvalidImageError("corrupt", "The image can't be decoded.") from error


def _too_many_pixels(max_pixels: int) -> InvalidImageError:
    return InvalidImageError("too_many_pixels", f"The image has more than {max_pixels:,} pixels.")


def _inspect(data: bytes, max_pixels: int) -> ImageInfo:
    # Restricting the formats stops Pillow from even trying the other decoders on untrusted bytes.
    with Image.open(BytesIO(data), formats=["JPEG", "PNG"]) as image:
        image_format = _FORMATS.get(image.format or "")
        if image_format is None:
            raise Image.UnidentifiedImageError(f"Unexpected format: {image.format}")
        width, height = image.size
        # The size comes from the header, so this refuses a decompression bomb before decoding it.
        if width * height > max_pixels:
            raise _too_many_pixels(max_pixels)
        orientation = _exif(image, data).get(ExifTags.Base.Orientation)
    if orientation in _QUARTER_TURN_ORIENTATIONS:
        width, height = height, width
    return ImageInfo(
        format=image_format,
        mime_type=_MIME_TYPES[image_format],
        extension=_EXTENSIONS[image_format],
        width=width,
        height=height,
    )


def _exif(image: Image.Image, data: bytes) -> Image.Exif:
    """Return the EXIF block of `image` without decoding its pixels."""
    if image.format != "PNG" or "exif" in image.info:
        return image.getexif()
    # A PNG may put its eXIf chunk after the pixel data, so Pillow's getexif() decodes the whole
    # image to look for it. Walk the chunk list instead, which reads no pixel.
    exif = Image.Exif()
    position = len(_PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        if kind == b"eXIf":
            exif.load(data[position + 8 : position + 8 + length])
            break
        if kind == b"IEND":
            break
        position += 8 + length + 4  # chunk header, body and CRC
    return exif
