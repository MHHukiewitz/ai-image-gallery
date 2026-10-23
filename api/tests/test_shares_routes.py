"""Share routes with the Supabase adapter overridden by a mock: status codes and response shapes.

The real service and error handlers run. Only the share repo is replaced, so none of these tests
touches Supabase or needs a token.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gallery.api import deps
from gallery.domain.errors import ShareRefusedError
from gallery.domain.models import GalleryShare, ShareDirection, ShareStatus
from gallery.ports import ShareRepo

FRIEND_ID = UUID("0d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a")
URL = "/api/v1/shares"
SHARE_NOT_FOUND = {"error": {"code": "not_found", "message": "Share not found."}}


def make_share(
    share_id: int,
    direction: ShareDirection,
    email: str,
    status: ShareStatus = ShareStatus.PENDING,
) -> GalleryShare:
    accepted = status is ShareStatus.ACCEPTED
    return GalleryShare(
        id=share_id,
        direction=direction,
        other_user_id=FRIEND_ID,
        email=email,
        status=status,
        created_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
        accepted_at=datetime(2026, 10, 22, 9, 5, tzinfo=UTC) if accepted else None,
    )


@pytest.fixture
def shares() -> AsyncMock:
    return AsyncMock(spec=ShareRepo)


@pytest.fixture
def client(shares: AsyncMock) -> Iterator[TestClient]:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_share_repo] = lambda: shares
    yield TestClient(app, raise_server_exceptions=False)


# --- GET /api/v1/shares --------------------------------------------------------------------


def test_list_splits_outgoing_from_incoming_shares(client: TestClient, shares: AsyncMock) -> None:
    shares.list.return_value = [
        make_share(3, ShareDirection.INCOMING, "owner@example.test"),
        make_share(2, ShareDirection.OUTGOING, "friend@example.test", ShareStatus.ACCEPTED),
    ]

    response = client.get(URL)

    assert response.status_code == 200
    assert response.json() == {
        "outgoing": [
            {
                "id": 2,
                "email": "friend@example.test",
                "status": "accepted",
                "created_at": "2026-10-22T09:00:00Z",
                "accepted_at": "2026-10-22T09:05:00Z",
            }
        ],
        "incoming": [
            {
                "id": 3,
                "email": "owner@example.test",
                "status": "pending",
                "created_at": "2026-10-22T09:00:00Z",
                "accepted_at": None,
            }
        ],
    }


# --- POST /api/v1/shares -------------------------------------------------------------------


def test_sharing_answers_201_with_the_pending_share(client: TestClient, shares: AsyncMock) -> None:
    shares.create.return_value = make_share(5, ShareDirection.OUTGOING, "friend@example.test")

    response = client.post(URL, json={"email": " Friend@Example.test "})

    assert response.status_code == 201
    assert response.json()["id"] == 5
    assert response.json()["status"] == "pending"
    shares.create.assert_awaited_once_with("friend@example.test")


@pytest.mark.parametrize(
    ("code", "status"),
    [("no_account", 404), ("already_shared", 409), ("own_address", 422)],
)
def test_a_refused_share_answers_in_the_error_shape(
    client: TestClient, shares: AsyncMock, code: str, status: int
) -> None:
    shares.create.side_effect = ShareRefusedError(code, "Refused for a reason.")

    response = client.post(URL, json={"email": "someone@example.test"})

    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": "Refused for a reason."}}


@pytest.mark.parametrize("email", ["", "not-an-address", "two@at@example.test", "a b@example.test"])
def test_something_that_is_not_an_address_is_a_422_before_any_lookup(
    client: TestClient, shares: AsyncMock, email: str
) -> None:
    response = client.post(URL, json={"email": email})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    shares.create.assert_not_awaited()


# --- accept and end ------------------------------------------------------------------------


def test_accept_answers_204_with_no_body(client: TestClient, shares: AsyncMock) -> None:
    shares.accept.return_value = True

    response = client.post(f"{URL}/7/accept")

    assert response.status_code == 204
    assert response.content == b""
    shares.accept.assert_awaited_once_with(7)


def test_accepting_a_share_the_caller_did_not_receive_is_a_404(
    client: TestClient, shares: AsyncMock
) -> None:
    shares.accept.return_value = False

    response = client.post(f"{URL}/7/accept")

    assert response.status_code == 404
    assert response.json() == SHARE_NOT_FOUND


def test_delete_answers_204_with_no_body(client: TestClient, shares: AsyncMock) -> None:
    shares.delete.return_value = True

    response = client.delete(f"{URL}/7")

    assert response.status_code == 204
    assert response.content == b""
    shares.delete.assert_awaited_once_with(7)


def test_deleting_a_missing_share_is_a_404(client: TestClient, shares: AsyncMock) -> None:
    shares.delete.return_value = False

    response = client.delete(f"{URL}/7")

    assert response.status_code == 404
    assert response.json() == SHARE_NOT_FOUND


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", URL), ("POST", URL), ("POST", f"{URL}/7/accept"), ("DELETE", f"{URL}/7")],
)
def test_every_share_route_needs_a_bearer_token(method: str, path: str) -> None:
    # No overrides here: the real auth dependency has to refuse the request first.
    from gallery.main import create_app

    anonymous = TestClient(create_app(), raise_server_exceptions=False)

    response = anonymous.request(method, path, json={"email": "friend@example.test"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
