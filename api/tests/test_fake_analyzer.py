import io

import pytest
from PIL import Image

from gallery.adapters.analyzers import build_analyzer
from gallery.adapters.analyzers.fake import FakeAnalyzer
from gallery.config import Settings
from gallery.domain.errors import AnalyzerError
from gallery.domain.tags import MIN_TAGS


def image_bytes(
    color: tuple[int, int, int] = (255, 0, 0),
    size: tuple[int, int] = (64, 64),
    fmt: str = "PNG",
) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt)
    return out.getvalue()


async def test_red_square_is_tagged_red_and_square() -> None:
    analysis = await FakeAnalyzer().analyze(image_bytes((255, 0, 0), (64, 64)), "image/png")

    assert analysis.tags == ("red", "square", "bright", "sample", "test")
    assert analysis.description == "A square bright picture dominated by red tones."


async def test_same_bytes_give_the_same_output() -> None:
    data = image_bytes((20, 120, 200), (80, 40))

    first = await FakeAnalyzer().analyze(data, "image/png")
    second = await FakeAnalyzer().analyze(data, "image/png")
    third = await FakeAnalyzer().analyze(data, "image/png")

    assert first == second == third


async def test_usage_is_none() -> None:
    analysis = await FakeAnalyzer().analyze(image_bytes(), "image/png")

    assert analysis.usage is None


async def test_returns_at_least_the_minimum_number_of_tags() -> None:
    analysis = await FakeAnalyzer().analyze(image_bytes(), "image/png")

    assert len(analysis.tags) == 5 >= MIN_TAGS
    assert len(set(analysis.tags)) == len(analysis.tags)


def test_analyzer_id_has_provider_and_version() -> None:
    assert FakeAnalyzer().analyzer_id == "fake:v1"


@pytest.mark.parametrize(
    ("color", "hue", "brightness"),
    [
        ((255, 0, 0), "red", "bright"),
        ((255, 140, 0), "orange", "bright"),
        ((255, 255, 0), "yellow", "bright"),
        ((0, 200, 0), "green", "bright"),
        ((0, 0, 255), "blue", "bright"),
        ((20, 120, 200), "blue", "bright"),
        ((150, 0, 200), "purple", "bright"),
        ((255, 105, 180), "pink", "bright"),
        ((255, 192, 203), "pink", "bright"),
        ((128, 128, 128), "gray", "bright"),
        ((90, 90, 90), "gray", "dark"),
        ((0, 0, 0), "black", "dark"),
        ((255, 255, 255), "white", "bright"),
        ((100, 0, 0), "red", "dark"),
    ],
)
async def test_names_the_dominant_hue_and_brightness(
    color: tuple[int, int, int], hue: str, brightness: str
) -> None:
    analysis = await FakeAnalyzer().analyze(image_bytes(color), "image/png")

    assert analysis.tags[0] == hue
    assert analysis.tags[2] == brightness


@pytest.mark.parametrize(
    ("size", "orientation"),
    [((100, 40), "landscape"), ((40, 100), "portrait"), ((50, 50), "square")],
)
async def test_names_the_orientation(size: tuple[int, int], orientation: str) -> None:
    analysis = await FakeAnalyzer().analyze(image_bytes(size=size), "image/png")

    assert analysis.tags[1] == orientation
    assert orientation in analysis.description


async def test_dominant_hue_wins_over_a_smaller_patch() -> None:
    image = Image.new("RGB", (64, 64), (0, 0, 255))
    image.paste((255, 0, 0), (0, 0, 64, 16))
    out = io.BytesIO()
    image.save(out, format="PNG")

    analysis = await FakeAnalyzer().analyze(out.getvalue(), "image/png")

    assert analysis.tags[0] == "blue"


async def test_accepts_other_formats_than_png() -> None:
    analysis = await FakeAnalyzer().analyze(image_bytes(fmt="JPEG"), "image/jpeg")

    assert analysis.tags[:2] == ("red", "square")


async def test_fail_mode_raises_a_non_retryable_error() -> None:
    with pytest.raises(AnalyzerError) as raised:
        await FakeAnalyzer(fail=True).analyze(image_bytes(), "image/png")

    assert raised.value.retryable is False


async def test_unreadable_bytes_raise_a_non_retryable_error() -> None:
    with pytest.raises(AnalyzerError) as raised:
        await FakeAnalyzer().analyze(b"not an image", "image/png")

    assert raised.value.retryable is False


def test_factory_builds_the_fake_analyzer() -> None:
    analyzer = build_analyzer(Settings(ANALYZER="fake"))

    assert isinstance(analyzer, FakeAnalyzer)
    assert analyzer.analyzer_id == "fake:v1"


def test_factory_defaults_to_the_fake_analyzer(settings: Settings) -> None:
    assert isinstance(build_analyzer(settings), FakeAnalyzer)


def test_factory_leaves_the_anthropic_adapter_to_ticket_31() -> None:
    with pytest.raises(NotImplementedError, match="#31"):
        build_analyzer(Settings(ANALYZER="anthropic", ANTHROPIC_API_KEY="test-key"))
