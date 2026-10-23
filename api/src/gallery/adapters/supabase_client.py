"""Supabase client factories.

The API acts as the signed-in user (`user_client`), so Row Level Security and the storage folder
policies apply to every call. Only the worker uses the secret key (`service_client`).
"""

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from postgrest import APIError
from supabase import AsyncClient, AsyncClientOptions, acreate_client

from gallery.config import Settings

# AsyncClient is re-exported so api/deps.py can type the per-request client without importing
# the SDK itself.
__all__ = [
    "AsyncClient",
    "anon_client",
    "close_client",
    "database_is_reachable",
    "service_client",
    "user_client",
]

logger = logging.getLogger(__name__)

# Uploads and downloads move up to MAX_UPLOAD_MB, so the read timeout is generous. A dead host
# should still fail fast, hence the short connect timeout.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_CONNECT_TIMEOUT_SECONDS = 5.0
# A monitor would rather hear "down" quickly than wait on a hung database.
_HEALTH_TIMEOUT = httpx.Timeout(5.0)

# Postgres SQLSTATE for "insufficient privilege".
_INSUFFICIENT_PRIVILEGE = "42501"


def _database_timeout(settings: Settings) -> httpx.Timeout:
    """The limit for one PostgREST request, from SUPABASE_HTTP_TIMEOUT_SECONDS."""
    seconds = settings.SUPABASE_HTTP_TIMEOUT_SECONDS
    return httpx.Timeout(seconds, connect=min(seconds, _CONNECT_TIMEOUT_SECONDS))


def _postgrest_timeout_hook(
    settings: Settings, timeout: httpx.Timeout
) -> Callable[[httpx.Request], Awaitable[None]]:
    """Return an httpx request hook that gives PostgREST requests, and only them, `timeout`.

    The Supabase SDK shares one HTTP client between PostgREST and Storage, whose uploads and
    downloads need the longer default. httpx reads a request's own timeout from its extensions,
    so the hook sets it for the `/rest/v1/` path and leaves every other request alone.
    """
    rest_path = f"{urlsplit(settings.SUPABASE_URL).path.rstrip('/')}/rest/v1/"
    per_request = timeout.as_dict()

    async def apply(request: httpx.Request) -> None:
        if request.url.path.startswith(rest_path):
            request.extensions["timeout"] = per_request

    return apply


async def _create_client(
    settings: Settings,
    api_key: str,
    *,
    authorization: str | None = None,
    http_timeout: httpx.Timeout = _HTTP_TIMEOUT,
    database_timeout: httpx.Timeout | None = None,
) -> AsyncClient:
    # One HTTP client per Supabase client. The user's token lives only on this client, and
    # close_client() releases the connections once the request is done.
    http_client = httpx.AsyncClient(
        timeout=http_timeout,
        follow_redirects=True,
        event_hooks=(
            {"request": [_postgrest_timeout_hook(settings, database_timeout)]}
            if database_timeout is not None
            else None
        ),
    )
    # We only make API calls, so there is no session to refresh or persist.
    options = AsyncClientOptions(
        auto_refresh_token=False, persist_session=False, httpx_client=http_client
    )
    if authorization is not None:
        options.headers = {**options.headers, "Authorization": authorization}
    try:
        return await acreate_client(settings.SUPABASE_URL, api_key, options)
    except BaseException:
        await http_client.aclose()
        raise


async def user_client(settings: Settings, access_token: str) -> AsyncClient:
    """Return a client that calls PostgREST and Storage as the user who owns `access_token`.

    It carries the publishable key as `apikey` and the user's JWT as the bearer token, so RLS
    sees `auth.uid()`. A database request that gets no answer within
    `SUPABASE_HTTP_TIMEOUT_SECONDS` fails with an httpx timeout. Call `close_client` when the
    request is done.
    """
    return await _create_client(
        settings,
        settings.SUPABASE_PUBLISHABLE_KEY,
        authorization=f"Bearer {access_token}",
        database_timeout=_database_timeout(settings),
    )


async def anon_client(settings: Settings) -> AsyncClient:
    """Return a client that calls PostgREST as `anon`: the publishable key and no user.

    `anon` has no grants on our tables, so this can only call the few functions granted to it,
    such as `signups_allowed`. Call `close_client` when the request is done.
    """
    return await _create_client(
        settings,
        settings.SUPABASE_PUBLISHABLE_KEY,
        database_timeout=_database_timeout(settings),
    )


async def service_client(settings: Settings) -> AsyncClient:
    """Return a client that bypasses RLS with the secret key. **Worker only.**

    The API must never call this: its requests have to run as the user.
    """
    if not settings.SUPABASE_SECRET_KEY:
        raise RuntimeError("SUPABASE_SECRET_KEY is not set; the service client needs it")
    return await _create_client(
        settings, settings.SUPABASE_SECRET_KEY, database_timeout=_database_timeout(settings)
    )


async def close_client(client: AsyncClient) -> None:
    """Close the HTTP connections of a client built by this module."""
    if client.options.httpx_client is not None:
        await client.options.httpx_client.aclose()


async def database_is_reachable(settings: Settings) -> bool:
    """Run one cheap query with the publishable key and no user, and say whether it got through.

    `anon` has no grants on our tables, so a healthy database answers "permission denied". That
    reply still proves the round trip from PostgREST to Postgres. Any other error means the
    query didn't get through.
    """
    try:
        client = await _create_client(
            settings, settings.SUPABASE_PUBLISHABLE_KEY, http_timeout=_HEALTH_TIMEOUT
        )
    except Exception:
        logger.warning("database health check could not build a client", exc_info=True)
        return False
    try:
        # One attempt: the client would otherwise retry a 503 for seven seconds, and the monitor
        # wants the answer now.
        await client.table("images").select("id").limit(1).retry(False).execute()
    except APIError as error:
        if error.code == _INSUFFICIENT_PRIVILEGE:
            return True
        logger.warning("database health check failed", extra={"code": error.code})
        return False
    except Exception:
        logger.warning("database health check failed", exc_info=True)
        return False
    finally:
        await close_client(client)
    return True
