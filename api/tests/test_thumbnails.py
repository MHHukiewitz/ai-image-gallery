import random
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from PIL.ExifTags import Base

from gallery.domain.errors import InvalidImageError
from gallery.domain.thumbnails import THUMBNAIL_SIZE, make_thumbnail

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
# WebP is lossy, so a flat color comes back close to itself, not exactly.
TOLERANCE = 12


def encode(image: Image.Image, image_format: str = "PNG", **options: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def decode(thumbnail: bytes) -> Image.Image:
    image = Image.open(BytesIO(thumbnail))
    image.load()
    return image


def bands(
    size: tuple[int, int], colors: list[tuple[int, int, int]], *, vertical: bool
) -> Image.Image:
    """An image cut into equal bands of flat color, top to bottom or left to right."""
    image = Image.new("RGB", size)
    length = size[1] if vertical else size[0]
    for index, color in enumerate(colors):
        start, end = length * index // len(colors), length * (index + 1) // len(colors)
        box = (0, start, size[0], end) if vertical else (start, 0, end, size[1])
        image.paste(color, box)
    return image


def assert_color(pixel: Any, expected: tuple[int, int, int]) -> None:
    assert pixel == pytest.approx(expected, abs=TOLERANCE)


def test_the_thumbnail_is_a_300_by_300_webp() -> None:
    thumbnail = decode(make_thumbnail(encode(Image.new("RGB", (800, 600), RED))))

    assert thumbnail.format == "WEBP"
    assert thumbnail.size == (300, 300)
    assert THUMBNAIL_SIZE == 300


def test_the_thumbnail_has_no_alpha_channel() -> None:
    assert decode(make_thumbnail(encode(Image.new("RGBA", (50, 50), RED + (255,))))).mode == "RGB"


@pytest.mark.parametrize("size", [(800, 600), (600, 800), (300, 300), (64, 32), (3000, 40)])
def test_any_shape_comes_out_square(size: tuple[int, int]) -> None:
    assert decode(make_thumbnail(encode(Image.new("RGB", size, RED)))).size == (300, 300)


def test_a_custom_size_is_honored() -> None:
    assert decode(make_thumbnail(encode(Image.new("RGB", (200, 100))), size=64)).size == (64, 64)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_both_accepted_formats_are_supported(image_format: str) -> None:
    thumbnail = decode(make_thumbnail(encode(Image.new("RGB", (120, 90), BLUE), image_format)))

    assert_color(thumbnail.getpixel((150, 150)), BLUE)


def test_a_portrait_image_is_cropped_to_its_center() -> None:
    # Three equal bands, 100 high each. The center square is the middle one.
    portrait = bands((100, 300), [RED, GREEN, BLUE], vertical=True)

    thumbnail = decode(make_thumbnail(encode(portrait)))

    for point in [(150, 5), (150, 150), (150, 294), (5, 5), (294, 294)]:
        assert_color(thumbnail.getpixel(point), GREEN)


def test_a_landscape_image_is_cropped_to_its_center() -> None:
    landscape = bands((300, 100), [RED, GREEN, BLUE], vertical=False)

    thumbnail = decode(make_thumbnail(encode(landscape)))

    for point in [(5, 150), (150, 150), (294, 150), (5, 5), (294, 294)]:
        assert_color(thumbnail.getpixel(point), GREEN)


def test_a_small_image_is_scaled_up_to_fill_the_square() -> None:
    thumbnail = decode(make_thumbnail(encode(bands((20, 20), [RED, BLUE], vertical=False))))

    assert thumbnail.size == (300, 300)
    assert_color(thumbnail.getpixel((10, 150)), RED)
    assert_color(thumbnail.getpixel((290, 150)), BLUE)


def test_transparent_pixels_become_white() -> None:
    # Left half opaque red. Right half fully transparent, with black stored underneath.
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    image.paste(RED + (255,), (0, 0, 50, 100))

    thumbnail = decode(make_thumbnail(encode(image)))

    assert_color(thumbnail.getpixel((20, 150)), RED)
    assert_color(thumbnail.getpixel((280, 150)), WHITE)


def test_a_fully_transparent_png_is_a_white_square() -> None:
    thumbnail = decode(make_thumbnail(encode(Image.new("RGBA", (40, 40), (10, 20, 30, 0)))))

    assert_color(thumbnail.getpixel((150, 150)), WHITE)
    assert_color(thumbnail.getpixel((0, 0)), WHITE)


def test_half_transparent_pixels_blend_with_white() -> None:
    thumbnail = decode(make_thumbnail(encode(Image.new("RGBA", (40, 40), (0, 0, 0, 128)))))

    assert_color(thumbnail.getpixel((150, 150)), (127, 127, 127))


def test_a_palette_png_with_a_transparent_color_gets_a_white_background() -> None:
    image = Image.new("P", (40, 40), 0)
    image.putpalette([255, 0, 0, 0, 0, 255])
    image.paste(1, (0, 0, 20, 40))
    # Index 0, the red half, is the transparent one.
    data = encode(image, transparency=0)

    thumbnail = decode(make_thumbnail(data))

    assert_color(thumbnail.getpixel((280, 150)), WHITE)
    assert_color(thumbnail.getpixel((20, 150)), BLUE)


def test_a_grayscale_png_with_alpha_gets_a_white_background() -> None:
    thumbnail = decode(make_thumbnail(encode(Image.new("LA", (40, 40), (0, 0)))))

    assert_color(thumbnail.getpixel((150, 150)), WHITE)


@pytest.mark.parametrize("mode", ["L", "1", "I;16", "P"])
def test_other_color_modes_are_supported(mode: str) -> None:
    assert decode(make_thumbnail(encode(Image.new(mode, (40, 40))))).size == (300, 300)


def test_a_16_bit_grayscale_png_keeps_its_brightness() -> None:
    # Mid-gray in 16 bits. Clipping instead of scaling would make it white.
    image = Image.new("I;16", (40, 40))
    image.putdata([60000 if x < 20 else 30000 for _ in range(40) for x in range(40)])

    thumbnail = decode(make_thumbnail(encode(image)))

    assert_color(thumbnail.getpixel((20, 150)), (234, 234, 234))
    assert_color(thumbnail.getpixel((280, 150)), (117, 117, 117))


def test_a_cmyk_jpeg_is_supported() -> None:
    assert decode(make_thumbnail(encode(Image.new("CMYK", (40, 40)), "JPEG"))).mode == "RGB"


def test_an_exif_rotated_jpeg_is_turned_upright() -> None:
    # Stored as left red, right blue, with orientation 6: display it turned a quarter clockwise.
    # Upright, red is then on top and blue below.
    exif = Image.Exif()
    exif[Base.Orientation] = 6
    stored = bands((200, 100), [RED, BLUE], vertical=False)

    thumbnail = decode(make_thumbnail(encode(stored, "JPEG", exif=exif)))

    assert_color(thumbnail.getpixel((150, 20)), RED)
    assert_color(thumbnail.getpixel((150, 280)), BLUE)


def test_the_original_bytes_are_not_changed() -> None:
    data = encode(Image.new("RGB", (50, 50)))
    copy = bytes(data)

    make_thumbnail(data)

    assert data == copy


def test_the_same_input_gives_the_same_thumbnail() -> None:
    data = encode(Image.new("RGB", (120, 90), GREEN))

    assert make_thumbnail(data) == make_thumbnail(data)


def noisy_png() -> bytes:
    rng = random.Random(0)
    image = Image.new("RGB", (64, 48))
    image.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(64 * 48)]
    )
    return encode(image)


def test_bytes_that_are_not_an_image_are_corrupt() -> None:
    with pytest.raises(InvalidImageError) as excinfo:
        make_thumbnail(b"not an image")

    assert excinfo.value.code == "corrupt"


def test_a_truncated_image_is_corrupt() -> None:
    data = noisy_png()

    with pytest.raises(InvalidImageError) as excinfo:
        make_thumbnail(data[: len(data) // 2])

    assert excinfo.value.code == "corrupt"
