"""Liveness check, used by Uptime Kuma."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthOut(BaseModel):
    status: str


@router.get("/api/health", response_model=HealthOut)
async def health(deep: bool = False) -> HealthOut:
    """Return liveness. `deep` is accepted, but its database check isn't written yet."""
    return HealthOut(status="ok")
