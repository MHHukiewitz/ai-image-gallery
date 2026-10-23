"""Site settings routes with the Supabase adapter replaced by a mock: status codes and shapes.

The real service and error handlers run. Only the repo is replaced, so none of these tests
touches Supabase. The ones that leave the repo alone check which routes need a sign-in.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gallery.api import deps
from gallery.domain.errors import ForbiddenError
from gallery.domain.models import SiteSettings
from gallery.ports import SiteSettingsRepo

SETTINGS_URL = "/api/v1/admin/settings"
FORBIDDEN = {"error": {"code": "forbidden", "message": "Only an admin can do this."}}
CHANGED_AT = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> AsyncMock:
    return AsyncMock(spec=SiteSettingsRepo)


@pytest.fixture
def app() -> FastAPI:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI, repo: AsyncMock) -> Iterator[TestClient]:
    app.dependency_overrides[deps.get_site_settings_repo] = lambda: repo
    app.dependency_overrides[deps.get_public_site_settings_repo] = lambda: repo
    yield TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("allowed", [True, False])
def test_signup_status_reports_the_switch(
    client: TestClient, repo: AsyncMock, allowed: bool
) -> None:
    repo.signups_allowed.return_value = allowed

    response = client.get("/api/v1/signups")

    assert response.status_code == 200
    assert response.json() == {"allow_signups": allowed}


def test_signup_status_needs_no_sign_in(app: FastAPI, repo: AsyncMock) -> None:
    # Only the anon repo is replaced: a route that wanted a user would answer 401 here.
    app.dependency_overrides[deps.get_public_site_settings_repo] = lambda: repo
    repo.signups_allowed.return_value = False

    response = TestClient(app).get("/api/v1/signups")

    assert response.status_code == 200
    assert response.json() == {"allow_signups": False}


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/v1/account"), ("GET", SETTINGS_URL), ("PUT", SETTINGS_URL)],
)
def test_account_and_admin_routes_need_a_sign_in(app: FastAPI, method: str, path: str) -> None:
    response = TestClient(app).request(method, path, json={"allow_signups": False})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("admin", [True, False])
def test_account_says_whether_the_caller_is_an_admin(
    client: TestClient, repo: AsyncMock, admin: bool
) -> None:
    repo.caller_is_admin.return_value = admin

    response = client.get("/api/v1/account")

    assert response.status_code == 200
    assert response.json() == {"is_admin": admin}


def test_an_admin_reads_the_settings(client: TestClient, repo: AsyncMock) -> None:
    repo.get.return_value = SiteSettings(allow_signups=True, updated_at=CHANGED_AT)

    response = client.get(SETTINGS_URL)

    assert response.status_code == 200
    assert response.json() == {"allow_signups": True, "updated_at": "2026-10-23T09:00:00Z"}


def test_an_admin_closes_signups(client: TestClient, repo: AsyncMock) -> None:
    repo.set_allow_signups.return_value = SiteSettings(allow_signups=False, updated_at=CHANGED_AT)

    response = client.put(SETTINGS_URL, json={"allow_signups": False})

    assert response.status_code == 200
    assert response.json() == {"allow_signups": False, "updated_at": "2026-10-23T09:00:00Z"}
    repo.set_allow_signups.assert_awaited_once_with(False)


@pytest.mark.parametrize(("method", "attribute"), [("GET", "get"), ("PUT", "set_allow_signups")])
def test_anyone_else_gets_403(
    client: TestClient, repo: AsyncMock, method: str, attribute: str
) -> None:
    getattr(repo, attribute).side_effect = ForbiddenError("Only an admin can do this.")

    response = client.request(method, SETTINGS_URL, json={"allow_signups": False})

    assert response.status_code == 403
    assert response.json() == FORBIDDEN


@pytest.mark.parametrize("body", [{}, {"allow_signups": "maybe"}, {"allow_signups": None}])
def test_the_switch_must_be_a_boolean(client: TestClient, repo: AsyncMock, body: object) -> None:
    response = client.put(SETTINGS_URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    repo.set_allow_signups.assert_not_awaited()
