"""Every error the API can raise answers in the one error shape."""

import logging
from collections.abc import Callable

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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
