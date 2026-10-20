"""Color math for dominant colors: sRGB to CIE Lab, the ΔE76 distance, and hex formatting.

Pure functions with no I/O. The worker computes dominant colors locally, and the color search
ranks them by ΔE76 in Lab space (docs/architecture.md D6 and §7).
"""

import math
import re

type Rgb = tuple[int, int, int]
type Lab = tuple[float, float, float]

# D65 reference white, as X, Y, Z with Y = 1.
_D65_WHITE = (0.95047, 1.0, 1.08883)
# CIE constants, which give the linear segment of the Lab curve near black.
_EPSILON = 216 / 24389
_KAPPA = 24389 / 27
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


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
