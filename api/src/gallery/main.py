"""FastAPI app factory. Run with ``uvicorn gallery.main:app``."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gallery.api.routes import health, images, uploads
from gallery.api.schemas import ErrorDetail, ErrorResponse
from gallery.config import get_settings
from gallery.observability import init_observability


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return every 422 in the one error shape. Minimal until the error mapping ticket (#7)."""
    message = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    body = ErrorResponse(error=ErrorDetail(code="validation_error", message=message))
    return JSONResponse(status_code=422, content=body.model_dump())


def create_app() -> FastAPI:
    """Build the API application and mount every router."""
    init_observability(get_settings(), "api")
    app = FastAPI(
        title="AI Image Gallery API",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(uploads.router)
    app.include_router(images.router)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    return app


app = create_app()
