"""ShareRepo on PostgREST.

The client carries the user's JWT. Creating, accepting and listing go through RPCs, because they
need auth.users or a check that RLS can't express (only the recipient accepts). Ending a share is a
plain delete, which the table's RLS allows to both sides.

Only the calls that are safe to repeat are retried: the listing and the accept. Creating and
deleting aren't, because a second run after a lost answer would report a conflict or a miss.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from postgrest import APIError

from gallery.adapters.retry import RetryPolicy, retry_transient, transient_database_reason
from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.errors import ShareRefusedError
from gallery.domain.models import GalleryShare, ShareDirection, ShareStatus

logger = logging.getLogger(__name__)

_SHARES = "gallery_shares"

# share_gallery raises PT<status>, which PostgREST answers with that status and keeps as the code.
# Matching the code, not the HTTP status, keeps a gateway's own 404 or 409 from reading as one.
_REFUSALS: dict[str, ShareRefusedError] = {
    "PT404": ShareRefusedError("no_account", "No account uses this e-mail address."),
    "PT409": ShareRefusedError(
        "already_shared", "Your gallery is already shared with this address."
    ),
    "PT422": ShareRefusedError("own_address", "That is your own e-mail address."),
}

_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.2, cap_delay_seconds=1.0)


def _row_to_share(row: dict[str, Any]) -> GalleryShare:
    """Map one row of list_gallery_shares or share_gallery, which return the same columns."""
    accepted_at = row.get("accepted_at")
    return GalleryShare(
        id=row["id"],
        direction=ShareDirection(row["direction"]),
        other_user_id=UUID(row["other_user_id"]),
        email=row["email"],
        status=ShareStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        accepted_at=datetime.fromisoformat(accepted_at) if accepted_at else None,
    )


class SupabaseShareRepo:
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

    async def list(self) -> list[GalleryShare]:
        """Return the caller's shares in both directions, newest first."""

        async def call() -> list[GalleryShare]:
            response = await self._client.rpc("list_gallery_shares", {}).execute()
            return [_row_to_share(row) for row in response.data]

        return await self._retry(call, "list_shares")

    async def create(self, email: str) -> GalleryShare:
        """Share the caller's gallery with the account that uses `email`.

        Raises ShareRefusedError when the database refuses: no account uses the address, it is
        the caller's own, or the caller already shares with it.
        """
        try:
            response = await self._client.rpc("share_gallery", {"p_email": email}).execute()
        except APIError as error:
            refusal = _REFUSALS.get(error.code or "")
            if refusal is None:
                raise
            # The address is the other user's personal data, so it stays out of the log.
            logger.info("refused a gallery share", extra={"reason": refusal.code})
            raise ShareRefusedError(refusal.code, refusal.message) from error
        return _row_to_share(response.data[0])

    async def accept(self, share_id: int) -> bool:
        """Accept a share made to the caller. False when there is no such share."""

        async def call() -> bool:
            response = await self._client.rpc(
                "accept_gallery_share", {"p_share_id": share_id}
            ).execute()
            # The RPC returns a bare boolean. Compare strictly so a surprising payload can't pass.
            return response.data is True

        return await self._retry(call, "accept_share")

    async def delete(self, share_id: int) -> bool:
        """End a share the caller made or received. False when there is none."""
        response = await self._client.table(_SHARES).delete().eq("id", share_id).execute()
        return bool(response.data)

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
