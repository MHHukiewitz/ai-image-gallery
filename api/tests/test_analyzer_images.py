"""The vision-model image prep on phone-sized photos: decoded at reduced size, same result."""

import io
from functools import cache
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageOps, ImageStat
from PIL.ExifTags import Base

from gallery.adapters.analyzers import images
from gallery.adapters.analyzers.images import VISION_MAX_SIDE, normalize_image
from gallery.domain.errors import AnalyzerError

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)
# JPEG is lossy, so a flat color comes back close to itself, not exactly.
TOLERANCE = 12
# Where to sample the upright image: near the top left, top right, bottom left and bottom right.
CORNER_FRACTIONS = [(0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)]


@cache
def photo(size: tuple[int, int] = (4032, 3024), orientation: int | None = None) -> bytes:
    """A JPEG as stored: red, green, blue and yellow quadrants, then the EXIF rotation."""
    image = Image.new("RGB", size)
    width, height = size
    for color, box in zip(
        [RED, GREEN, BLUE, YELLOW],
        [
            (0, 0, width // 2, height // 2),
            (width // 2, 0, width, height // 2),
            (0, height // 2, width // 2, height),
            (width // 2, height // 2, width, height),
        ],
        strict=True,
    ):
        image.paste(color, box)
    exif = Image.Exif()
    if orientation is not None:
        exif[Base.Orientation] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, exif=exif)
    return buffer.getvalue()


def prepared(data: bytes) -> Image.Image:
    _, jpeg = normalize_image(data)
    image = Image.open(io.BytesIO(jpeg))
    image.load()
    return image


def full_size_normalize(data: bytes) -> Image.Image:
    """normalize_image as it was before the reduced-size decode: the whole photo, then shrunk."""
    with Image.open(io.BytesIO(data)) as source:
        image = images._flatten_to_rgb(ImageOps.exif_transpose(source))
    image.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
    return image


def test_a_12_megapixel_jpeg_is_decoded_at_half_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # exif_transpose is the first step that loads pixels, so the size it sees is the decoded size.
    decoded_sizes: list[tuple[int, int]] = []
    transpose = ImageOps.exif_transpose

    def spy(image: Image.Image, **options: Any) -> Image.Image:
        decoded_sizes.append(image.size)
        return transpose(image, **options)

    monkeypatch.setattr(ImageOps, "exif_transpose", spy)

    normalize_image(photo())

    [(width, height)] = decoded_sizes
    assert width <= 4032 // 2
    assert height <= 3024 // 2
    # Never below the target, so the final downscale doesn't have to enlarge anything.
    assert min(width, height) >= VISION_MAX_SIDE


@pytest.mark.parametrize(
    ("size", "orientation", "expected"),
    [
        ((4032, 3024), None, (1024, 768)),
        ((4032, 3024), 6, (768, 1024)),
        ((4032, 3024), 8, (768, 1024)),
        ((4032, 3024), 3, (1024, 768)),
        # A panorama is not drafted, because its short side is under the target.
        ((4000, 800), None, (1024, 205)),
        # Smaller than the target stays as it is.
        ((640, 480), 6, (480, 640)),
    ],
)
def test_the_image_is_at_most_1024_pixels_and_upright(
    size: tuple[int, int], orientation: int | None, expected: tuple[int, int]
) -> None:
    assert prepared(photo(size, orientation)).size == expected


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        (None, [RED, GREEN, BLUE, YELLOW]),
        (3, [YELLOW, BLUE, GREEN, RED]),
        (6, [BLUE, RED, YELLOW, GREEN]),
        (8, [GREEN, YELLOW, RED, BLUE]),
    ],
)
def test_a_12_megapixel_photo_is_turned_upright_after_the_reduced_decode(
    orientation: int | None, expected: list[tuple[int, int, int]]
) -> None:
    image = prepared(photo(orientation=orientation))

    for (across, down), color in zip(CORNER_FRACTIONS, expected, strict=True):
        pixel = image.getpixel((int(image.width * across), int(image.height * down)))
        assert pixel == pytest.approx(color, abs=TOLERANCE)


@pytest.mark.parametrize("orientation", [None, 6, 8])
def test_the_result_is_close_to_what_a_full_size_decode_gives(orientation: int | None) -> None:
    data = photo(orientation=orientation)

    after, before = prepared(data), full_size_normalize(data)

    assert after.size == before.size
    # Sharp quadrant edges are the hardest case, and they still differ by under 1 in 255 on average.
    assert max(ImageStat.Stat(ImageChops.difference(after, before)).mean) < 1


def test_a_cmyk_jpeg_that_is_decoded_at_reduced_size_is_still_supported() -> None:
    buffer = io.BytesIO()
    Image.new("CMYK", (2400, 2400), (0, 255, 255, 0)).save(buffer, format="JPEG")

    image = prepared(buffer.getvalue())

    assert image.mode == "RGB"
    assert image.size == (1024, 1024)
    assert image.getpixel((500, 500)) == pytest.approx(RED, abs=TOLERANCE)


def test_a_truncated_12_megapixel_jpeg_cannot_be_read() -> None:
    data = photo()

    with pytest.raises(AnalyzerError) as raised:
        normalize_image(data[: len(data) * 6 // 10])

    assert raised.value.retryable is False
