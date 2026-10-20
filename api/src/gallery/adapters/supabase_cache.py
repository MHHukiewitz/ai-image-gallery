"""AnalysisCache on the `ai_cache` table, keyed by image content and analyzer.

The table has RLS with no policies and grants only to `service_role`, so the client must hold
the secret key.
"""

import logging
from typing import Any

from gallery.adapters.supabase_client import AsyncClient
from gallery.domain.models import Analysis

logger = logging.getLogger(__name__)

_TABLE = "ai_cache"


class SupabaseAnalysisCache:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def get(self, content_sha256: str, analyzer_id: str) -> Analysis | None:
        """Return the cached tags and description, or None on a miss.

        A row that doesn't have the expected shape counts as a miss, so the next `put` repairs it.
        """
        response = (
            await self._client.table(_TABLE)
            .select("result")
            .eq("content_sha256", content_sha256)
            .eq("analyzer_id", analyzer_id)
            .limit(1)
            .execute()
        )
        rows: list[dict[str, Any]] = response.data
        if not rows:
            return None
        try:
            result = rows[0]["result"]
            return Analysis(tags=tuple(result["tags"]), description=result["description"])
        except (KeyError, TypeError):
            logger.warning(
                "ignoring a malformed ai_cache row",
                extra={"content_sha256": content_sha256, "analyzer_id": analyzer_id},
            )
            return None

    async def put(self, content_sha256: str, analyzer_id: str, analysis: Analysis) -> None:
        """Store tags and description. The usage numbers belong to one call and are not cached."""
        await (
            self._client.table(_TABLE)
            .upsert(
                {
                    "content_sha256": content_sha256,
                    "analyzer_id": analyzer_id,
                    "result": {"tags": list(analysis.tags), "description": analysis.description},
                },
                on_conflict="content_sha256,analyzer_id",
            )
            .execute()
        )
