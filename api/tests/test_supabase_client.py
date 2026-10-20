"""The HTTP timeout of the Supabase clients: PostgREST gets SUPABASE_HTTP_TIMEOUT_SECONDS.

Storage shares the client and keeps its longer limit, because uploads and downloads move files.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx

from gallery.adapters.supabase_client import close_client, service_client, user_client
from gallery.config import Settings

BASE = "http://localhost:54321"


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def timeouts_seen_by(route: respx.Route) -> dict[str, float | None]:
    [call] = route.calls
    return call.request.extensions["timeout"]


async def test_a_database_request_gets_the_configured_timeout(http: respx.MockRouter) -> None:
    route = http.get(f"{BASE}/rest/v1/images").mock(return_value=httpx.Response(200, json=[]))
    client = await user_client(Settings(SUPABASE_HTTP_TIMEOUT_SECONDS=3.0), "user-jwt")
    try:
        await client.table("images").select("id").execute()
    finally:
        await close_client(client)

    assert timeouts_seen_by(route) == {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}


async def test_the_connect_timeout_stays_short_when_the_setting_is_long(
    http: respx.MockRouter,
) -> None:
    route = http.post(f"{BASE}/rest/v1/rpc/bump_image").mock(
        return_value=httpx.Response(200, json=True)
    )
    client = await user_client(Settings(SUPABASE_HTTP_TIMEOUT_SECONDS=20.0), "user-jwt")
    try:
        await client.rpc("bump_image", {"p_image_id": 1}).execute()
    finally:
        await close_client(client)

    assert timeouts_seen_by(route)["connect"] == 5.0
    assert timeouts_seen_by(route)["read"] == 20.0


async def test_the_default_timeout_is_eight_seconds(http: respx.MockRouter) -> None:
    route = http.get(f"{BASE}/rest/v1/images").mock(return_value=httpx.Response(200, json=[]))
    client = await user_client(Settings(), "user-jwt")
    try:
        await client.table("images").select("id").execute()
    finally:
        await close_client(client)

    assert timeouts_seen_by(route)["read"] == 8.0


async def test_the_worker_client_times_out_database_requests_too(http: respx.MockRouter) -> None:
    route = http.get(f"{BASE}/rest/v1/images").mock(return_value=httpx.Response(200, json=[]))
    client = await service_client(Settings(SUPABASE_SECRET_KEY="test-secret"))
    try:
        await client.table("images").select("id").execute()
    finally:
        await close_client(client)

    assert timeouts_seen_by(route)["read"] == 8.0


async def test_storage_keeps_the_longer_timeout(http: respx.MockRouter) -> None:
    route = http.get(f"{BASE}/storage/v1/object/images/folder/original.jpg").mock(
        return_value=httpx.Response(200, content=b"bytes")
    )
    client = await user_client(Settings(SUPABASE_HTTP_TIMEOUT_SECONDS=3.0), "user-jwt")
    try:
        await client.storage.from_("images").download("folder/original.jpg")
    finally:
        await close_client(client)

    assert timeouts_seen_by(route)["read"] == 30.0


async def test_a_path_prefix_in_the_supabase_url_is_respected(http: respx.MockRouter) -> None:
    route = http.get("http://gateway.test/supabase/rest/v1/images").mock(
        return_value=httpx.Response(200, json=[])
    )
    settings = Settings(
        SUPABASE_URL="http://gateway.test/supabase/", SUPABASE_HTTP_TIMEOUT_SECONDS=3
    )
    client = await user_client(settings, "user-jwt")
    try:
        await client.table("images").select("id").execute()
    finally:
        await close_client(client)

    assert timeouts_seen_by(route)["read"] == 3.0


@pytest.fixture
async def silent_server() -> AsyncIterator[int]:
    """A local TCP server that reads a request and never answers it. Yields its port."""
    release = asyncio.Event()

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(65536)
            await release.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        release.set()
        server.close()
        await server.wait_closed()


async def test_a_database_read_with_no_answer_gives_up_at_the_timeout(silent_server: int) -> None:
    settings = Settings(
        SUPABASE_URL=f"http://127.0.0.1:{silent_server}", SUPABASE_HTTP_TIMEOUT_SECONDS=0.3
    )
    client = await user_client(settings, "user-jwt")
    started = time.monotonic()
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.table("images").select("id").retry(False).execute()
    finally:
        await close_client(client)

    # Far below the 30 s the client waited before the setting existed.
    assert time.monotonic() - started < 3.0
