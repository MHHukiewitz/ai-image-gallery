"""Upload route. A stub until the upload service is written."""

from fastapi import APIRouter, HTTPException, UploadFile

from gallery.api.schemas import ErrorResponse, ImageOut

router = APIRouter()


@router.post(
    "/api/v1/images",
    status_code=201,
    response_model=ImageOut,
    responses={
        413: {"model": ErrorResponse, "description": "The file is larger than MAX_UPLOAD_MB."},
        415: {"model": ErrorResponse, "description": "The file is not a JPEG or PNG."},
    },
)
async def upload_image(file: UploadFile) -> ImageOut:
    raise HTTPException(status_code=501)
