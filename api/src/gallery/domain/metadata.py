"""Strip location and camera metadata from a stored original, without re-encoding its pixels.

Phone photos carry EXIF (GPS position, camera, capture time, a thumbnail), XMP and IPTC. Only the
container is rewritten here: the compressed image data is copied byte for byte, so the picture
decodes to exactly the same pixels. The one tag kept is the EXIF orientation, in a minimal EXIF
block of its own, because browsers rotate an original by it. A file that holds no metadata comes
back unchanged.
"""

import struct
import zlib
from dataclasses import dataclass

from gallery.domain.errors import InvalidImageError

# --- JPEG ---------------------------------------------------------------------------------------

_JPEG_SOI = b"\xff\xd8"
_SOS = 0xDA
_EOI = 0xD9
_SOI = 0xD8
_APP0 = 0xE0
_APP1 = 0xE1
_APP2 = 0xE2
_APP14 = 0xEE
_APP15 = 0xEF
_COM = 0xFE
# TEM and the restart markers stand alone: no length follows them.
_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_RESTART_MARKERS = range(0xD0, 0xD8)
_EXIF_PREFIX = b"Exif\x00\x00"
_ICC_PREFIX = b"ICC_PROFILE\x00"

# --- PNG ----------------------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_EXIF = b"eXIf"
_PNG_END = b"IEND"
# EXIF, the three kinds of text chunk (which also carry XMP and IPTC) and the modification time.
_PNG_DROPPED = frozenset({_PNG_EXIF, b"tEXt", b"zTXt", b"iTXt", b"tIME"})
# A PNG chunk length is at most 2^31 - 1.
_PNG_MAX_CHUNK = 2**31 - 1

# --- EXIF (TIFF) --------------------------------------------------------------------------------

_ORIENTATION = 0x0112
_GPS_IFD = 0x8825
_TIFF_SHORT = 3
_TIFF_ENTRY_SIZE = 12


@dataclass(frozen=True, slots=True)
class ExifSummary:
    """What the EXIF block a browser reads holds, without any of its values.

    `gps` is true when the block points to a GPS IFD. `other_tags` is true when it holds any tag
    besides the orientation, or a second IFD (where the EXIF thumbnail lives).
    """

    gps: bool
    other_tags: bool


@dataclass(frozen=True, slots=True)
class _Parsed:
    """A file split into the parts to keep, and the EXIF block that was dropped from it.

    `exif_slot` is where in `kept` that block was, so its replacement goes in the same place.
    """

    kept: list[bytes]
    exif: bytes | None
    exif_slot: int


def strip_metadata(data: bytes, mime_type: str) -> bytes:
    """Return `data` without its EXIF, XMP, IPTC, comments or trailing data, pixels untouched.

    A JPEG keeps the segments a decoder reads (APP0 JFIF, the APP2 ICC profile, APP14 Adobe, and
    every table, frame and scan segment) and loses every other APPn segment, the comments, and
    everything after the primary image's EOI, where phones append MPO and depth images that carry
    their own EXIF. A PNG loses its eXIf, tEXt, zTXt, iTXt and tIME chunks, and anything after
    IEND, and keeps every other chunk in order.

    An orientation other than 1 is written back, alone, in a minimal EXIF block where the old one
    was, so the image still displays upright. Raises InvalidImageError when the file can't be
    parsed: it is never stored as it came.
    """
    parsed = _parse(data, mime_type)
    orientation = _orientation(parsed.exif) if parsed.exif is not None else None
    parts = parsed.kept
    if orientation is not None and orientation != 1:
        tiff = _minimal_exif(orientation)
        if mime_type == "image/jpeg":
            block = _jpeg_exif_segment(tiff)
        else:
            block = _png_chunk(_PNG_EXIF, tiff)
        parts = [*parts[: parsed.exif_slot], block, *parts[parsed.exif_slot :]]
    return b"".join(parts)


def summarize_exif(data: bytes, mime_type: str) -> ExifSummary:
    """Say whether the file's EXIF holds a GPS position or other tags. Never returns their values.

    It looks at the block a browser reads: the first EXIF APP1 segment of a JPEG, the eXIf chunk
    of a PNG. Raises InvalidImageError when the file can't be parsed.
    """
    exif = _parse(data, mime_type).exif
    ifd = _first_ifd(exif) if exif is not None else None
    if ifd is None:
        return ExifSummary(gps=False, other_tags=False)
    tags, next_ifd = ifd
    return ExifSummary(
        gps=_GPS_IFD in tags,
        other_tags=any(tag != _ORIENTATION for tag in tags) or next_ifd != 0,
    )


def _parse(data: bytes, mime_type: str) -> _Parsed:
    if mime_type == "image/jpeg":
        return _parse_jpeg(data)
    if mime_type == "image/png":
        return _parse_png(data)
    raise InvalidImageError("unsupported_type", "Only JPEG and PNG images are accepted.")


def _corrupt() -> InvalidImageError:
    return InvalidImageError("corrupt", "The image can't be decoded.")


# --- JPEG ---------------------------------------------------------------------------------------


def _parse_jpeg(data: bytes) -> _Parsed:
    """Walk the segments up to the first scan, then the scans up to the primary image's EOI.

    Bytes that aren't part of a marker segment, such as junk before a marker (which decoders skip)
    or the entropy-coded scan data, are kept as they are.
    """
    if not data.startswith(_JPEG_SOI):
        raise _corrupt()
    kept = [_JPEG_SOI]
    exif: bytes | None = None
    exif_slot = 0
    position = len(_JPEG_SOI)
    in_scan = False
    while True:
        if in_scan:
            marker_at = _next_scan_marker(data, position)
        else:
            marker_at = _next_header_marker(data, position)
        if marker_at < 0:
            if in_scan:
                # Cut off before its EOI. Nothing follows the image, so there's nothing to drop,
                # and whether what's there decodes is the thumbnail step's call.
                kept.append(data[position:])
                break
            raise _corrupt()
        marker, segment_start = _marker(data, marker_at)
        # Junk before a header marker, or the scan data before a marker inside the scans.
        kept.append(data[position:marker_at])
        if marker == _EOI:
            if not in_scan:
                raise _corrupt()
            kept.append(data[marker_at:segment_start])
            # Everything after the primary image goes: MPO frames, depth maps, vendor trailers.
            break
        if marker == _SOI:
            raise _corrupt()
        if marker in _STANDALONE_MARKERS:
            kept.append(data[marker_at:segment_start])
            position = segment_start
            continue
        if segment_start + 2 > len(data):
            raise _corrupt()
        (length,) = struct.unpack(">H", data[segment_start : segment_start + 2])
        segment_end = segment_start + length
        if length < 2 or segment_end > len(data):
            raise _corrupt()
        body = data[segment_start + 2 : segment_end]
        if _keeps_jpeg_segment(marker, body):
            kept.append(data[marker_at:segment_end])
        elif marker == _APP1 and not in_scan and exif is None and body.startswith(_EXIF_PREFIX):
            # Browsers read the orientation from the first EXIF segment before the scans.
            exif = body[len(_EXIF_PREFIX) :]
            exif_slot = len(kept)
        position = segment_end
        in_scan = in_scan or marker == _SOS
    return _Parsed(kept=kept, exif=exif, exif_slot=exif_slot)


def _marker(data: bytes, at: int) -> tuple[int, int]:
    """Read the marker whose 0xFF is at `at`, past any fill bytes. Return it and where it ends."""
    position = at + 1
    while position < len(data) and data[position] == 0xFF:
        position += 1
    if position >= len(data) or data[position] == 0x00:
        raise _corrupt()
    return data[position], position + 1


def _next_header_marker(data: bytes, position: int) -> int:
    """Return where the next marker before the scans starts, or -1 if the data ends first.

    Decoders skip junk between segments, including a stray 0xFF 0x00, so this does too.
    """
    while True:
        at = data.find(b"\xff", position)
        if at < 0 or at + 1 >= len(data):
            return -1
        if data[at + 1] != 0x00:
            return at
        position = at + 2


def _next_scan_marker(data: bytes, position: int) -> int:
    """Return where the next marker after entropy-coded data starts, or -1 if the data ends first.

    Inside a scan, 0xFF is followed by 0x00 (a stuffed data byte) or a restart marker. Neither
    ends the scan, and neither is a segment.
    """
    while True:
        at = data.find(b"\xff", position)
        if at < 0 or at + 1 >= len(data):
            return -1
        following = data[at + 1]
        if following == 0x00 or following in _RESTART_MARKERS:
            position = at + 2
        elif following == 0xFF:
            # A fill byte: the marker starts at the last 0xFF of the run.
            position = at + 1
        else:
            return at


def _keeps_jpeg_segment(marker: int, body: bytes) -> bool:
    """Keep what a decoder reads. Every other application segment and comment is metadata."""
    if marker == _APP2:
        # APP2 is also MPF, the index of the images that follow EOI, and FlashPix data.
        return body.startswith(_ICC_PREFIX)
    if marker in (_APP0, _APP14):
        # JFIF, and Adobe's colour transform flag, which decides how the pixels decode.
        return True
    return not (_APP0 <= marker <= _APP15 or marker == _COM)


def _jpeg_exif_segment(tiff: bytes) -> bytes:
    body = _EXIF_PREFIX + tiff
    return bytes([0xFF, _APP1]) + struct.pack(">H", len(body) + 2) + body


# --- PNG ----------------------------------------------------------------------------------------


def _parse_png(data: bytes) -> _Parsed:
    """Walk the chunks up to IEND. Each kept chunk is copied whole, with its own CRC."""
    if not data.startswith(_PNG_SIGNATURE):
        raise _corrupt()
    kept = [_PNG_SIGNATURE]
    exif: bytes | None = None
    exif_slot = 0
    position = len(_PNG_SIGNATURE)
    while True:
        if position + 12 > len(data):
            # No IEND: the file is cut off.
            raise _corrupt()
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        chunk_end = position + 12 + length
        if length > _PNG_MAX_CHUNK or chunk_end > len(data) or not _is_chunk_type(kind):
            raise _corrupt()
        if kind not in _PNG_DROPPED:
            kept.append(data[position:chunk_end])
        elif kind == _PNG_EXIF and exif is None:
            body = data[position + 8 : position + 8 + length]
            # Some writers keep JPEG's "Exif\0\0" prefix in the chunk. Readers accept both.
            exif = body.removeprefix(_EXIF_PREFIX)
            exif_slot = len(kept)
        position = chunk_end
        if kind == _PNG_END:
            # Anything after IEND isn't part of the image.
            break
    return _Parsed(kept=kept, exif=exif, exif_slot=exif_slot)


def _is_chunk_type(kind: bytes) -> bool:
    return all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind)


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body)
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


# --- EXIF (TIFF) --------------------------------------------------------------------------------


def _first_ifd(tiff: bytes) -> tuple[dict[int, tuple[int, int, bytes]], int] | None:
    """Read IFD0: each tag's type, count and 4-byte value field, and the next IFD's offset.

    Returns None when the block isn't a readable TIFF, which browsers treat as no EXIF at all.
    An IFD that runs past the block ends where the block does.
    """
    if tiff.startswith(b"II*\x00"):
        order = "<"
    elif tiff.startswith(b"MM\x00*"):
        order = ">"
    else:
        return None
    if len(tiff) < 8:
        return None
    (offset,) = struct.unpack(order + "I", tiff[4:8])
    if offset + 2 > len(tiff):
        return None
    (count,) = struct.unpack(order + "H", tiff[offset : offset + 2])
    entries: dict[int, tuple[int, int, bytes]] = {}
    position = offset + 2
    for _ in range(count):
        if position + _TIFF_ENTRY_SIZE > len(tiff):
            return entries, 0
        tag, kind, values = struct.unpack(order + "HHI", tiff[position : position + 8])
        entries.setdefault(tag, (kind, values, tiff[position + 8 : position + 12]))
        position += _TIFF_ENTRY_SIZE
    next_ifd = 0
    if position + 4 <= len(tiff):
        (next_ifd,) = struct.unpack(order + "I", tiff[position : position + 4])
    return entries, next_ifd


def _orientation(tiff: bytes) -> int | None:
    """Return the EXIF orientation, 1 to 8, the way browsers read it: one SHORT in IFD0."""
    ifd = _first_ifd(tiff)
    if ifd is None:
        return None
    entry = ifd[0].get(_ORIENTATION)
    if entry is None:
        return None
    kind, count, field = entry
    if kind != _TIFF_SHORT or count != 1:
        return None
    order = "<" if tiff.startswith(b"II") else ">"
    (value,) = struct.unpack(order + "H", field[:2])
    return value if 1 <= value <= 8 else None


def _minimal_exif(orientation: int) -> bytes:
    """A big-endian TIFF block with one IFD that holds only the orientation."""
    header = b"MM\x00*" + struct.pack(">I", 8)
    entry = struct.pack(">HHIHH", _ORIENTATION, _TIFF_SHORT, 1, orientation, 0)
    return header + struct.pack(">H", 1) + entry + struct.pack(">I", 0)
