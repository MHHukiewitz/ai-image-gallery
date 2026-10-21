"""scripts/strip_original_metadata.py against the local stack, limited to one test user's images.

The user stores a phone photo as uploads were stored before #85, byte for byte, through RLS. The
script, with the secret key, strips it in place: the user can still read and delete the object, the
row's size follows, and a deleted object is never brought back.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_strip_metadata_backfill.py
"""

import hashlib
import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import ModuleType
from uuid import UUID

import httpx
import pytest
from PIL import ExifTags, Image

from gallery.adapters.supabase_client import AsyncClient, close_client, service_client, user_client
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.config import Settings
from gallery.domain.metadata import strip_metadata
from gallery.domain.models import NewImage

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)


def _stack_value(*names: str) -> str | None:
    return next((_ENVIRONMENT[name] for name in names if _ENVIRONMENT.get(name)), None)


def _load_script() -> ModuleType:
    path = Path(__file__).parents[3] / "scripts" / "strip_original_metadata.py"
    spec = importlib.util.spec_from_file_location("strip_original_metadata", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


@pytest.fixture
def stack_settings() -> Settings:
    url = _stack_value("SUPABASE_URL", "API_URL")
    publishable_key = _stack_value("SUPABASE_PUBLISHABLE_KEY", "PUBLISHABLE_KEY", "ANON_KEY")
    secret_key = _stack_value("SUPABASE_SECRET_KEY", "SECRET_KEY", "SERVICE_ROLE_KEY")
    if url is None or publishable_key is None or secret_key is None:
        pytest.fail(
            "The local Supabase stack's values are not in the environment. Run `make dev-db`, "
            'then `set -a; eval "$(supabase status -o env)"; set +a`.'
        )
    return Settings(
        SUPABASE_URL=url,
        SUPABASE_PUBLISHABLE_KEY=publishable_key,
        SUPABASE_SECRET_KEY=secret_key,
        SUPABASE_JWT_SECRET=_stack_value("SUPABASE_JWT_SECRET", "JWT_SECRET"),
    )


@dataclass(frozen=True)
class Tenant:
    id: UUID
    images: SupabaseImageRepo
    objects: SupabaseObjectStore


@pytest.fixture
async def tenant(stack_settings: Settings) -> AsyncIterator[Tenant]:
    secret_key = stack_settings.SUPABASE_SECRET_KEY
    assert secret_key is not None
    async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
        response = await http.post(
            "/auth/v1/signup",
            headers={"apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
        response.raise_for_status()
        body = response.json()
        user_id = UUID(body["user"]["id"])
        client = await user_client(stack_settings, body["access_token"])
        try:
            yield Tenant(
                id=user_id,
                images=SupabaseImageRepo(client),
                objects=SupabaseObjectStore(client, stack_settings.STORAGE_BUCKET),
            )
        finally:
            await close_client(client)
            deleted = await http.delete(
                f"/auth/v1/admin/users/{user_id}",
                headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            )
            # A user left behind would keep their rows in the shared database.
            deleted.raise_for_status()


@pytest.fixture
async def admin(stack_settings: Settings) -> AsyncIterator[AsyncClient]:
    client = await service_client(stack_settings)
    yield client
    await close_client(client)


def phone_photo() -> bytes:
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.Base.Make] = "TestCam"
    exif[ExifTags.IFD.GPSInfo] = {
        ExifTags.GPS.GPSLatitudeRef: "N",
        ExifTags.GPS.GPSLatitude: (52.0, 31.0, 12.0),
    }
    buffer = BytesIO()
    Image.new("RGB", (64, 48), "teal").save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


async def store_as_before(tenant: Tenant, data: bytes) -> int:
    """Store the file byte for byte, with its row, the way uploads were stored before #85."""
    folder = f"{tenant.id}/{uuid.uuid4()}"
    original_path = f"{folder}/original.jpg"
    await tenant.objects.put(original_path, data, "image/jpeg")
    record = await tenant.images.insert(
        NewImage(
            user_id=tenant.id,
            filename="beach.jpg",
            original_path=original_path,
            thumbnail_path=f"{folder}/thumb.webp",
            mime_type="image/jpeg",
            size_bytes=len(data),
            width=48,
            height=64,
            content_sha256=hashlib.sha256(data).hexdigest(),
            stored_sha256=hashlib.sha256(data).hexdigest(),
        )
    )
    return record.id


async def test_the_script_strips_a_stored_original_in_place(
    tenant: Tenant, admin: AsyncClient, stack_settings: Settings
) -> None:
    data = phone_photo()
    image_id = await store_as_before(tenant, data)
    record = await tenant.images.get(image_id)
    assert record is not None
    originals = script.SupabaseOriginals(admin, stack_settings.STORAGE_BUCKET, user_id=tenant.id)
    try:
        dry_run = await script.strip_originals(originals, apply=False)
        assert dry_run == script.StripCounts(scanned=1, gps=1)
        assert await tenant.objects.get(record.original_path) == data

        applied = await script.strip_originals(originals, apply=True)

        assert (applied.rewritten, applied.resized, applied.failed) == (1, 1, 0)
        # The owner still reads the object through RLS, and it is the stripped file.
        stored = await tenant.objects.get(record.original_path)
        assert stored == strip_metadata(data, "image/jpeg")
        with Image.open(BytesIO(stored)) as image:
            assert image.getexif().get_ifd(ExifTags.IFD.GPSInfo) == {}
            assert dict(image.getexif()) == {ExifTags.Base.Orientation: 6}
        after = await tenant.images.get(image_id)
        assert after is not None
        assert after.size_bytes == len(stored)
        assert after.content_sha256 == hashlib.sha256(data).hexdigest()

        again = await script.strip_originals(originals, apply=True)
        assert again == script.StripCounts(scanned=1, clean=1)
    finally:
        # The owner can still delete the object the secret key rewrote.
        await tenant.objects.delete([record.original_path])
        await tenant.images.delete(image_id)

    # A replace that lands after the owner deleted the image doesn't leave the file behind.
    original = script.StoredOriginal(image_id, record.original_path, "image/jpeg", len(data))
    assert await originals.replace(original, data) is False
    leftovers = await admin.storage.from_(stack_settings.STORAGE_BUCKET).list(str(tenant.id))
    assert leftovers == []
