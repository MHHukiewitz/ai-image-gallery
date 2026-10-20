import random
from collections.abc import Callable
from functools import cache
from io import BytesIO
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps
from PIL.ExifTags import Base

from gallery.domain.colors import (
    MIN_DELTA_E,
    THUMBNAIL_SIZE,
    _flatten_onto_white,
    _load_thumbnail,
    delta_e76,
    dominant_colors,
    hex_to_rgb,
    rgb_to_hex,
    rgb_to_lab,
)
from gallery.domain.errors import InvalidImageError


def encode(image: Image.Image, image_format: str = "PNG", **options: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def noise_image() -> Image.Image:
    """A busy palette from a fixed seed, so the test is repeatable but not trivially uniform."""
    rng = random.Random(0)
    image = Image.new("RGB", (128, 128))
    image.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(128 * 128)]
    )
    return image


def test_white_is_lab_100_0_0() -> None:
    assert rgb_to_lab((255, 255, 255)) == pytest.approx((100, 0, 0), abs=0.5)


def test_black_is_lab_0_0_0() -> None:
    assert rgb_to_lab((0, 0, 0)) == pytest.approx((0, 0, 0), abs=0.5)


def test_pure_red_matches_the_commonly_cited_d65_value() -> None:
    assert rgb_to_lab((255, 0, 0)) == pytest.approx((53.24, 80.09, 67.20), abs=0.5)


def test_delta_e76_is_the_euclidean_distance_in_either_direction() -> None:
    assert delta_e76((50, 0, 0), (50, 3, 4)) == pytest.approx(5.0)
    assert delta_e76((50, 3, 4), (50, 0, 0)) == pytest.approx(5.0)


def test_delta_e76_of_a_color_with_itself_is_zero() -> None:
    lab = rgb_to_lab((12, 34, 56))

    assert delta_e76(lab, lab) == 0


@pytest.mark.parametrize(
    ("hex_color", "rgb"),
    [("#ff8800", (255, 136, 0)), ("#FF8800", (255, 136, 0)), ("#000000", (0, 0, 0))],
)
def test_hex_to_rgb_accepts_either_case(hex_color: str, rgb: tuple[int, int, int]) -> None:
    assert hex_to_rgb(hex_color) == rgb


def test_rgb_to_hex_is_lowercase_and_zero_padded() -> None:
    assert rgb_to_hex((255, 136, 0)) == "#ff8800"
    assert rgb_to_hex((0, 0, 10)) == "#00000a"


@pytest.mark.parametrize("hex_color", ["ff8800", "#fff", "#gg0000", "#ff88001"])
def test_hex_to_rgb_rejects_malformed_colors(hex_color: str) -> None:
    with pytest.raises(ValueError):
        hex_to_rgb(hex_color)


@pytest.mark.parametrize("rgb", [(256, 0, 0), (0, -1, 0)])
def test_rgb_to_hex_rejects_channels_outside_0_to_255(rgb: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        rgb_to_hex(rgb)


def test_solid_red_is_one_color_within_two_per_channel() -> None:
    colors = dominant_colors(encode(Image.new("RGB", (64, 64), (255, 0, 0))))

    assert len(colors) == 1
    actual = hex_to_rgb(colors[0])
    assert all(abs(got - want) <= 2 for got, want in zip(actual, (255, 0, 0), strict=True))


def test_seventy_percent_blue_comes_before_thirty_percent_yellow() -> None:
    image = Image.new("RGB", (100, 100), (0, 0, 255))
    image.paste((255, 255, 0), (0, 70, 100, 100))

    assert dominant_colors(encode(image)) == ("#0000ff", "#ffff00")


def test_near_identical_reds_collapse_to_the_more_frequent_one() -> None:
    # Premise: these two reds are close enough that the palette would otherwise keep both.
    assert delta_e76(rgb_to_lab((255, 0, 0)), rgb_to_lab((250, 10, 10))) < MIN_DELTA_E

    image = Image.new("RGB", (100, 100), (255, 0, 0))
    image.paste((250, 10, 10), (0, 50, 100, 80))
    image.paste((0, 255, 0), (0, 80, 100, 100))

    assert dominant_colors(encode(image)) == ("#ff0000", "#00ff00")


def test_transparent_padding_composites_onto_white() -> None:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (16, 16, 48, 48))

    assert dominant_colors(encode(image)) == ("#ffffff", "#ff0000")


def test_the_same_bytes_always_give_the_same_colors() -> None:
    data = encode(noise_image())

    results = {dominant_colors(data) for _ in range(5)}

    assert len(results) == 1
    assert len(results.pop()) == 3


def test_k_limits_the_result_to_the_most_frequent_colors() -> None:
    data = encode(noise_image())
    all_three = dominant_colors(data)

    assert dominant_colors(data, k=1) == all_three[:1]
    assert dominant_colors(data, k=2) == all_three[:2]


def test_k_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        dominant_colors(encode(noise_image()), k=0)


@pytest.mark.parametrize("data", [b"", b"definitely not an image"])
def test_bytes_that_are_not_an_image_raise_corrupt(data: bytes) -> None:
    with pytest.raises(InvalidImageError) as raised:
        dominant_colors(data)

    assert raised.value.code == "corrupt"


def test_a_truncated_image_raises_corrupt() -> None:
    data = encode(noise_image())

    with pytest.raises(InvalidImageError) as raised:
        dominant_colors(data[: len(data) // 2])

    assert raised.value.code == "corrupt"


def test_an_image_with_no_pixels_raises_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decoding never produces an empty thumbnail from a real file, so stub the decoder.
    monkeypatch.setattr(
        "gallery.domain.colors._load_thumbnail", lambda data: Image.new("RGB", (0, 0))
    )

    with pytest.raises(InvalidImageError) as raised:
        dominant_colors(b"stubbed")

    assert raised.value.code == "corrupt"


def test_an_image_over_pillow_s_pixel_limit_raises_too_many_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = encode(Image.new("RGB", (64, 64), (1, 2, 3)))
    # Pillow refuses to open an image with more than twice MAX_IMAGE_PIXELS.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(InvalidImageError) as raised:
        dominant_colors(data)

    assert raised.value.code == "too_many_pixels"


def test_exif_rotation_is_applied_before_the_thumbnail() -> None:
    # 200x100 pixels, tagged so that cameras display it rotated 90 degrees.
    exif = Image.Exif()
    exif[Base.Orientation] = 6
    data = encode(Image.new("RGB", (200, 100), (0, 0, 255)), "JPEG", exif=exif.tobytes())

    thumbnail = _load_thumbnail(data)

    assert thumbnail.size == (64, 128)


# --- photos decoded at reduced size ---

# Fractions of the height, top to bottom, and the color of each band. Real scenes, not noise: a
# quantizer is chaotic on noise, so only well-separated regions give a repeatable palette.
SEASIDE = [(0.25, (120, 170, 230)), (0.40, (20, 80, 150)), (0.35, (225, 200, 150))]
FOREST = [(0.22, (200, 210, 220)), (0.48, (30, 90, 40)), (0.30, (110, 75, 45))]
SUNSET = [(0.50, (240, 140, 60)), (0.30, (150, 50, 90)), (0.20, (25, 25, 60))]
MAX_DELTA_E = 3


@cache
def scene(size: tuple[int, int], bands: tuple[tuple[float, tuple[int, int, int]], ...]) -> bytes:
    """A JPEG of colored bands, shaded top to bottom and speckled like sensor noise."""
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    top = 0
    for fraction, color in bands:
        bottom = top + round(size[1] * fraction)
        draw.rectangle((0, top, size[0], bottom), fill=color)
        top = bottom
    shade = Image.linear_gradient("L").resize(size).convert("RGB")
    image = ImageChops.blend(image, ImageChops.multiply(image, shade), 0.25)
    grain = Image.frombytes("L", size, random.Random(0).randbytes(size[0] * size[1]))
    return encode(ImageChops.blend(image, grain.convert("RGB"), 0.08), "JPEG", quality=88)


def tagged(data: bytes, orientation: int) -> bytes:
    """`data` with the EXIF orientation set, the way a phone records how it was held."""
    exif = Image.Exif()
    exif[Base.Orientation] = orientation
    with Image.open(BytesIO(data)) as image:
        return encode(image, "JPEG", quality=88, exif=exif)


def full_size_thumbnail(data: bytes) -> Image.Image:
    """The thumbnail loader as it was before the reduced-size decode."""
    with Image.open(BytesIO(data)) as opened:
        rgb = _flatten_onto_white(ImageOps.exif_transpose(opened))
    rgb.thumbnail(THUMBNAIL_SIZE)
    return rgb


@pytest.fixture
def colors_of_full_size_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[bytes], tuple[str, ...]]:
    """dominant_colors with the old loader: the same ranking, fed by a full-size decode."""

    def compute(data: bytes) -> tuple[str, ...]:
        with monkeypatch.context() as patch:
            patch.setattr("gallery.domain.colors._load_thumbnail", full_size_thumbnail)
            return dominant_colors(data)

    return compute


PHOTOS = {
    "seaside, 6 MP, rotated": lambda: tagged(scene((3000, 2000), tuple(SEASIDE)), 6),
    "forest, 12 MP, rotated": lambda: tagged(scene((4032, 3024), tuple(FOREST)), 8),
    "forest, 12 MP, upside down": lambda: tagged(scene((4032, 3024), tuple(FOREST)), 3),
    "sunset, 5 MP": lambda: scene((2560, 1920), tuple(SUNSET)),
}


@pytest.mark.parametrize("photo", PHOTOS.values(), ids=PHOTOS.keys())
def test_a_reduced_decode_gives_the_same_colors_within_delta_e_3(
    photo: Callable[[], bytes], colors_of_full_size_decode: Callable[[bytes], tuple[str, ...]]
) -> None:
    data = photo()

    before, after = colors_of_full_size_decode(data), dominant_colors(data)

    assert len(after) == len(before) == 3
    for old, new in zip(before, after, strict=True):
        assert delta_e76(rgb_to_lab(hex_to_rgb(old)), rgb_to_lab(hex_to_rgb(new))) < MAX_DELTA_E


def test_a_12_megapixel_jpeg_is_decoded_at_an_eighth_of_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # exif_transpose is the first step that loads pixels, so the size it sees is the decoded size.
    decoded_sizes: list[tuple[int, int]] = []
    transpose = ImageOps.exif_transpose

    def spy(image: Image.Image, **options: Any) -> Image.Image:
        decoded_sizes.append(image.size)
        return transpose(image, **options)

    monkeypatch.setattr(ImageOps, "exif_transpose", spy)

    thumbnail = _load_thumbnail(tagged(scene((4032, 3024), tuple(FOREST)), 6))

    [(width, height)] = decoded_sizes
    assert width <= 4032 // 8
    assert height <= 3024 // 8
    # Rotated a quarter turn, so the thumbnail is portrait, and still no larger than 128 a side.
    assert thumbnail.size == (96, 128)


def test_a_truncated_12_megapixel_jpeg_raises_corrupt() -> None:
    data = scene((4032, 3024), tuple(FOREST))

    with pytest.raises(InvalidImageError) as raised:
        dominant_colors(data[: len(data) // 2])

    assert raised.value.code == "corrupt"
