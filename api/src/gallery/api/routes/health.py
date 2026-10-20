"""Liveness and database health, used by Uptime Kuma."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gallery.api.deps import DatabaseCheck, get_database_check

router = APIRouter()


class HealthOut(BaseModel):
    status: str


class DeepHealthOut(HealthOut):
    """The body of `?deep=1`. It adds a field to HealthOut, so clients of HealthOut still work."""

    db: str


@router.get("/api/health", response_model=HealthOut)
async def health(
    check_database: Annotated[DatabaseCheck, Depends(get_database_check)], deep: bool = False
) -> HealthOut | JSONResponse:
    """Return liveness. With `deep=1`, also check that the database answers.

    The deep check keeps a free-tier Supabase project awake and lets the monitor see an outage
    that the API process itself doesn't notice. It answers 503 when the database doesn't.
    """
    if not deep:
        return HealthOut(status="ok")
    if await check_database():
        return JSONResponse(DeepHealthOut(status="ok", db="ok").model_dump())
    return JSONResponse(DeepHealthOut(status="error", db="error").model_dump(), status_code=503)
