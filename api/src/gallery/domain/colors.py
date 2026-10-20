"""Color math for dominant colors: sRGB to CIE Lab, the ΔE76 distance, and hex formatting.

Pure functions with no I/O. The worker computes dominant colors locally, and the color search
ranks them by ΔE76 in Lab space (docs/architecture.md D6 and §7).
"""

import math
import re
from io import BytesIO

from PIL import Image, ImageOps

from gallery.domain.errors import InvalidImageError

type Rgb = tuple[int, int, int]
type Lab = tuple[float, float, float]

THUMBNAIL_SIZE = (128, 128)
PALETTE_SIZE = 8
# Palette entries this close are near-duplicates, so only the more frequent one is kept.
MIN_DELTA_E = 10.0

# D65 reference white, as X, Y, Z with Y = 1.
_D65_WHITE = (0.95047, 1.0, 1.08883)
# CIE constants, which give the linear segment of the Lab curve near black.
_EPSILON = 216 / 24389
_KAPPA = 24389 / 27
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def dominant_colors(data: bytes, k: int = 3) -> tuple[str, ...]:
    """Return up to `k` dominant colors as lowercase hex, most frequent first.

    Fewer than `k` colors come back when the image has fewer distinct colors, but there's always
    at least one. Raises InvalidImageError when the bytes don't decode as an image.
    """
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    palette = _load_thumbnail(data).quantize(colors=PALETTE_SIZE, method=Image.Quantize.MEDIANCUT)
    # The palette image stores indexes, not colors, so convert to RGB before counting pixels.
    counts = palette.convert("RGB").getcolors()
    if not counts:
        raise InvalidImageError("corrupt", "The image has no pixels.")

    # Equal counts are ordered by color value, so the result doesn't depend on Pillow's internals.
    ranked = sorted(counts, key=lambda entry: (-entry[0], entry[1]))
    kept_lab: list[Lab] = []
    kept_hex: list[str] = []
    for _, rgb in ranked:
        lab = rgb_to_lab(rgb)
        if any(delta_e76(lab, kept) < MIN_DELTA_E for kept in kept_lab):
            continue
        kept_lab.append(lab)
        kept_hex.append(rgb_to_hex(rgb))
        if len(kept_hex) == k:
            break
    return tuple(kept_hex)


def rgb_to_lab(rgb: Rgb) -> Lab:
    """Convert sRGB to CIE L*a*b* under D65, the space ΔE76 is measured in."""
    red, green, blue = (_srgb_to_linear(channel / 255) for channel in rgb)
    x = 0.4124564 * red + 0.3575761 * green + 0.1804375 * blue
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = 0.0193339 * red + 0.1191920 * green + 0.9503041 * blue
    fx, fy, fz = (_lab_f(value / white) for value, white in zip((x, y, z), _D65_WHITE, strict=True))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e76(lab_a: Lab, lab_b: Lab) -> float:
    """CIE76 color difference, which is the Euclidean distance between two Lab colors."""
    return math.dist(lab_a, lab_b)


def hex_to_rgb(hex_color: str) -> Rgb:
    """Parse `#rrggbb`, in either case, into its red, green and blue channels."""
    if not _HEX_COLOR.fullmatch(hex_color):
        raise ValueError(f"not a #rrggbb color: {hex_color!r}")
    return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))


def rgb_to_hex(rgb: Rgb) -> str:
    """Format channels as lowercase `#rrggbb`."""
    if any(not 0 <= channel <= 255 for channel in rgb):
        raise ValueError(f"color channels must be 0-255: {rgb!r}")
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _srgb_to_linear(channel: float) -> float:
    """Undo the sRGB transfer curve, which is linear near black and a power curve elsewhere."""
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _lab_f(ratio: float) -> float:
    """The Lab companding function. It is a cube root, with a linear segment near zero."""
    if ratio > _EPSILON:
        return ratio ** (1 / 3)
    return (_KAPPA * ratio + 16) / 116


def _load_thumbnail(data: bytes) -> Image.Image:
    """Decode `data` into an upright RGB image that fits inside THUMBNAIL_SIZE."""
    try:
        with Image.open(BytesIO(data)) as opened:
            # Cameras store rotation in EXIF, not in the pixels, so apply it before the thumbnail.
            upright = ImageOps.exif_transpose(opened)
            rgb = _flatten_onto_white(upright)
    except Image.DecompressionBombError as error:
        raise InvalidImageError("too_many_pixels", "The image is too large to decode.") from error
    except (OSError, SyntaxError, ValueError) as error:
        raise InvalidImageError("corrupt", "The image can't be decoded.") from error
    rgb.thumbnail(THUMBNAIL_SIZE)
    return rgb


def _flatten_onto_white(image: Image.Image) -> Image.Image:
    """Return an RGB copy of `image`, with any transparency shown as white."""
    if not image.has_transparency_data:
        return image.convert("RGB")
    white = Image.new("RGBA", image.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, image.convert("RGBA")).convert("RGB")
