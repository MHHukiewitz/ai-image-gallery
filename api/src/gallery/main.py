"""FastAPI app factory. Run with ``uvicorn gallery.main:app``."""

from fastapi import FastAPI

from gallery.config import get_settings
from gallery.observability import init_observability


def create_app() -> FastAPI:
    """Build the API application. Routers are mounted here by later tickets."""
    init_observability(get_settings(), "api")
    return FastAPI(
        title="AI Image Gallery API",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )


app = create_app()
