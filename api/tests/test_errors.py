"""Every error the API can raise answers in the one error shape."""

import logging
from collections.abc import Callable

import httpx
import pytest
import respx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gallery.adapters.supabase_client import database_is_reachable
from gallery.api.deps import get_database_check
from gallery.config import Settings
from gallery.domain.errors import (
    AnalyzerError,
    AuthError,
    GalleryError,
    InvalidImageError,
    NotFoundError,
)


@pytest.fixture
def app() -> FastAPI:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    return create_app()


@pytest.fixture
def client_for(app: FastAPI) -> Callable[[Exception], TestClient]:
    """Return a client for an app whose `/boom` route raises the given error."""

    def build(error: Exception) -> TestClient:
        @app.get("/boom")
        async def boom() -> None:
            raise error

        # A real server turns an unhandled error into a 500 response. The default client
        # re-raises it into the test instead.
        return TestClient(app, raise_server_exceptions=False)

    return build


def assert_error_shape(response_json: dict[str, object], code: str, message: str) -> None:
    assert response_json == {"error": {"code": code, "message": message}}


@pytest.mark.parametrize(
    ("error", "status_code", "code", "message"),
    [
        (NotFoundError("Image 7 not found."), 404, "not_found", "Image 7 not found."),
        (NotFoundError(), 404, "not_found", "Not found."),
        (
            InvalidImageError("unsupported_type", "Only JPEG and PNG are accepted."),
            415,
            "unsupported_type",
            "Only JPEG and PNG are accepted.",
        ),
        (
            InvalidImageError("corrupt", "The image could not be decoded."),
            415,
            "corrupt",
            "The image could not be decoded.",
        ),
        (
            InvalidImageError("too_large", "The file is larger than 10 MB."),
            413,
            "too_large",
            "The file is larger than 10 MB.",
        ),
        (
            InvalidImageError("too_many_pixels", "The image has more than 40 megapixels."),
            413,
            "too_many_pixels",
            "The image has more than 40 megapixels.",
        ),
        (AuthError("Missing bearer token."), 401, "unauthorized", "Missing bearer token."),
        (AuthError(), 401, "unauthorized", "Not authenticated."),
        (
            NotImplementedError("request_reanalysis is not implemented yet"),
            501,
            "not_implemented",
            "This is not implemented yet.",
        ),
        (HTTPException(status_code=404), 404, "not_found", "Not Found"),
        (
            HTTPException(status_code=403, detail="Not yours."),
            403,
            "forbidden",
            "Not yours.",
        ),
        (
            HTTPException(status_code=503, detail="Try again later."),
            503,
            "service_unavailable",
            "Try again later.",
        ),
        (
            HTTPException(status_code=418, detail={"anything": "else"}),
            418,
            "i_m_a_teapot",
            "i_m_a_teapot",
        ),
    ],
)
def test_each_error_maps_to_its_status_and_shape(
    client_for: Callable[[Exception], TestClient],
    error: Exception,
    status_code: int,
    code: str,
    message: str,
) -> None:
    response = client_for(error).get("/boom")

    assert response.status_code == status_code
    assert_error_shape(response.json(), code, message)


def test_an_analyzer_error_is_a_502_that_hides_the_provider_details(
    client_for: Callable[[Exception], TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    error = AnalyzerError("rate limited by api.example.test, org org-secret-123", retryable=True)

    with caplog.at_level(logging.WARNING):
        response = client_for(error).get("/boom")

    assert response.status_code == 502
    assert_error_shape(response.json(), "analyzer_error", "The image analysis service failed.")
    assert "org-secret-123" not in response.text
    # A retryable failure is a warning, so it stays out of Sentry.
    [record] = [r for r in caplog.records if r.getMessage() == "image analyzer failed"]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert record.exc_info[1] is error


def test_a_permanent_analyzer_failure_is_logged_as_an_error(
    client_for: Callable[[Exception], TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        response = client_for(AnalyzerError("invalid api key", retryable=False)).get("/boom")

    assert response.status_code == 502
    [record] = [r for r in caplog.records if r.getMessage() == "image analyzer failed"]
    assert record.levelno == logging.ERROR


def test_an_auth_error_asks_the_client_to_send_a_bearer_token(
    client_for: Callable[[Exception], TestClient],
) -> None:
    response = client_for(AuthError("Invalid or expired access token.")).get("/boom")

    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_an_http_exception_keeps_its_headers(
    client_for: Callable[[Exception], TestClient],
) -> None:
    error = HTTPException(status_code=429, detail="Slow down.", headers={"Retry-After": "7"})

    response = client_for(error).get("/boom")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert_error_shape(response.json(), "too_many_requests", "Slow down.")


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("connection to db.internal.example.test:5432 refused"),
        GalleryError("a domain error nobody maps"),
        KeyError("not-a-real-secret-value"),
    ],
)
def test_an_unhandled_error_is_a_generic_500_and_is_logged(
    client_for: Callable[[Exception], TestClient],
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    with caplog.at_level(logging.ERROR):
        response = client_for(error).get("/boom")

    assert response.status_code == 500
    assert_error_shape(response.json(), "internal_error", "Something went wrong on our side.")
    # The details are for the log, never for the client.
    assert str(error) not in response.text
    [record] = [r for r in caplog.records if r.getMessage() == "unhandled error"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is error


def test_an_unknown_path_is_a_404_in_the_error_shape(
    client_for: Callable[[Exception], TestClient],
) -> None:
    response = client_for(RuntimeError()).get("/api/nope")

    assert response.status_code == 404
    assert_error_shape(response.json(), "not_found", "Not Found")


def test_a_wrong_method_is_a_405_in_the_error_shape(
    client_for: Callable[[Exception], TestClient],
) -> None:
    response = client_for(RuntimeError()).post("/api/health")

    assert response.status_code == 405
    assert_error_shape(response.json(), "method_not_allowed", "Method Not Allowed")
    assert response.headers["Allow"] == "GET"


def test_a_validation_error_is_a_422_in_the_error_shape(
    client_for: Callable[[Exception], TestClient],
) -> None:
    response = client_for(RuntimeError()).get("/api/v1/images?limit=51")

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "validation_error"
    assert "limit" in body["error"]["message"]


# --- the deep health check -----------------------------------------------------------------


def _health_client(app: FastAPI, *, database_answers: bool) -> tuple[TestClient, list[str]]:
    """Return a client whose database check gives the given answer, and the log of its calls."""
    calls: list[str] = []

    async def check() -> bool:
        calls.append("checked")
        return database_answers

    app.dependency_overrides[get_database_check] = lambda: check
    return TestClient(app), calls


def test_the_shallow_health_check_does_not_touch_the_database(app: FastAPI) -> None:
    client, calls = _health_client(app, database_answers=False)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == []


def test_the_deep_health_check_reports_a_working_database(app: FastAPI) -> None:
    client, calls = _health_client(app, database_answers=True)

    response = client.get("/api/health?deep=1")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
    assert calls == ["checked"]


def test_the_deep_health_check_answers_503_when_the_database_does_not(app: FastAPI) -> None:
    client, _ = _health_client(app, database_answers=False)

    response = client.get("/api/health?deep=1")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error"}


def test_the_health_check_leaves_the_openapi_schema_alone(app: FastAPI) -> None:
    # The web types are generated from the schema, so the deep body must not change it.
    schema = app.openapi()["components"]["schemas"]["HealthOut"]

    assert set(schema["properties"]) == {"status"}


POSTGREST_IMAGES_URL = "http://localhost:54321/rest/v1/images"


def _postgrest_error(status_code: int, code: str, message: str) -> httpx.Response:
    # PostgREST always sends all four fields, and the client only parses the full shape.
    body = {"code": code, "message": message, "details": None, "hint": None}
    return httpx.Response(status_code, json=body)


@pytest.mark.parametrize(
    ("reply", "reachable"),
    [
        # `anon` has no grants, so a healthy database refuses the query. That's still an answer.
        (_postgrest_error(401, "42501", "permission denied for table images"), True),
        (httpx.Response(200, json=[]), True),
        (_postgrest_error(401, "PGRST301", "JWSError JWSInvalidSignature"), False),
        (_postgrest_error(503, "PGRST002", "Could not query the database"), False),
        (_postgrest_error(404, "PGRST205", "Could not find the table"), False),
        (httpx.Response(502, text="Bad Gateway"), False),
    ],
)
async def test_the_database_check_reads_the_reply(
    respx_mock: respx.MockRouter, reply: httpx.Response, reachable: bool
) -> None:
    respx_mock.get(POSTGREST_IMAGES_URL).mock(return_value=reply)
    settings = Settings(SUPABASE_URL="http://localhost:54321", SUPABASE_PUBLISHABLE_KEY="test-key")

    assert await database_is_reachable(settings) is reachable


async def test_the_database_check_asks_as_the_publishable_key_with_no_user(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(POSTGREST_IMAGES_URL).mock(return_value=httpx.Response(200, json=[]))
    settings = Settings(SUPABASE_URL="http://localhost:54321", SUPABASE_PUBLISHABLE_KEY="test-key")

    await database_is_reachable(settings)

    headers = route.calls.last.request.headers
    assert headers["apikey"] == "test-key"
    assert headers["authorization"] == "Bearer test-key"


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("hung")])
async def test_the_database_check_is_false_when_the_database_cannot_be_reached(
    respx_mock: respx.MockRouter, failure: httpx.HTTPError
) -> None:
    respx_mock.get(POSTGREST_IMAGES_URL).mock(side_effect=failure)
    settings = Settings(SUPABASE_URL="http://localhost:54321", SUPABASE_PUBLISHABLE_KEY="test-key")

    assert await database_is_reachable(settings) is False
