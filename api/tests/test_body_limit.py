import json
from collections.abc import Iterable

import pytest
from starlette.types import Message, Receive, Scope, Send

from gallery.api.body_limit import MULTIPART_ALLOWANCE_BYTES, BodyLimitMiddleware
from gallery.config import get_settings

MEGABYTE = 1024 * 1024
# With MAX_UPLOAD_MB=1, the fixture below.
LIMIT = MEGABYTE + MULTIPART_ALLOWANCE_BYTES


@pytest.fixture(autouse=True)
def one_megabyte_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()


class ReadingApp:
    """Reads the whole body, then answers 200. Records whether it ran and what it read."""

    def __init__(self) -> None:
        self.entered = False
        self.received = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.entered = True
        more_body = True
        while more_body:
            message = await receive()
            self.received += len(message.get("body", b""))
            more_body = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class Client:
    """Feeds `chunks` to the middleware as the request body and collects the response."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = list(chunks)
        self.chunks_read = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        if self.chunks_read == len(self.chunks):
            return {"type": "http.disconnect"}
        chunk = self.chunks[self.chunks_read]
        self.chunks_read += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": self.chunks_read < len(self.chunks),
        }

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        assert len(starts) == 1
        return int(starts[0]["status"])

    def json(self) -> object:
        return json.loads(b"".join(m.get("body", b"") for m in self.sent if "body" in m))


def http_scope(content_length: int | None = None) -> Scope:
    headers = [(b"content-type", b"multipart/form-data; boundary=x")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {"type": "http", "method": "POST", "path": "/api/v1/images", "headers": headers}


async def call(app: ReadingApp, scope: Scope, client: Client) -> None:
    await BodyLimitMiddleware(app)(scope, client.receive, client.send)


async def test_a_content_length_over_the_limit_is_a_413_and_the_app_never_runs() -> None:
    app = ReadingApp()
    client = Client([b"x" * (LIMIT + 1)])

    await call(app, http_scope(content_length=LIMIT + 1), client)

    assert client.status == 413
    assert client.json() == {
        "error": {"code": "too_large", "message": "The file is larger than 1 MB."}
    }
    assert app.entered is False
    assert client.chunks_read == 0


async def test_a_content_length_at_the_limit_reaches_the_app() -> None:
    app = ReadingApp()
    client = Client([b"x" * LIMIT])

    await call(app, http_scope(content_length=LIMIT), client)

    assert client.status == 200
    assert app.received == LIMIT


async def test_a_body_without_content_length_is_cut_off_once_it_passes_the_limit() -> None:
    app = ReadingApp()
    chunk = b"x" * (64 * 1024)
    # Far more than the limit, as a client streaming a chunked upload would send.
    client = Client([chunk] * 100)

    await call(app, http_scope(), client)

    assert client.status == 413
    assert client.json() == {
        "error": {"code": "too_large", "message": "The file is larger than 1 MB."}
    }
    # The middleware stops at the first chunk past the limit instead of draining the rest.
    assert client.chunks_read == LIMIT // len(chunk) + 1


async def test_a_body_without_content_length_within_the_limit_reaches_the_app() -> None:
    app = ReadingApp()
    client = Client([b"x" * 1000] * 3)

    await call(app, http_scope(), client)

    assert client.status == 200
    assert app.received == 3000


async def test_an_error_response_the_app_makes_of_the_cut_off_is_replaced_by_the_413() -> None:
    async def app_answering_400(scope: Scope, receive: Receive, send: Send) -> None:
        # Like FastAPI, which turns a failure to read the body into a 400.
        try:
            while (await receive()).get("more_body"):
                pass
        except Exception:
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    client = Client([b"x" * MEGABYTE] * 3)

    await BodyLimitMiddleware(app_answering_400)(http_scope(), client.receive, client.send)

    assert client.status == 413
    assert client.json()["error"]["code"] == "too_large"


async def test_an_error_unrelated_to_the_body_still_propagates() -> None:
    async def broken_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("a bug")

    client = Client([b"small"])

    with pytest.raises(RuntimeError, match="a bug"):
        await BodyLimitMiddleware(broken_app)(http_scope(), client.receive, client.send)


async def test_other_scopes_pass_through_untouched() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    client = Client([])

    await BodyLimitMiddleware(app)({"type": "lifespan"}, client.receive, client.send)

    assert seen == ["lifespan"]
