"""JobQueue on the #4 queue RPCs. The queue is `image_metadata` itself: a row is a job.

The client must hold the secret key: the RPCs are executable by `service_role` only.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.models import AnalysisJob


class SupabaseJobQueue:
    def __init__(self, client: AsyncClient, *, stale_after_seconds: int, max_attempts: int) -> None:
        self._client = client
        self._stale_after_seconds = stale_after_seconds
        self._max_attempts = max_attempts

    async def claim(self, limit: int) -> list[AnalysisJob]:
        """Take up to `limit` due jobs and mark them 'processing'.

        Jobs whose worker went quiet for `stale_after_seconds` are taken over, and the claim
        counts as an attempt.
        """
        response = await self._client.rpc(
            "claim_analysis_jobs",
            {
                "max_jobs": limit,
                "stale_after_seconds": self._stale_after_seconds,
                "max_attempts": self._max_attempts,
            },
        ).execute()
        rows: list[dict[str, Any]] = response.data
        return [
            AnalysisJob(
                image_id=row["image_id"],
                user_id=UUID(row["user_id"]),
                original_path=row["original_path"],
                content_sha256=row["content_sha256"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def complete(
        self,
        image_id: int,
        *,
        tags: Sequence[str],
        description: str,
        colors: Sequence[str],
        analyzer_id: str,
    ) -> None:
        """Store the analysis and mark the job done. A deleted image is not an error."""
        await self._client.rpc(
            "complete_analysis_job",
            {
                "p_image_id": image_id,
                "p_tags": list(tags),
                "p_description": description,
                "p_colors": list(colors),
                "p_analyzer_id": analyzer_id,
            },
        ).execute()

    async def fail(
        self,
        image_id: int,
        *,
        error: str,
        colors: Sequence[str],
        retryable: bool,
    ) -> None:
        """Record a failed attempt.

        The database decides between 'pending' (retryable, attempts left) and 'failed'.
        """
        await self._client.rpc(
            "fail_analysis_job",
            {
                "p_image_id": image_id,
                "p_error": error,
                "p_colors": list(colors),
                "p_retryable": retryable,
                "p_max_attempts": self._max_attempts,
            },
        ).execute()
