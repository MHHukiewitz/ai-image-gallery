"""Sharing routes: list, create, accept and end gallery shares, one service call each."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from gallery.api.deps import get_share_repo
from gallery.api.schemas import ErrorResponse, ShareList, ShareOut, ShareRequest, to_share_out
from gallery.ports import ShareRepo
from gallery.services.sharing import SharingService

router = APIRouter()

_NOT_FOUND = {
    "model": ErrorResponse,
    "description": "No share with this id that the caller made or received.",
}


def get_sharing_service(shares: Annotated[ShareRepo, Depends(get_share_repo)]) -> SharingService:
    return SharingService(shares)


@router.get("/api/v1/shares", response_model=ShareList)
async def list_shares(
    service: Annotated[SharingService, Depends(get_sharing_service)],
) -> ShareList:
    """List whom the caller shares their gallery with, and who shares theirs with the caller."""
    outgoing, incoming = await service.list()
    return ShareList(
        outgoing=[to_share_out(share) for share in outgoing],
        incoming=[to_share_out(share) for share in incoming],
    )


@router.post(
    "/api/v1/shares",
    status_code=201,
    response_model=ShareOut,
    responses={
        404: {"model": ErrorResponse, "description": "No account uses this address."},
        409: {
            "model": ErrorResponse,
            "description": "The caller's gallery is already shared with this account.",
        },
        422: {
            "model": ErrorResponse,
            "description": "Not an e-mail address, or the caller's own address.",
        },
    },
)
async def create_share(
    body: ShareRequest, service: Annotated[SharingService, Depends(get_sharing_service)]
) -> ShareOut:
    """Ask another account to accept the caller's gallery. It stays pending until they do."""
    return to_share_out(await service.share(body.email))


@router.post(
    "/api/v1/shares/{share_id}/accept",
    status_code=204,
    response_class=Response,
    responses={404: _NOT_FOUND},
)
async def accept_share(
    share_id: int, service: Annotated[SharingService, Depends(get_sharing_service)]
) -> None:
    """Accept a share made to the caller. The owner's images join the caller's gallery."""
    await service.accept(share_id)


@router.delete(
    "/api/v1/shares/{share_id}",
    status_code=204,
    response_class=Response,
    responses={404: _NOT_FOUND},
)
async def delete_share(
    share_id: int, service: Annotated[SharingService, Depends(get_sharing_service)]
) -> None:
    """End a share: the owner revokes it, or the recipient declines or leaves it."""
    await service.remove(share_id)
