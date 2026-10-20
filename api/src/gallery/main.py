"""FastAPI app factory. Run with ``uvicorn gallery.main:app``."""

from fastapi import FastAPI

from gallery.api.errors import register_error_handlers
from gallery.api.routes import health, images, uploads
from gallery.api.schemas import ErrorResponse
from gallery.config import get_settings
from gallery.observability import init_observability


def create_app() -> FastAPI:
    """Build the API application and mount every router."""
    init_observability(get_settings(), "api")
    app = FastAPI(
        title="AI Image Gallery API",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        # The error handlers return this shape for every 422, so document it on every route.
        responses={422: {"model": ErrorResponse, "description": "Invalid request."}},
    )
    app.include_router(health.router)
    app.include_router(uploads.router)
    app.include_router(images.router)
    register_error_handlers(app)
    return app


app = create_app()
