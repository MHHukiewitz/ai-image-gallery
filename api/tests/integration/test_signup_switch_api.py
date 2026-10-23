"""The sign-up switch through the API against the local stack, with real users from Auth.

Admin A closes sign-ups through the API. Then nobody can create an account, not even by calling
Supabase Auth directly with the publishable key, while user B still signs in. B can neither read nor
flip the switch. A opens sign-ups again, and new accounts work again.

Start the stack with `make dev-db`, then export its values and run the tests:

    set -a; eval "$(supabase status -o env)"; set +a
    cd api && uv run pytest -m integration tests/integration/test_signup_switch_api.py

The secret key makes A an admin and deletes the test users at the end. The stack is shared, so the
teardown opens sign-ups again with it too, even if a test failed with them closed.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest

from gallery.api import deps
from gallery.config import Settings

pytestmark = pytest.mark.integration

# conftest.py clears the settings variables before every test, so read the stack's values now,
# while the tests are being collected.
_ENVIRONMENT = dict(os.environ)

_PASSWORD = "test-password-123"
_SETTINGS_URL = "/api/v1/admin/settings"


def _stack_value(*names: str) -> str | None:
    return next((_ENVIRONMENT[name] for name in names if _ENVIRONMENT.get(name)), None)


@pytest.fixture
def stack_settings() -> Settings:
    url = _stack_value("SUPABASE_URL", "API_URL")
    publishable_key = _stack_value("SUPABASE_PUBLISHABLE_KEY", "PUBLISHABLE_KEY", "ANON_KEY")
    secret_key = _stack_value("SUPABASE_SECRET_KEY", "SECRET_KEY", "SERVICE_ROLE_KEY")
    if url is None or publishable_key is None or secret_key is None:
        pytest.fail(
            "The local Supabase stack's values are not in the environment. Run `make dev-db`, "
            'then `set -a; eval "$(supabase status -o env)"; set +a`.'
        )
    return Settings(
        SUPABASE_URL=url,
        SUPABASE_PUBLISHABLE_KEY=publishable_key,
        SUPABASE_SECRET_KEY=secret_key,
        SUPABASE_JWT_SECRET=_stack_value("SUPABASE_JWT_SECRET", "JWT_SECRET"),
    )


@dataclass(frozen=True)
class Tenant:
    id: UUID
    email: str
    headers: dict[str, str]


def _secret_headers(settings: Settings) -> dict[str, str]:
    secret_key = settings.SUPABASE_SECRET_KEY
    assert secret_key is not None
    return {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}


async def _sign_up(http: httpx.AsyncClient, settings: Settings, email: str) -> httpx.Response:
    """Ask Supabase Auth for a new account, as anyone could with the public publishable key."""
    return await http.post(
        "/auth/v1/signup",
        headers={"apikey": settings.SUPABASE_PUBLISHABLE_KEY},
        json={"email": email, "password": _PASSWORD},
    )


def _new_email() -> str:
    return f"{uuid.uuid4().hex}@example.test"


@pytest.fixture
async def auth(stack_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=stack_settings.SUPABASE_URL) as http:
        yield http


@pytest.fixture
async def admin_and_user(
    auth: httpx.AsyncClient, stack_settings: Settings
) -> AsyncIterator[tuple[Tenant, Tenant]]:
    tenants: list[Tenant] = []
    try:
        for _ in range(2):
            email = _new_email()
            response = await _sign_up(auth, stack_settings, email)
            response.raise_for_status()
            body = response.json()
            tenants.append(
                Tenant(
                    id=UUID(body["user"]["id"]),
                    email=email,
                    headers={"Authorization": f"Bearer {body['access_token']}"},
                )
            )
        admin, user = tenants
        made_admin = await auth.post(
            "/rest/v1/admins",
            headers=_secret_headers(stack_settings),
            json={"user_id": str(admin.id)},
        )
        made_admin.raise_for_status()
        yield admin, user
    finally:
        # Other worktrees share this stack: never leave it closed to sign-ups.
        reopened = await auth.patch(
            "/rest/v1/site_settings",
            params={"id": "eq.true"},
            headers=_secret_headers(stack_settings),
            json={"allow_signups": True},
        )
        reopened.raise_for_status()
        for tenant in tenants:
            # Deleting the admin's account also removes their admins row.
            deleted = await auth.delete(
                f"/auth/v1/admin/users/{tenant.id}", headers=_secret_headers(stack_settings)
            )
            deleted.raise_for_status()


@pytest.fixture
async def api(stack_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    # gallery.main builds the app when it is imported, and that reads the settings. Import it
    # here, after conftest's autouse fixture has set them, instead of at module level.
    from gallery.main import create_app

    app = create_app()
    app.dependency_overrides[deps.get_settings] = lambda: stack_settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as http:
        yield http


async def test_an_admin_closes_and_reopens_signups(
    api: httpx.AsyncClient,
    auth: httpx.AsyncClient,
    stack_settings: Settings,
    admin_and_user: tuple[Tenant, Tenant],
) -> None:
    admin, user = admin_and_user

    assert (await api.get("/api/v1/signups")).json() == {"allow_signups": True}
    assert (await api.get("/api/v1/account", headers=admin.headers)).json() == {"is_admin": True}
    assert (await api.get("/api/v1/account", headers=user.headers)).json() == {"is_admin": False}

    closed = await api.put(_SETTINGS_URL, headers=admin.headers, json={"allow_signups": False})
    assert closed.status_code == 200
    assert closed.json()["allow_signups"] is False

    # Anyone can see it, signed in or not.
    assert (await api.get("/api/v1/signups")).json() == {"allow_signups": False}
    # Supabase Auth itself refuses, so hiding the form is not what keeps people out.
    refused = await _sign_up(auth, stack_settings, _new_email())
    assert refused.status_code == 500
    assert refused.json()["message"] == "Sign-ups are closed."
    # An existing user still signs in.
    signed_in = await auth.post(
        "/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": stack_settings.SUPABASE_PUBLISHABLE_KEY},
        json={"email": user.email, "password": _PASSWORD},
    )
    assert signed_in.status_code == 200

    reopened = await api.put(_SETTINGS_URL, headers=admin.headers, json={"allow_signups": True})
    assert reopened.json()["allow_signups"] is True
    read_back = await api.get(_SETTINGS_URL, headers=admin.headers)
    assert read_back.json() == reopened.json()

    new_email = _new_email()
    created = await _sign_up(auth, stack_settings, new_email)
    assert created.status_code == 200
    deleted = await auth.delete(
        f"/auth/v1/admin/users/{created.json()['user']['id']}",
        headers=_secret_headers(stack_settings),
    )
    deleted.raise_for_status()


async def test_a_user_who_is_not_an_admin_can_neither_read_nor_flip_the_switch(
    api: httpx.AsyncClient, admin_and_user: tuple[Tenant, Tenant]
) -> None:
    _, user = admin_and_user

    read = await api.get(_SETTINGS_URL, headers=user.headers)
    flipped = await api.put(_SETTINGS_URL, headers=user.headers, json={"allow_signups": False})

    assert read.status_code == 403
    assert flipped.status_code == 403
    assert flipped.json()["error"]["code"] == "forbidden"
    assert (await api.get("/api/v1/signups")).json() == {"allow_signups": True}
