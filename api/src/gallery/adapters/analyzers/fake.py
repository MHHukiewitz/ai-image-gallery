"""A deterministic ImageAnalyzer for tests and offline development. It never calls a model."""

import asyncio
import colorsys
import io
from collections import Counter

from PIL import Image

from gallery.domain.errors import AnalyzerError
from gallery.domain.models import Analysis

# Enough pixels to find the dominant color, few enough to keep decoding cheap.
_SAMPLE_SIDE = 32
# Tie-breaker: when two names cover the same number of pixels, the earlier one wins.
_HUE_NAMES = (
    "red",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "pink",
    "gray",
    "black",
    "white",
)


class FakeAnalyzer:
    """Describes an image from its pixels alone, so the same bytes always give the same result.

    The tags are the dominant hue name, the orientation, the brightness, then `sample` and
    `test`. With `fail=True` every call raises a non-retryable `AnalyzerError`.
    """

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    @property
    def analyzer_id(self) -> str:
        return "fake:v1"

    async def analyze(self, image: bytes, mime_type: str) -> Analysis:
        if self._fail:
            raise AnalyzerError("The fake analyzer was told to fail", retryable=False)
        return await asyncio.to_thread(_describe, image)


def _describe(data: bytes) -> Analysis:
    try:
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            sample = source.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise AnalyzerError("The image could not be read", retryable=False) from error
    sample.thumbnail((_SAMPLE_SIDE, _SAMPLE_SIDE))
    raw = sample.tobytes()
    pixels = list(zip(raw[0::3], raw[1::3], raw[2::3], strict=True))

    names = Counter(_hue_name(*pixel) for pixel in pixels)
    # max() keeps the first of equal candidates, so _HUE_NAMES order settles ties.
    hue = max(_HUE_NAMES, key=lambda name: names[name])
    brightness_values = [max(pixel) / 255 for pixel in pixels]
    brightness = "bright" if sum(brightness_values) / len(brightness_values) >= 0.5 else "dark"
    orientation = _orientation(width, height)

    return Analysis(
        tags=(hue, orientation, brightness, "sample", "test"),
        description=f"A {orientation} {brightness} picture dominated by {hue} tones.",
    )


def _orientation(width: int, height: int) -> str:
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def _hue_name(red: int, green: int, blue: int) -> str:
    hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    if value < 0.15:
        return "black"
    if saturation < 0.12:
        return "white" if value > 0.85 else "gray"
    degrees = hue * 360
    if degrees < 15 or degrees >= 345:
        # Light, soft reds read as pink.
        return "pink" if saturation < 0.45 and value > 0.75 else "red"
    if degrees < 45:
        return "orange"
    if degrees < 70:
        return "yellow"
    if degrees < 170:
        return "green"
    if degrees < 260:
        return "blue"
    if degrees < 300:
        return "purple"
    return "pink"
