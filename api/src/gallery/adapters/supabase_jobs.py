"""JobQueue on the `jobs` table's RPCs (#89), with analysis as its first kind of job.

The client must hold the secret key: the RPCs are executable by `service_role` only.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.models import AnalysisStatus, AnalysisTarget, Job


class SupabaseJobQueue:
    def __init__(self, client: AsyncClient, *, stale_after_seconds: int, max_attempts: int) -> None:
        self._client = client
        self._stale_after_seconds = stale_after_seconds
        self._max_attempts = max_attempts

    async def claim(self, kinds: Sequence[str], limit: int) -> list[Job]:
        """Take up to `limit` due jobs of `kinds` and mark them 'processing'.

        Jobs whose worker went quiet for `stale_after_seconds` are taken over, and the claim
        counts as an attempt.
        """
        response = await self._client.rpc(
            "claim_jobs",
            {
                "p_kinds": list(kinds),
                "p_limit": limit,
                "p_stale_after_seconds": self._stale_after_seconds,
                "p_max_attempts": self._max_attempts,
            },
        ).execute()
        rows: list[dict[str, Any]] = response.data
        return [
            Job(
                id=row["id"],
                kind=row["kind"],
                image_id=row["image_id"],
                user_id=UUID(row["user_id"]) if row["user_id"] else None,
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def complete(self, job_id: int) -> None:
        """Mark the job done. A job that is gone is not an error."""
        await self._client.rpc("complete_job", {"p_job_id": job_id}).execute()

    async def fail(self, job_id: int, *, error: str, retryable: bool) -> None:
        """Record a failed attempt.

        The database decides between 'pending' (retryable, attempts left) and 'failed'.
        """
        await self._client.rpc(
            "fail_job",
            {
                "p_job_id": job_id,
                "p_error": error,
                "p_max_attempts": self._max_attempts,
                "p_retryable": retryable,
            },
        ).execute()

    async def analysis_target(self, job_id: int) -> AnalysisTarget | None:
        """The image of an analyze_image job, or None when the image was deleted."""
        response = await self._client.rpc("analysis_target", {"p_job_id": job_id}).execute()
        rows: list[dict[str, Any]] = response.data
        if not rows:
            return None
        [row] = rows
        return AnalysisTarget(
            image_id=row["image_id"],
            original_path=row["original_path"],
            content_sha256=row["content_sha256"],
            status=AnalysisStatus(row["ai_processing_status"]),
            error=row["last_error"],
        )

    async def complete_analysis(
        self,
        job_id: int,
        *,
        tags: Sequence[str],
        description: str,
        colors: Sequence[str],
        analyzer_id: str,
    ) -> None:
        """Store the analysis on the image and complete its job, in one transaction."""
        await self._client.rpc(
            "complete_analysis",
            {
                "p_job_id": job_id,
                "p_tags": list(tags),
                "p_description": description,
                "p_colors": list(colors),
                "p_analyzer_id": analyzer_id,
            },
        ).execute()

    async def fail_analysis(
        self,
        job_id: int,
        *,
        error: str,
        colors: Sequence[str],
        retryable: bool,
    ) -> None:
        """Record a failed analysis attempt, as `fail` does, and store the colors either way."""
        await self._client.rpc(
            "fail_analysis",
            {
                "p_job_id": job_id,
                "p_error": error,
                "p_colors": list(colors),
                "p_retryable": retryable,
                "p_max_attempts": self._max_attempts,
            },
        ).execute()

    async def store_embedding(self, image_id: int, vector: Sequence[float], model_id: str) -> None:
        """Attach the embedding of the image's current tags and description.

        A deleted image is not an error. The database rejects a vector of the wrong size and an
        all-zero one.
        """
        await self._client.rpc(
            "store_image_embedding",
            {"p_image_id": image_id, "p_embedding": list(vector), "p_model": model_id},
        ).execute()
