"""strip_metadata and summarize_exif, on fixtures that Pillow builds here. No photo is committed.

Pillow is the independent reader: every check on a stripped file reads it back with Pillow, which
parses EXIF, ICC profiles, text chunks and MPO frames on its own.
"""

import contextlib
import random
import struct
import zlib
from io import BytesIO
from typing import Any

import pytest
from PIL import ExifTags, Image, ImageCms, PngImagePlugin

from gallery.domain.errors import InvalidImageError
from gallery.domain.metadata import ExifSummary, strip_metadata, summarize_exif

JPEG = "image/jpeg"
PNG = "image/png"
ORIENTATION = ExifTags.Base.Orientation
GPS_XMP = b'<x:xmpmeta><rdf:Description exif:GPSLatitude="52,31.2N"/></x:xmpmeta>'


# --- fixtures -----------------------------------------------------------------------------------


def gradient(size: tuple[int, int] = (96, 64)) -> Image.Image:
    """A different colour at every pixel, so a decoding difference anywhere would show."""
    width, height = size
    image = Image.new("RGB", size)
    image.putdata(
        [
            (x * 255 // width, y * 255 // height, (x * 7 + y * 13) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def srgb_profile() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def encode(image: Image.Image, image_format: str, **options: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def camera_exif(orientation: int = 6, *, gps: bool = True, endian: str = ">") -> Image.Exif:
    """EXIF the way a phone writes it: camera, capture time, serial number and position."""
    exif = Image.Exif()
    exif.endian = endian
    exif[ORIENTATION] = orientation
    exif[ExifTags.Base.Make] = "TestCam"
    exif[ExifTags.Base.Model] = "T-1000"
    exif[ExifTags.Base.DateTime] = "2026:10:20 12:00:00"
    exif[ExifTags.IFD.Exif] = {
        ExifTags.Base.DateTimeOriginal: "2026:10:20 12:00:00",
        ExifTags.Base.BodySerialNumber: "SN-0042",
    }
    if gps:
        exif[ExifTags.IFD.GPSInfo] = {
            ExifTags.GPS.GPSLatitudeRef: "N",
            ExifTags.GPS.GPSLatitude: (52.0, 31.0, 12.0),
            ExifTags.GPS.GPSLongitudeRef: "E",
            ExifTags.GPS.GPSLongitude: (13.0, 24.0, 36.0),
        }
    return exif


def with_exif_thumbnail(exif: Image.Exif, thumbnail: bytes) -> bytes:
    """Serialize `exif` with IFD1 pointing to a JPEG thumbnail, which Pillow doesn't write."""
    tiff = bytearray(exif.tobytes()[len(b"Exif\x00\x00") :])
    order = "<" if tiff.startswith(b"II") else ">"
    (ifd0,) = struct.unpack(order + "I", tiff[4:8])
    (count,) = struct.unpack(order + "H", tiff[ifd0 : ifd0 + 2])
    next_ifd_field = ifd0 + 2 + 12 * count
    if len(tiff) % 2:
        tiff += b"\x00"  # TIFF offsets fall on word boundaries
    ifd1 = len(tiff)
    thumbnail_at = ifd1 + 2 + 2 * 12 + 4
    tiff[next_ifd_field : next_ifd_field + 4] = struct.pack(order + "I", ifd1)
    tiff += struct.pack(order + "H", 2)
    tiff += struct.pack(order + "HHII", 0x0201, 4, 1, thumbnail_at)  # JPEGInterchangeFormat
    tiff += struct.pack(order + "HHII", 0x0202, 4, 1, len(thumbnail))  # ...Length
    tiff += struct.pack(order + "I", 0)
    return b"Exif\x00\x00" + bytes(tiff) + thumbnail


def insert_segment(jpeg: bytes, marker: int, body: bytes) -> bytes:
    """Put an application segment right after SOI, as editors do with IPTC and Ducky blocks."""
    segment = bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body
    return jpeg[:2] + segment + jpeg[2:]


def phone_jpeg(orientation: int = 6, **options: Any) -> bytes:
    """A phone photo: EXIF with GPS, camera tags and a thumbnail, an ICC profile, XMP, a comment,
    Photoshop's IPTC block (APP13) and a Ducky block (APP12)."""
    picture = gradient()
    thumbnail = encode(picture.resize((24, 16)), "JPEG")
    jpeg = encode(
        picture,
        "JPEG",
        exif=with_exif_thumbnail(camera_exif(orientation), thumbnail),
        icc_profile=srgb_profile(),
        xmp=GPS_XMP,
        comment="shot at home",
        quality=90,
        **options,
    )
    iptc = b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x0c\x1c\x02\x5a\x00\x06Berlin"
    jpeg = insert_segment(jpeg, 0xED, iptc)
    return insert_segment(jpeg, 0xEC, b"Ducky\x00\x01\x00\x04\x00\x00\x00\x50\x00\x00")


def phone_mpo() -> bytes:
    """A primary image and a second frame, both with GPS EXIF, as an MPO with its MPF index."""
    return encode(
        gradient(),
        "MPO",
        save_all=True,
        append_images=[gradient((48, 32))],
        exif=camera_exif(6),
        icc_profile=srgb_profile(),
    )


def tagged_png(orientation: int = 6) -> bytes:
    """A PNG with eXIf (GPS included), the three text chunk kinds, tIME, iCCP and pHYs."""
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "shot at home")
    info.add_text("Raw profile type iptc", "Berlin " * 20, zip=True)
    info.add_itxt("XML:com.adobe.xmp", GPS_XMP.decode())
    info.add(b"tIME", struct.pack(">HBBBBB", 2026, 10, 20, 12, 0, 0))
    return encode(
        gradient(),
        "PNG",
        pnginfo=info,
        exif=camera_exif(orientation),
        icc_profile=srgb_profile(),
        dpi=(144, 144),
    )


# --- readers ------------------------------------------------------------------------------------


def opened(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data))


def pixels(data: bytes) -> tuple[str, tuple[int, int], bytes]:
    """The decoded pixels as stored, before any EXIF rotation."""
    with opened(data) as image:
        return image.mode, image.size, image.tobytes()


def png_chunks(data: bytes) -> list[bytes]:
    kinds, position = [], 8
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        kinds.append(kind)
        position += 12 + length
    return kinds


def jpeg_header_markers(data: bytes) -> list[int]:
    """The markers before the first scan, in order. Assumes a well-formed file."""
    markers, position = [], 2
    while data[position + 1] != 0xDA:
        markers.append(data[position + 1])
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        position += 2 + length
    return markers


def assert_only_orientation(data: bytes, orientation: int) -> None:
    with opened(data) as image:
        exif = image.getexif()
        assert dict(exif) == {ORIENTATION: orientation}
        assert exif.get_ifd(ExifTags.IFD.GPSInfo) == {}
        assert exif.get_ifd(ExifTags.IFD.Exif) == {}
        assert exif.get_ifd(ExifTags.IFD.IFD1) == {}


# --- the fixtures are what they claim -----------------------------------------------------------


def test_the_phone_jpeg_carries_everything_a_phone_writes() -> None:
    data = phone_jpeg()

    with opened(data) as image:
        exif = image.getexif()
        assert exif[ORIENTATION] == 6
        assert exif.get_ifd(ExifTags.IFD.GPSInfo)[ExifTags.GPS.GPSLatitudeRef] == "N"
        assert exif.get_ifd(ExifTags.IFD.IFD1)[0x0202] > 0
        assert image.info["icc_profile"] == srgb_profile()
        assert {"xmp", "comment", "photoshop"} <= image.info.keys()
    assert 0xEC in jpeg_header_markers(data)


def test_the_mpo_has_a_second_frame_with_its_own_gps() -> None:
    data = phone_mpo()

    with opened(data) as image:
        assert (image.format, image.n_frames) == ("MPO", 2)
        primary_size = image.mpinfo[0xB002][0]["Size"]
    trailing = data[primary_size:]
    with opened(trailing) as second:
        assert second.getexif().get_ifd(ExifTags.IFD.GPSInfo)


def test_the_png_carries_exif_text_and_a_timestamp() -> None:
    data = tagged_png()

    assert {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME", b"iCCP"} <= set(png_chunks(data))
    with opened(data) as image:
        assert image.getexif().get_ifd(ExifTags.IFD.GPSInfo)


# --- JPEG ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [{}, {"progressive": True}, {"restart_marker_blocks": 1}, {"subsampling": 0}],
    ids=["baseline", "progressive", "restart-markers", "4:4:4"],
)
def test_a_phone_jpeg_keeps_only_its_orientation_and_profile(options: dict[str, Any]) -> None:
    data = phone_jpeg(**options)

    stripped = strip_metadata(data, JPEG)

    assert_only_orientation(stripped, 6)
    with opened(stripped) as image:
        assert image.info["icc_profile"] == srgb_profile()
        assert not {"xmp", "comment", "photoshop"} & image.info.keys()
    assert pixels(stripped) == pixels(data)
    # JFIF, the new EXIF block where the old one was, and the ICC profile. No XMP, IPTC, Ducky
    # or comment segment is left.
    assert [m for m in jpeg_header_markers(stripped) if 0xE0 <= m <= 0xEF or m == 0xFE] == [
        0xE0,
        0xE1,
        0xE2,
    ]
    assert stripped.endswith(b"\xff\xd9")
    assert b"GPS" not in stripped
    assert b"TestCam" not in stripped


def test_the_scan_data_is_copied_unchanged() -> None:
    data = phone_jpeg(progressive=True)
    plain = encode(gradient(), "JPEG", quality=90, progressive=True)

    stripped = strip_metadata(data, JPEG)

    # Everything from the first DQT on, every scan included, is byte for byte the original's.
    tables_at = plain.index(b"\xff\xdb")
    assert stripped.endswith(plain[tables_at:])
    assert data.endswith(plain[tables_at:])


def test_everything_after_the_primary_image_is_dropped() -> None:
    data = phone_mpo()
    with opened(data) as image:
        primary = data[: image.mpinfo[0xB002][0]["Size"]]

    stripped = strip_metadata(data, JPEG)

    # The same as stripping the primary image alone: the second frame and its EXIF are gone.
    assert stripped == strip_metadata(primary, JPEG)
    assert b"MPF\x00" not in stripped
    with opened(stripped) as image:
        assert (image.format, getattr(image, "n_frames", 1)) == ("JPEG", 1)
        assert image.info["icc_profile"] == srgb_profile()
    assert_only_orientation(stripped, 6)
    assert pixels(stripped) == pixels(primary)
    assert stripped.endswith(b"\xff\xd9")


@pytest.mark.parametrize("orientation", range(2, 9))
def test_every_rotation_and_mirroring_is_kept(orientation: int) -> None:
    stripped = strip_metadata(phone_jpeg(orientation), JPEG)

    assert_only_orientation(stripped, orientation)


def test_orientation_one_leaves_no_exif_at_all() -> None:
    stripped = strip_metadata(phone_jpeg(orientation=1), JPEG)

    assert b"Exif" not in stripped
    with opened(stripped) as image:
        assert dict(image.getexif()) == {}


def test_little_endian_exif_is_read_too() -> None:
    data = encode(gradient(), "JPEG", exif=camera_exif(8, endian="<"))
    assert data[data.index(b"Exif\x00\x00") + 6 :].startswith(b"II")

    assert_only_orientation(strip_metadata(data, JPEG), 8)


def test_an_icc_profile_split_over_several_segments_is_kept_whole() -> None:
    # Over 64 KB, so Pillow writes it as a numbered series of APP2 segments.
    profile = srgb_profile() + bytes(range(256)) * 400
    data = encode(gradient(), "JPEG", icc_profile=profile, exif=camera_exif())
    assert data.count(b"ICC_PROFILE\x00") > 1

    stripped = strip_metadata(data, JPEG)

    with opened(stripped) as image:
        assert image.info["icc_profile"] == profile
    assert pixels(stripped) == pixels(data)


def test_an_adobe_segment_is_kept_because_it_decides_the_colours() -> None:
    cmyk = encode(gradient().convert("CMYK"), "JPEG", exif=camera_exif())
    assert 0xEE in jpeg_header_markers(cmyk)

    stripped = strip_metadata(cmyk, JPEG)

    assert 0xEE in jpeg_header_markers(stripped)
    assert pixels(stripped) == pixels(cmyk)


def test_a_jpeg_cut_off_inside_its_scan_keeps_what_it_has() -> None:
    """There is nothing after the image to drop. Whether it decodes is the thumbnail's call."""
    data = phone_jpeg()

    assert strip_metadata(data[:-2], JPEG) == strip_metadata(data, JPEG)[:-2]


# --- PNG ----------------------------------------------------------------------------------------


def test_a_png_loses_exif_text_and_time_and_keeps_every_other_chunk_in_order() -> None:
    data = tagged_png()

    stripped = strip_metadata(data, PNG)

    dropped = {b"tEXt", b"zTXt", b"iTXt", b"tIME"}
    # The eXIf chunk is rewritten in place, with the orientation alone.
    assert png_chunks(stripped) == [kind for kind in png_chunks(data) if kind not in dropped]
    assert_only_orientation(stripped, 6)
    with opened(data) as image:
        dpi = image.info["dpi"]
    with opened(stripped) as image:
        assert image.text == {}
        assert image.info["icc_profile"] == srgb_profile()
        assert image.info["dpi"] == dpi
    assert pixels(stripped) == pixels(data)
    assert stripped.endswith(b"IEND\xaeB`\x82")


def test_a_png_with_orientation_one_has_no_exif_chunk_left() -> None:
    stripped = strip_metadata(tagged_png(orientation=1), PNG)

    assert b"eXIf" not in png_chunks(stripped)


def test_a_png_exif_chunk_with_the_jpeg_prefix_is_read_too() -> None:
    tiff = camera_exif(3).tobytes()  # starts with "Exif\0\0", as some writers leave it
    chunk = struct.pack(">I", len(tiff)) + b"eXIf" + tiff
    chunk += struct.pack(">I", zlib.crc32(b"eXIf" + tiff))
    plain = encode(gradient(), "PNG")
    data = plain[:33] + chunk + plain[33:]  # right after IHDR

    assert_only_orientation(strip_metadata(data, PNG), 3)


def test_bytes_after_iend_are_dropped() -> None:
    plain = encode(gradient(), "PNG")

    assert strip_metadata(plain + b"PK\x03\x04 a hidden archive", PNG) == plain


# --- what stays the same ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "mime_type"),
    [
        (encode(gradient(), "JPEG"), JPEG),
        (encode(gradient(), "JPEG", progressive=True), JPEG),
        (encode(gradient(), "JPEG", icc_profile=srgb_profile()), JPEG),
        (encode(gradient().convert("CMYK"), "JPEG"), JPEG),
        (encode(gradient(), "PNG"), PNG),
        (encode(gradient().convert("RGBA"), "PNG", icc_profile=srgb_profile()), PNG),
    ],
    ids=["jpeg", "progressive", "jpeg-icc", "cmyk", "png", "png-icc"],
)
def test_a_file_without_metadata_comes_back_byte_for_byte(data: bytes, mime_type: str) -> None:
    assert strip_metadata(data, mime_type) == data


@pytest.mark.parametrize(
    ("data", "mime_type"), [(phone_jpeg(), JPEG), (phone_mpo(), JPEG), (tagged_png(), PNG)]
)
def test_stripping_twice_changes_nothing_more(data: bytes, mime_type: str) -> None:
    once = strip_metadata(data, mime_type)

    assert strip_metadata(once, mime_type) == once


# --- what it refuses ----------------------------------------------------------------------------


def no_scan() -> bytes:
    data = encode(gradient(), "JPEG")
    return data[: data.index(b"\xff\xdb")] + b"\xff\xd9"


@pytest.mark.parametrize(
    ("data", "mime_type"),
    [
        (b"not an image at all", JPEG),
        (b"not an image at all", PNG),
        (phone_jpeg()[:40], JPEG),  # cut inside the EXIF segment
        (no_scan(), JPEG),
        (b"\xff\xd8\xff\xe1\x00\x01", JPEG),  # a length too short to hold itself
        (tagged_png()[:-12], PNG),  # no IEND
        (tagged_png()[:50], PNG),  # cut inside a chunk
        (encode(gradient(), "PNG")[:12] + b"\x00\x00\x00\x0dI#DR", PNG),  # a bad chunk type
    ],
    ids=[
        "jpeg-garbage",
        "png-garbage",
        "jpeg-cut-in-header",
        "jpeg-no-scan",
        "jpeg-bad-length",
        "png-no-iend",
        "png-cut-in-chunk",
        "png-bad-chunk-type",
    ],
)
def test_a_file_it_cannot_parse_is_refused(data: bytes, mime_type: str) -> None:
    with pytest.raises(InvalidImageError) as refused:
        strip_metadata(data, mime_type)

    assert refused.value.code == "corrupt"


def test_a_type_other_than_jpeg_or_png_is_refused() -> None:
    with pytest.raises(InvalidImageError) as refused:
        strip_metadata(encode(gradient(), "GIF"), "image/gif")

    assert refused.value.code == "unsupported_type"


# --- summarize_exif -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "mime_type", "summary"),
    [
        (phone_jpeg(), JPEG, ExifSummary(gps=True, other_tags=True)),
        (tagged_png(), PNG, ExifSummary(gps=True, other_tags=True)),
        (
            encode(gradient(), "JPEG", exif=camera_exif(gps=False)),
            JPEG,
            ExifSummary(gps=False, other_tags=True),
        ),
        (strip_metadata(phone_jpeg(), JPEG), JPEG, ExifSummary(gps=False, other_tags=False)),
        (strip_metadata(tagged_png(), PNG), PNG, ExifSummary(gps=False, other_tags=False)),
        (encode(gradient(), "JPEG"), JPEG, ExifSummary(gps=False, other_tags=False)),
    ],
    ids=["phone-jpeg", "png", "camera-only", "stripped-jpeg", "stripped-png", "no-exif"],
)
def test_summarize_exif_tells_gps_from_other_tags(
    data: bytes, mime_type: str, summary: ExifSummary
) -> None:
    assert summarize_exif(data, mime_type) == summary


def test_an_exif_thumbnail_alone_counts_as_other_tags() -> None:
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    data = encode(gradient(), "JPEG", exif=with_exif_thumbnail(exif, encode(gradient(), "JPEG")))

    assert summarize_exif(data, JPEG) == ExifSummary(gps=False, other_tags=True)


@pytest.mark.parametrize(
    ("data", "mime_type"), [(phone_jpeg(), JPEG), (phone_mpo(), JPEG), (tagged_png(), PNG)]
)
def test_damaged_files_are_stripped_or_refused_and_nothing_else(
    data: bytes, mime_type: str
) -> None:
    """Uploads are untrusted: a cut or a flipped byte anywhere must not crash the parser."""
    rng = random.Random(84)
    for _ in range(400):
        damaged = bytearray(data[: rng.randrange(len(data) + 1)])
        for _ in range(rng.randrange(4)):
            if damaged:
                damaged[rng.randrange(len(damaged))] = rng.randrange(256)
        with contextlib.suppress(InvalidImageError):
            strip_metadata(bytes(damaged), mime_type)
