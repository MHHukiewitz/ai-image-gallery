import contextlib
import random
import struct
import warnings
import zlib
from io import BytesIO
from typing import Any

import pytest
from PIL import Image, ImageFile
from PIL.ExifTags import Base

from gallery.domain.errors import InvalidImageError
from gallery.domain.filenames import MAX_FILENAME_LENGTH, display_filename
from gallery.domain.thumbnails import make_thumbnail
from gallery.domain.validation import ImageInfo, inspect_image

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000


def encode(image: Image.Image, image_format: str, **options: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def noisy(size: tuple[int, int] = (64, 48)) -> Image.Image:
    """Noise that compresses badly, so a cut file loses real pixel data."""
    rng = random.Random(0)
    image = Image.new("RGB", size)
    image.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(size[0] * size[1])
        ]
    )
    return image


def inspect(data: bytes, **limits: int) -> ImageInfo:
    return inspect_image(
        data,
        max_bytes=limits.get("max_bytes", MAX_BYTES),
        max_pixels=limits.get("max_pixels", MAX_PIXELS),
    )


def rejection(data: bytes, **limits: int) -> InvalidImageError:
    with pytest.raises(InvalidImageError) as excinfo:
        inspect(data, **limits)
    return excinfo.value


def processing_rejection(data: bytes) -> InvalidImageError:
    """Run the checks an upload goes through: the header, then the thumbnail, which decodes it.

    inspect_image reads only the header, so damage in the pixel data shows up in the second step.
    """
    with pytest.raises(InvalidImageError) as excinfo:
        inspect(data)
        make_thumbnail(data)
    return excinfo.value


def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_claiming(width: int, height: int) -> bytes:
    """A PNG with a valid header that declares a canvas this big, and no pixel data behind it."""

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(b""))
        + png_chunk(b"IEND", b"")
    )


def jpeg_claiming(width: int, height: int) -> bytes:
    """A JPEG whose header declares a canvas this big. The few rows of data behind it don't fit."""
    data = bytearray(encode(Image.new("RGB", (16, 16)), "JPEG"))
    marker = data.index(b"\xff\xc0")  # SOF0: marker, length, precision, then height and width
    data[marker + 5 : marker + 9] = struct.pack(">HH", height, width)
    return bytes(data)


def with_orientation(orientation: int) -> Image.Exif:
    exif = Image.Exif()
    exif[Base.Orientation] = orientation
    return exif


# --- accepted formats ---


def test_a_jpeg_is_accepted() -> None:
    info = inspect(encode(Image.new("RGB", (40, 30), "red"), "JPEG"))

    assert info == ImageInfo(
        format="jpeg", mime_type="image/jpeg", extension="jpg", width=40, height=30
    )


def test_a_png_is_accepted() -> None:
    info = inspect(encode(Image.new("RGBA", (40, 30), "red"), "PNG"))

    assert info == ImageInfo(
        format="png", mime_type="image/png", extension="png", width=40, height=30
    )


def test_a_multi_picture_jpeg_is_accepted_as_a_jpeg() -> None:
    # Many phones write these. Pillow names the format "MPO", but the file is a JPEG.
    first, second = Image.new("RGB", (40, 30), "red"), Image.new("RGB", (40, 30), "blue")
    data = encode(first, "MPO", save_all=True, append_images=[second])

    assert inspect(data).format == "jpeg"


@pytest.mark.parametrize(
    "image",
    [
        Image.new("L", (8, 8), 100),
        Image.new("1", (8, 8)),
        Image.new("P", (8, 8)),
        Image.new("LA", (8, 8)),
        Image.new("I;16", (8, 8), 1000),
    ],
    ids=lambda image: image.mode,
)
def test_png_color_modes_are_accepted(image: Image.Image) -> None:
    assert inspect(encode(image, "PNG")).format == "png"


def test_a_cmyk_jpeg_is_accepted() -> None:
    assert inspect(encode(Image.new("CMYK", (8, 8)), "JPEG")).format == "jpeg"


# --- format sniffing ---


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "BMP", "TIFF"])
def test_other_image_formats_are_unsupported(image_format: str) -> None:
    error = rejection(encode(Image.new("RGB", (8, 8)), image_format))

    assert error.code == "unsupported_type"


def test_a_text_file_named_like_a_jpeg_is_unsupported() -> None:
    error = rejection(b"This is not an image, whatever its name says.")

    assert error.code == "unsupported_type"


def test_an_empty_file_is_unsupported() -> None:
    assert rejection(b"").code == "unsupported_type"


def test_an_svg_is_unsupported() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    assert rejection(svg).code == "unsupported_type"


# --- size limits ---


def test_a_file_over_the_byte_limit_is_too_large() -> None:
    data = encode(noisy(), "PNG")

    error = rejection(data, max_bytes=len(data) - 1)

    assert error.code == "too_large"


def test_a_file_exactly_at_the_byte_limit_is_accepted() -> None:
    data = encode(noisy(), "PNG")

    assert inspect(data, max_bytes=len(data)).format == "png"


def test_the_size_is_checked_before_the_content() -> None:
    # Junk over the limit is "too large", not "unsupported": no decoder ever sees it.
    assert rejection(b"x" * 101, max_bytes=100).code == "too_large"


def test_a_file_with_too_many_pixels_is_rejected() -> None:
    data = encode(Image.new("RGB", (100, 100)), "PNG")

    error = rejection(data, max_pixels=100 * 100 - 1)

    assert error.code == "too_many_pixels"


def test_a_file_exactly_at_the_pixel_limit_is_accepted() -> None:
    data = encode(Image.new("RGB", (100, 100)), "PNG")

    assert inspect(data, max_pixels=100 * 100).width == 100


def test_a_huge_dimension_jpeg_is_rejected_without_decoding_it() -> None:
    # 100 million pixels is above the limit and above Pillow's warning threshold, in 600 bytes.
    data = jpeg_claiming(10_000, 10_000)

    assert len(data) < 1000
    assert rejection(data).code == "too_many_pixels"


def test_a_jpeg_beyond_pillows_hard_limit_is_rejected() -> None:
    assert rejection(jpeg_claiming(65_535, 65_535), max_pixels=10**12).code == "too_many_pixels"


def test_a_huge_dimension_png_is_rejected_without_decoding_it() -> None:
    # 200 million pixels, in a file of a few dozen bytes.
    data = png_claiming(20_000, 10_000)

    assert len(data) < 100
    assert rejection(data).code == "too_many_pixels"


def test_a_decompression_bomb_is_rejected_even_when_the_pixel_limit_allows_it() -> None:
    # 120 million pixels trips Pillow's own warning. It must become an error, not a log line.
    data = encode(Image.new("1", (12_000, 10_000)), "PNG")

    error = rejection(data, max_pixels=10**9)

    assert error.code == "too_many_pixels"


def test_a_decompression_bomb_beyond_pillows_hard_limit_is_rejected() -> None:
    data = png_claiming(100_000, 100_000)

    assert rejection(data, max_pixels=10**12).code == "too_many_pixels"


def test_the_bomb_warning_does_not_stay_an_error_after_the_call() -> None:
    bomb = encode(Image.new("1", (12_000, 10_000)), "PNG")
    rejection(bomb, max_pixels=10**9)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Image.open(BytesIO(bomb))

    assert [warning.category for warning in caught] == [Image.DecompressionBombWarning]


# --- damaged files ---


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_a_truncated_file_is_corrupt(image_format: str) -> None:
    data = encode(noisy(), image_format)

    error = processing_rejection(data[: len(data) // 2])

    assert error.code == "corrupt"


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_a_file_cut_just_after_its_signature_is_corrupt(image_format: str) -> None:
    assert rejection(encode(noisy(), image_format)[:12]).code == "corrupt"


def test_a_png_with_a_damaged_chunk_is_corrupt() -> None:
    data = bytearray(encode(noisy(), "PNG"))
    middle = len(data) // 2
    data[middle : middle + 16] = bytes(16)

    assert processing_rejection(bytes(data)).code == "corrupt"


def test_a_png_without_pixel_data_is_corrupt() -> None:
    # A well-formed header with an empty image stream.
    assert processing_rejection(png_claiming(10, 10)).code == "corrupt"


def test_flipping_bytes_never_escapes_as_anything_but_a_domain_error() -> None:
    rng = random.Random(1)
    for image_format in ("JPEG", "PNG"):
        original = encode(noisy(), image_format)
        for _ in range(60):
            data = bytearray(original)
            for position in rng.sample(range(len(data)), 4):
                data[position] = rng.randrange(256)
            # Any other exception escaping would fail the test.
            with contextlib.suppress(InvalidImageError):
                inspect(bytes(data))
                make_thumbnail(bytes(data))


# --- header only ---


@pytest.fixture
def no_pixels_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: ImageFile.ImageFile, *args: Any) -> None:
        raise AssertionError("inspect_image decoded the pixels")

    monkeypatch.setattr(ImageFile.ImageFile, "load", refuse)


@pytest.mark.usefixtures("no_pixels_decoded")
@pytest.mark.parametrize(
    "data",
    [
        encode(Image.new("RGB", (40, 30)), "JPEG"),
        encode(Image.new("RGB", (40, 30)), "JPEG", exif=with_orientation(6)),
        encode(Image.new("RGB", (40, 30)), "PNG"),
        encode(Image.new("RGBA", (40, 30)), "PNG", exif=with_orientation(6)),
        encode(
            Image.new("RGB", (40, 30)),
            "MPO",
            save_all=True,
            append_images=[Image.new("RGB", (40, 30))],
        ),
    ],
    ids=["jpeg", "rotated-jpeg", "png", "rotated-png", "mpo"],
)
def test_inspecting_an_image_decodes_no_pixels(data: bytes) -> None:
    assert inspect(data).width in (30, 40)


def test_damage_after_the_header_is_left_for_the_thumbnail_step() -> None:
    # The header says what the file claims to be. Whether the pixels are there is the next step's
    # question, and it answers `corrupt` (see test_a_truncated_file_is_corrupt).
    data = encode(noisy(), "JPEG")

    assert inspect(data[: len(data) // 2]).format == "jpeg"


@pytest.mark.usefixtures("no_pixels_decoded")
def test_a_png_with_its_exif_after_the_pixel_data_reports_its_upright_size() -> None:
    # The spec lets eXIf follow IDAT. Pillow only finds it there by decoding the image.
    png = encode(Image.new("RGB", (40, 30)), "PNG")
    end = png.rindex(png_chunk(b"IEND", b""))
    data = png[:end] + png_chunk(b"eXIf", with_orientation(6).tobytes()) + png[end:]

    info = inspect(data)

    assert (info.width, info.height) == (30, 40)


def test_a_png_with_a_damaged_late_exif_chunk_is_corrupt() -> None:
    # The same verdict as for a damaged EXIF block anywhere else in a file.
    png = encode(Image.new("RGB", (40, 30)), "PNG")
    end = png.rindex(png_chunk(b"IEND", b""))
    data = png[:end] + png_chunk(b"eXIf", b"\xff" * 5) + png[end:]

    assert rejection(data).code == "corrupt"


# --- orientation ---


def test_an_exif_rotated_jpeg_reports_its_upright_size() -> None:
    # Stored 40 wide and 30 high, with a quarter turn to apply when displaying.
    data = encode(Image.new("RGB", (40, 30)), "JPEG", exif=with_orientation(6))

    info = inspect(data)

    assert (info.width, info.height) == (30, 40)


@pytest.mark.parametrize(
    ("orientation", "swapped"),
    [(1, False), (2, False), (3, False), (4, False), (5, True), (6, True), (7, True), (8, True)],
)
def test_only_quarter_turn_orientations_swap_width_and_height(
    orientation: int, swapped: bool
) -> None:
    data = encode(Image.new("RGB", (40, 30)), "JPEG", exif=with_orientation(orientation))

    info = inspect(data)

    assert (info.width, info.height) == ((30, 40) if swapped else (40, 30))


def test_an_exif_rotated_png_reports_its_upright_size() -> None:
    data = encode(Image.new("RGB", (40, 30)), "PNG", exif=with_orientation(8))

    info = inspect(data)

    assert (info.width, info.height) == (30, 40)


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [(3, (4032, 3024)), (6, (3024, 4032)), (8, (3024, 4032))],
)
def test_a_12_megapixel_phone_photo_reports_its_upright_size(
    orientation: int, expected: tuple[int, int]
) -> None:
    data = encode(Image.new("RGB", (4032, 3024)), "JPEG", exif=with_orientation(orientation))

    info = inspect(data)

    assert (info.width, info.height) == expected


def test_the_pixel_limit_counts_the_stored_pixels_either_way_up() -> None:
    data = encode(Image.new("RGB", (40, 30)), "JPEG", exif=with_orientation(6))

    assert inspect(data, max_pixels=1200).width == 30
    assert rejection(data, max_pixels=1199).code == "too_many_pixels"


# --- display_filename ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("beach.jpg", "beach.jpg"),
        ("my holiday photo (1).PNG", "my holiday photo (1).PNG"),
        ("Fotó café 夏.jpg", "Fotó café 夏.jpg"),
        ("family 👨‍👩‍👧.png", "family 👨‍👩‍👧.png"),
        ("../../etc/passwd", "passwd"),
        ("/var/tmp/cat.png", "cat.png"),
        ("C:\\Users\\Me\\Pictures\\cat.png", "cat.png"),
        ("mixed/..\\sep/cat.png", "cat.png"),
        ("  padded.png  ", "padded.png"),
        ("tab\tand\nnewline.png", "tabandnewline.png"),
        ("nul\x00byte.png", "nulbyte.png"),
        ("escape\x1b[31m.png", "escape[31m.png"),
        ("photo\u202egpj.exe", "photogpj.exe"),
    ],
)
def test_display_filename_keeps_a_clean_basename(raw: str, expected: str) -> None:
    assert display_filename(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "/", "folder/", "..", ".", "a/..", "\x00\x01", "\n"]
)
def test_display_filename_falls_back_when_nothing_usable_is_left(raw: str | None) -> None:
    assert display_filename(raw) == "image"


def test_display_filename_drops_lone_surrogates() -> None:
    # They can't be encoded as UTF-8, so they would fail the insert.
    cleaned = display_filename("bad\ud800name.png")

    assert cleaned == "badname.png"
    cleaned.encode("utf-8")


def test_display_filename_caps_the_length_and_keeps_the_extension() -> None:
    name = display_filename("a" * 400 + ".jpeg")

    assert len(name) == MAX_FILENAME_LENGTH
    assert name == "a" * (MAX_FILENAME_LENGTH - len(".jpeg")) + ".jpeg"


def test_display_filename_caps_a_long_name_without_an_extension() -> None:
    assert display_filename("b" * 400) == "b" * MAX_FILENAME_LENGTH


def test_display_filename_caps_a_long_name_with_a_very_long_extension() -> None:
    assert len(display_filename("c." + "d" * 400)) == MAX_FILENAME_LENGTH


def test_display_filename_leaves_a_name_of_exactly_the_limit_alone() -> None:
    name = "e" * (MAX_FILENAME_LENGTH - 4) + ".png"

    assert display_filename(name) == name
