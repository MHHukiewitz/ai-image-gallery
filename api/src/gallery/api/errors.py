"""Map every error to the one response shape: ``{"error": {"code", "message"}}``.

Domain errors carry messages we wrote, so they pass through. Anything else, such as provider
errors and bugs, gets a generic message: the details go to the log, never to the client.
"""

import logging
import re
from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gallery.api.schemas import ErrorDetail, ErrorResponse
from gallery.domain.errors import (
    AnalyzerError,
    AuthError,
    InvalidImageError,
    NotFoundError,
    QuotaExceededError,
    ShareRefusedError,
)

logger = logging.getLogger(__name__)

# Uploads that fail on size get 413. Every other validation failure is a media type problem.
_PAYLOAD_TOO_LARGE_CODES = frozenset({"too_large", "too_many_pixels"})

_SHARE_REFUSAL_STATUS = {"no_account": 404, "already_shared": 409, "own_address": 422}


def _error_response(
    status_code: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(), headers=dict(headers or {})
    )


def _status_code_name(status_code: int) -> str:
    """Return a stable snake_case code for an HTTP status, such as ``method_not_allowed``."""
    try:
        return re.sub(r"[^a-z0-9]+", "_", HTTPStatus(status_code).phrase.lower()).strip("_")
    except ValueError:
        return f"http_{status_code}"


async def _handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return _error_response(404, "not_found", str(exc) or "Not found.")


async def _handle_invalid_image(request: Request, exc: InvalidImageError) -> JSONResponse:
    status_code = 413 if exc.code in _PAYLOAD_TOO_LARGE_CODES else 415
    return _error_response(status_code, exc.code, exc.message)


async def _handle_quota_exceeded(request: Request, exc: QuotaExceededError) -> JSONResponse:
    # The message is for the user; Retry-After is the wait, in seconds, that the client may honour.
    return _error_response(
        429,
        "quota_exceeded",
        exc.message,
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


async def _handle_share_refused(request: Request, exc: ShareRefusedError) -> JSONResponse:
    return _error_response(_SHARE_REFUSAL_STATUS[exc.code], exc.code, exc.message)


async def _handle_auth(request: Request, exc: AuthError) -> JSONResponse:
    return _error_response(
        401,
        "unauthorized",
        str(exc) or "Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _handle_analyzer(request: Request, exc: AnalyzerError) -> JSONResponse:
    # A retryable failure is the provider's bad moment. A permanent one points at our setup.
    log = logger.warning if exc.retryable else logger.error
    log("image analyzer failed", extra={"retryable": exc.retryable}, exc_info=exc)
    return _error_response(502, "analyzer_error", "The image analysis service failed.")


async def _handle_not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
    logger.warning("not implemented", extra={"path": request.url.path}, exc_info=exc)
    return _error_response(501, "not_implemented", "This is not implemented yet.")


async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    message = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    return _error_response(422, "validation_error", message)


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else _status_code_name(exc.status_code)
    return _error_response(
        exc.status_code, _status_code_name(exc.status_code), message, headers=exc.headers
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", extra={"method": request.method, "path": request.url.path})
    return _error_response(500, "internal_error", "Something went wrong on our side.")


def register_error_handlers(app: FastAPI) -> None:
    """Make every error the app can raise, even a bug, answer in the error shape."""
    app.add_exception_handler(NotFoundError, _handle_not_found)
    app.add_exception_handler(InvalidImageError, _handle_invalid_image)
    app.add_exception_handler(QuotaExceededError, _handle_quota_exceeded)
    app.add_exception_handler(ShareRefusedError, _handle_share_refused)
    app.add_exception_handler(AuthError, _handle_auth)
    app.add_exception_handler(AnalyzerError, _handle_analyzer)
    app.add_exception_handler(NotImplementedError, _handle_not_implemented)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
