"""SiteSettingsRepo on PostgREST.

Every call is an RPC, because site_settings and admins grant nothing to anon or authenticated: the
functions check the caller. signups_allowed is the one anon may call, so the API asks it with a
client that carries no user.

Every call is safe to repeat, including setting the switch to the value it already has, so all of
them retry a transient failure.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from postgrest import APIError

from gallery.adapters.retry import RetryPolicy, retry_transient, transient_database_reason
from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.errors import ForbiddenError
from gallery.domain.models import SiteSettings

logger = logging.getLogger(__name__)

# get_site_settings and set_allow_signups raise PT403 for anyone but an admin, which PostgREST
# answers with HTTP 403 and keeps as the code.
_NOT_ADMIN = "PT403"

_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.2, cap_delay_seconds=1.0)


def _row_to_settings(row: dict[str, Any]) -> SiteSettings:
    return SiteSettings(
        allow_signups=row["allow_signups"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class SupabaseSiteSettingsRepo:
    def __init__(
        self,
        client: AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._client = client
        # Injected so tests can run the retries without waiting or randomness.
        self._sleep = sleep
        self._uniform = uniform

    async def signups_allowed(self) -> bool:
        """Whether new accounts can be created right now."""

        async def call() -> bool:
            response = await self._client.rpc("signups_allowed", {}).execute()
            # A bare boolean. Compare strictly so a surprising payload reads as closed.
            return response.data is True

        return await self._retry(call, "signups_allowed")

    async def caller_is_admin(self) -> bool:
        """Whether the signed-in caller is an admin."""

        async def call() -> bool:
            response = await self._client.rpc("is_admin", {}).execute()
            return response.data is True

        return await self._retry(call, "is_admin")

    async def get(self) -> SiteSettings:
        """The settings. Raises ForbiddenError unless the caller is an admin."""

        async def call() -> SiteSettings:
            response = await self._client.rpc("get_site_settings", {}).execute()
            return _row_to_settings(response.data[0])

        return await self._admin_only(call, "get_site_settings")

    async def set_allow_signups(self, allow: bool) -> SiteSettings:
        """Turn sign-ups on or off. Raises ForbiddenError unless the caller is an admin."""

        async def call() -> SiteSettings:
            response = await self._client.rpc("set_allow_signups", {"p_allow": allow}).execute()
            return _row_to_settings(response.data[0])

        return await self._admin_only(call, "set_allow_signups")

    async def _admin_only(
        self, call: Callable[[], Awaitable[SiteSettings]], operation: str
    ) -> SiteSettings:
        try:
            return await self._retry(call, operation)
        except APIError as error:
            if error.code != _NOT_ADMIN:
                raise
            # Someone tried an admin page. A user mistake or curiosity, not a bug.
            logger.info("refused a site settings call", extra={"operation": operation})
            raise ForbiddenError("Only an admin can do this.") from error

    async def _retry[T](self, call: Callable[[], Awaitable[T]], operation: str) -> T:
        return await retry_transient(
            call,
            operation=operation,
            policy=_RETRY_POLICY,
            transient_reason=transient_database_reason,
            logger=logger,
            message="database call failed, retrying",
            sleep=self._sleep,
            uniform=self._uniform,
        )
