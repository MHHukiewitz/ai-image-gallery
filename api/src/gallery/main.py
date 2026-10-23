"""FastAPI app factory. Run with ``uvicorn gallery.main:app``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from PIL import Image

from gallery.api.body_limit import BodyLimitMiddleware
from gallery.api.errors import register_error_handlers
from gallery.api.routes import health, images, shares, uploads
from gallery.api.schemas import ErrorResponse
from gallery.config import get_settings
from gallery.observability import init_observability


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # make_thumbnail decodes uploads here, so Pillow's own bomb check should match our limit. Set
    # at startup rather than on import, so building the app (as the tests do) leaves Pillow alone.
    Image.MAX_IMAGE_PIXELS = get_settings().MAX_IMAGE_PIXELS
    yield


def create_app() -> FastAPI:
    """Build the API application and mount every router."""
    init_observability(get_settings(), "api")
    app = FastAPI(
        title="AI Image Gallery API",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        # The error handlers return this shape for every 422, so document it on every route.
        responses={422: {"model": ErrorResponse, "description": "Invalid request."}},
    )
    app.include_router(health.router)
    app.include_router(uploads.router)
    uploads.document_upload_form(app)
    app.include_router(images.router)
    app.include_router(shares.router)
    register_error_handlers(app)
    app.add_middleware(BodyLimitMiddleware)
    return app


app = create_app()
