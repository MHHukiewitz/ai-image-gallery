import pytest

from gallery.domain.colors import delta_e76, hex_to_rgb, rgb_to_hex, rgb_to_lab


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
