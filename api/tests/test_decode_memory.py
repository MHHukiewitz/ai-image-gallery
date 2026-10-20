"""Peak memory of the image paths on a phone-sized photo, measured in a child process.

The production incident was a burst of 12 MP uploads: every full-size decode and copy is 36 MB, and
the API container had 256 MB. A child process keeps the measurement free of what the test run has
already allocated. Its peak is read with `resource.getrusage` once after the imports and the file
read, and again after the work, so the difference is what the work cost.

Before the reduced-size decode this cost about 148 MB for the upload path, 144 MB for the colors
and 146 MB for the vision model's copy, on macOS. The limits sit well under those figures, and well
over what the code needs now, so only a return to full-size decoding trips them.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from PIL.ExifTags import Base

resource = pytest.importorskip("resource")

PHOTO_SIZE = (4032, 3024)
# The same work, as each caller does it. Run in a child process, so every name is imported there.
CHILD = """
import json
import platform
import resource
import sys
from pathlib import Path

from gallery.adapters.analyzers.images import normalize_image
from gallery.domain.colors import dominant_colors
from gallery.domain.thumbnails import make_thumbnail
from gallery.domain.validation import inspect_image

PATHS = {
    "upload": lambda data: (
        inspect_image(data, max_bytes=10 * 2**20, max_pixels=40_000_000),
        make_thumbnail(data),
    ),
    "colors": dominant_colors,
    "vision": normalize_image,
}


def peak_megabytes() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return peak / 2**20 if platform.system() == "Darwin" else peak / 1024


data = Path(sys.argv[1]).read_bytes()
before = peak_megabytes()
PATHS[sys.argv[2]](data)
print(json.dumps({"before": before, "after": peak_megabytes()}))
"""


@pytest.fixture(scope="module")
def phone_photo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 12 MP JPEG tagged as rotated, which is how a phone held upright records its photos."""
    exif = Image.Exif()
    exif[Base.Orientation] = 6
    path = tmp_path_factory.mktemp("photo") / "phone.jpg"
    Image.effect_noise(PHOTO_SIZE, 30).convert("RGB").save(path, "JPEG", quality=88, exif=exif)
    return path


def megabytes_used_by(which: str, photo: Path) -> float:
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(photo), which],
        capture_output=True,
        text=True,
        check=True,
    )
    peaks = json.loads(done.stdout)
    return peaks["after"] - peaks["before"]


@pytest.mark.parametrize(
    ("which", "limit_megabytes"),
    [
        # One full decode alone is 36 MB, so these limits rule out even a single full-size copy.
        ("upload", 36),
        ("colors", 36),
        # 1/2 scale is 9 MB a copy, and Pillow makes a few. The old full-size path took 146.
        ("vision", 80),
    ],
)
def test_a_12_megapixel_photo_is_processed_in_little_memory(
    phone_photo: Path, which: str, limit_megabytes: int
) -> None:
    assert megabytes_used_by(which, phone_photo) < limit_megabytes
