"""Refuse a request body larger than an upload may be, before anything reads it.

Starlette spools multipart file parts to disk with no size limit, and in production nothing in
front of the API caps the body either. Without this middleware, anyone could fill the
container's disk, signed in or not.
"""

import logging

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gallery.api.schemas import ErrorDetail, ErrorResponse
from gallery.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Room for the multipart boundaries, part headers and filename around a file of the full size.
MULTIPART_ALLOWANCE_BYTES = 64 * 1024


def max_body_bytes(settings: Settings) -> int:
    """Return the largest request body the API accepts: one upload of the full size, framed."""
    return settings.MAX_UPLOAD_MB * 1024 * 1024 + MULTIPART_ALLOWANCE_BYTES


class _BodyTooLarge(Exception):
    """Raised from `receive` to stop whatever is reading the body."""


class BodyLimitMiddleware:
    """Answer 413 when a request body passes `max_body_bytes`.

    A `Content-Length` over the limit is refused before the app runs. A body without one, such
    as a chunked upload, is counted as the app reads it and cut off once it passes the limit.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        limit = max_body_bytes(settings)
        content_length = Headers(scope=scope).get("content-length", "")
        if content_length.isdigit() and int(content_length) > limit:
            logger.info(
                "refused a request body over the limit",
                extra={"path": scope["path"], "content_length": int(content_length)},
            )
            await _send_too_large(send, settings)
            return

        received_bytes = 0
        cut_off = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, cut_off
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > limit:
                    cut_off = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            # The app may turn the cut-off into an error response of its own, such as FastAPI's
            # 400 for a body it could not read. The 413 below replaces it.
            if cut_off and not response_started:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # Whatever the app raised after the cut-off, the cut-off caused it.
            if not cut_off:
                raise
        if cut_off:
            logger.info("cut off a request body over the limit", extra={"path": scope["path"]})
            # A response the app began before the body ran over can't be replaced.
            if not response_started:
                await _send_too_large(send, settings)


async def _send_too_large(send: Send, settings: Settings) -> None:
    # The same code and message as the upload validation's 413, so the web client handles both
    # alike.
    body = ErrorResponse(
        error=ErrorDetail(
            code="too_large", message=f"The file is larger than {settings.MAX_UPLOAD_MB} MB."
        )
    )
    content = body.model_dump_json().encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(content)).encode()),
                # The rest of the body is never read, so the connection can't carry another request.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": content})
