"""The Supabase adapters against the local stack, with two real users signed up through Auth.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration

The stack's own names (API_URL, PUBLISHABLE_KEY, SECRET_KEY) and the SUPABASE_* names that CI
exports are both understood. The secret key is optional: it only lets the tests delete their users.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from typing import Annotated, Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from PIL import Image
from storage3.exceptions import StorageApiError

from gallery.adapters.supabase_client import (
    AsyncClient,
    close_client,
    database_is_reachable,
    user_client,
)
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.api import deps
from gallery.config import Settings
from gallery.domain.models import (
    AllImages,
    AnalysisStatus,
    NewImage,
)
from gallery.ports import ImageRepo
from gallery.services.gallery import GalleryService

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)


def _stack_value(*names: str) -> str | None:
    return next((_ENVIRONMENT[name] for name in names if _ENVIRONMENT.get(name)), None)


@pytest.fixture
def stack_settings() -> Settings:
    url = _stack_value("SUPABASE_URL", "API_URL")
    publishable_key = _stack_value("SUPABASE_PUBLISHABLE_KEY", "PUBLISHABLE_KEY", "ANON_KEY")
    if url is None or publishable_key is None:
        pytest.fail(
            "The local Supabase stack's values are not in the environment. Run `make dev-db`, "
            'then `set -a; eval "$(supabase status -o env)"; set +a`.'
        )
    # The secret only matters if the stack signs user tokens with HS256. The deps use the JWKS
    # for EC and RSA tokens, which is what current stacks sign with.
    return Settings(
        SUPABASE_URL=url,
        SUPABASE_PUBLISHABLE_KEY=publishable_key,
        SUPABASE_JWT_SECRET=_stack_value("SUPABASE_JWT_SECRET", "JWT_SECRET"),
    )


@dataclass(frozen=True)
class Tenant:
    id: UUID
    access_token: str
    client: AsyncClient
    images: SupabaseImageRepo
    objects: SupabaseObjectStore


async def _sign_up(settings: Settings) -> tuple[UUID, str]:
    async with httpx.AsyncClient(base_url=settings.SUPABASE_URL) as http:
        response = await http.post(
            "/auth/v1/signup",
            headers={"apikey": settings.SUPABASE_PUBLISHABLE_KEY},
            json={"email": f"{uuid.uuid4().hex}@example.test", "password": "test-password-123"},
        )
    response.raise_for_status()
    body = response.json()
    return UUID(body["user"]["id"]), body["access_token"]


async def _delete_user(user_id: UUID) -> None:
    secret_key = _stack_value("SUPABASE_SECRET_KEY", "SECRET_KEY", "SERVICE_ROLE_KEY")
    url = _stack_value("SUPABASE_URL", "API_URL")
    if secret_key is None or url is None:
        return
    async with httpx.AsyncClient(base_url=url) as http:
        await http.delete(
            f"/auth/v1/admin/users/{user_id}",
            headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
        )


async def _tenant(settings: Settings) -> AsyncIterator[Tenant]:
    user_id, access_token = await _sign_up(settings)
    client = await user_client(settings, access_token)
    try:
        yield Tenant(
            id=user_id,
            access_token=access_token,
            client=client,
            images=SupabaseImageRepo(client),
            objects=SupabaseObjectStore(client, settings.STORAGE_BUCKET),
        )
    finally:
        await close_client(client)
        # Deleting the user cascades to their images and image_metadata rows.
        await _delete_user(user_id)


@pytest.fixture
async def alice(stack_settings: Settings) -> AsyncIterator[Tenant]:
    async for tenant in _tenant(stack_settings):
        yield tenant


@pytest.fixture
async def bob(stack_settings: Settings) -> AsyncIterator[Tenant]:
    async for tenant in _tenant(stack_settings):
        yield tenant


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (4, 4), "#336699").save(buffer, format="PNG")
    return buffer.getvalue()


def _new_image(owner: Tenant, **overrides: Any) -> NewImage:
    folder = f"{owner.id}/{uuid.uuid4()}"
    fields: dict[str, Any] = {
        "user_id": owner.id,
        "filename": "beach.png",
        "original_path": f"{folder}/original.png",
        "thumbnail_path": f"{folder}/thumb.webp",
        "mime_type": "image/png",
        "size_bytes": 1234,
        "width": 640,
        "height": 480,
        "content_sha256": "ab" * 32,
    }
    return NewImage(**(fields | overrides))


async def test_the_owner_inserts_gets_and_lists_an_image(alice: Tenant) -> None:
    new_image = _new_image(alice)

    inserted = await alice.images.insert(new_image)

    assert inserted.user_id == alice.id
    assert inserted.filename == "beach.png"
    assert inserted.original_path == new_image.original_path
    assert inserted.thumbnail_path == new_image.thumbnail_path
    assert (inserted.width, inserted.height, inserted.size_bytes) == (640, 480, 1234)
    assert inserted.content_sha256 == "ab" * 32
    assert inserted.status is AnalysisStatus.PENDING
    assert inserted.tags == ()
    assert inserted.colors == ()
    assert inserted.description is None
    assert inserted.error is None
    assert inserted.score is None
    assert inserted.uploaded_at.tzinfo is not None
    assert await alice.images.get(inserted.id) == inserted
    page = await alice.images.list(AllImages(), limit=20, offset=0)
    assert page.items == [inserted]
    assert page.next_offset is None


async def test_another_user_sees_nothing_of_the_image(alice: Tenant, bob: Tenant) -> None:
    inserted = await alice.images.insert(_new_image(alice))

    assert await bob.images.get(inserted.id) is None
    page = await bob.images.list(AllImages(), limit=20, offset=0)
    assert page.items == []
    assert page.next_offset is None
    assert await bob.images.delete(inserted.id) is None
    assert await alice.images.get(inserted.id) == inserted


async def test_the_owner_deletes_the_image(alice: Tenant) -> None:
    inserted = await alice.images.insert(_new_image(alice))

    deleted = await alice.images.delete(inserted.id)

    assert deleted == inserted
    assert await alice.images.get(inserted.id) is None
    assert (await alice.images.list(AllImages(), limit=20, offset=0)).items == []
    assert await alice.images.delete(inserted.id) is None


async def test_list_pages_newest_first_with_a_next_offset(alice: Tenant) -> None:
    first = await alice.images.insert(_new_image(alice, filename="first.png"))
    second = await alice.images.insert(_new_image(alice, filename="second.png"))
    third = await alice.images.insert(_new_image(alice, filename="third.png"))

    page_one = await alice.images.list(AllImages(), limit=2, offset=0)
    page_two = await alice.images.list(AllImages(), limit=2, offset=2)

    assert [image.id for image in page_one.items] == [third.id, second.id]
    assert page_one.next_offset == 2
    assert [image.id for image in page_two.items] == [first.id]
    assert page_two.next_offset is None


async def test_list_has_no_next_offset_when_the_last_page_is_full(alice: Tenant) -> None:
    await alice.images.insert(_new_image(alice))
    await alice.images.insert(_new_image(alice))

    page = await alice.images.list(AllImages(), limit=2, offset=0)

    assert len(page.items) == 2
    assert page.next_offset is None


async def test_an_insert_that_loses_its_metadata_row_leaves_no_image(alice: Tenant) -> None:
    class MetadataFails:
        """A client whose image_metadata inserts fail, while every other table works."""

        def table(self, name: str) -> Any:
            if name == "image_metadata":
                raise ConnectionError("the metadata insert failed")
            return alice.client.table(name)

    broken = SupabaseImageRepo(cast(AsyncClient, MetadataFails()))

    with pytest.raises(ConnectionError):
        await broken.insert(_new_image(alice))

    # image_details hides an image without metadata, so ask the table itself.
    rows = await alice.client.table("images").select("id").execute()
    assert rows.data == []


async def test_only_the_owner_can_requeue_an_image(alice: Tenant, bob: Tenant) -> None:
    inserted = await alice.images.insert(_new_image(alice))

    assert await bob.images.request_reanalysis(inserted.id) is False
    assert await alice.images.request_reanalysis(inserted.id) is True


async def test_reanalysis_of_a_deleted_image_is_refused(alice: Tenant) -> None:
    inserted = await alice.images.insert(_new_image(alice))
    await alice.images.delete(inserted.id)

    assert await alice.images.request_reanalysis(inserted.id) is False


async def test_deleting_an_image_removes_both_objects_from_storage(alice: Tenant) -> None:
    new_image = _new_image(alice)
    inserted = await alice.images.insert(new_image)
    paths = [new_image.original_path, new_image.thumbnail_path]
    for path in paths:
        await alice.objects.put(path, _png(), "image/png")
    service = GalleryService(alice.images, alice.objects, signed_url_ttl=60)

    await service.delete(inserted.id)

    assert await alice.images.get(inserted.id) is None
    assert await alice.objects.signed_urls(paths, ttl_seconds=60) == {}
    for path in paths:
        with pytest.raises(StorageApiError):
            await alice.objects.get(path)


async def test_objects_round_trip_under_the_owners_folder(alice: Tenant) -> None:
    path = f"{alice.id}/{uuid.uuid4()}/original.png"
    data = _png()

    await alice.objects.put(path, data, "image/png")
    downloaded = await alice.objects.get(path)
    urls = await alice.objects.signed_urls([path], ttl_seconds=60)
    async with httpx.AsyncClient() as http:
        fetched = await http.get(urls[path])
    await alice.objects.delete([path])

    assert downloaded == data
    assert fetched.status_code == 200
    assert fetched.content == data
    with pytest.raises(StorageApiError):
        await alice.objects.get(path)
    assert await alice.objects.signed_urls([path], ttl_seconds=60) == {}


async def test_signed_urls_sign_every_path_in_one_call(alice: Tenant) -> None:
    folder = f"{alice.id}/{uuid.uuid4()}"
    paths = [f"{folder}/original.png", f"{folder}/thumb.png"]
    for path in paths:
        await alice.objects.put(path, _png(), "image/png")

    try:
        urls = await alice.objects.signed_urls(paths, ttl_seconds=60)
    finally:
        await alice.objects.delete(paths)

    assert set(urls) == set(paths)
    assert len(set(urls.values())) == 2


async def test_signed_urls_leave_out_a_missing_object(alice: Tenant) -> None:
    missing = f"{alice.id}/{uuid.uuid4()}/original.png"

    assert await alice.objects.signed_urls([missing], ttl_seconds=60) == {}
    assert await alice.objects.signed_urls([], ttl_seconds=60) == {}


async def test_a_user_cannot_write_into_another_users_folder(alice: Tenant, bob: Tenant) -> None:
    path = f"{alice.id}/{uuid.uuid4()}/original.png"

    with pytest.raises(StorageApiError):
        await bob.objects.put(path, _png(), "image/png")


async def test_an_existing_object_is_not_overwritten(alice: Tenant) -> None:
    path = f"{alice.id}/{uuid.uuid4()}/original.png"
    await alice.objects.put(path, _png(), "image/png")

    try:
        with pytest.raises(StorageApiError):
            await alice.objects.put(path, _png(), "image/png")
    finally:
        await alice.objects.delete([path])


# --- authentication against the stack's real tokens ----------------------------------------


async def test_a_real_access_token_is_accepted(alice: Tenant, stack_settings: Settings) -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=alice.access_token)

    user = await deps.get_current_user(credentials, stack_settings)

    assert user.id == alice.id
    assert user.access_token == alice.access_token
    assert user.email is not None


async def test_the_api_serves_each_user_only_their_own_images(
    alice: Tenant, bob: Tenant, stack_settings: Settings
) -> None:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: stack_settings

    @app.get("/test/images")
    async def own_image_ids(
        images: Annotated[ImageRepo, Depends(deps.get_image_repo)],
    ) -> list[int]:
        page = await images.list(AllImages(), limit=50, offset=0)
        return [image.id for image in page.items]

    inserted = await alice.images.insert(_new_image(alice))

    def as_user(tenant: Tenant) -> dict[str, str]:
        return {"Authorization": f"Bearer {tenant.access_token}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        owners = await http.get("/test/images", headers=as_user(alice))
        strangers = await http.get("/test/images", headers=as_user(bob))
        anonymous = await http.get("/test/images")
        # Corrupting the last characters breaks the signature.
        forged = await http.get(
            "/test/images",
            headers={"Authorization": f"Bearer {alice.access_token[:-4]}AAAA"},
        )

    assert owners.json() == [inserted.id]
    assert strangers.json() == []
    assert anonymous.status_code == 401
    assert forged.status_code == 401
    assert forged.json()["error"]["code"] == "unauthorized"


# --- the deep health check -----------------------------------------------------------------


async def test_the_database_check_reaches_the_local_stack(stack_settings: Settings) -> None:
    assert await database_is_reachable(stack_settings) is True


async def test_the_database_check_fails_for_a_wrong_key(stack_settings: Settings) -> None:
    wrong_key = stack_settings.model_copy(update={"SUPABASE_PUBLISHABLE_KEY": "not-a-real-key"})

    assert await database_is_reachable(wrong_key) is False


async def test_the_database_check_fails_when_nothing_listens(stack_settings: Settings) -> None:
    nobody_home = stack_settings.model_copy(update={"SUPABASE_URL": "http://127.0.0.1:9"})

    assert await database_is_reachable(nobody_home) is False


async def test_the_deep_health_route_is_ok_against_the_local_stack(
    stack_settings: Settings,
) -> None:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: stack_settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/api/health", params={"deep": 1})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
