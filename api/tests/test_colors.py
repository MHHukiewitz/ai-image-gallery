import random
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from PIL.ExifTags import Base

from gallery.domain.colors import (
    MIN_DELTA_E,
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
