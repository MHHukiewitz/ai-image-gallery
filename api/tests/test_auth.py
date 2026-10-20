"""JWT verification and the per-request Supabase clients. Keys and tokens are generated here."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gallery.adapters.supabase_client import (
    AsyncClient,
    close_client,
    service_client,
    user_client,
)
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.api import deps
from gallery.api.deps import CurrentUser, get_current_user
from gallery.config import Settings
from gallery.ports import ImageRepo, ObjectStore

USER_ID = UUID("7b9e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c")
# Generated per run: HS256 needs at least 32 bytes, and no key-like literal belongs in the repo.
HS256_SECRET = secrets.token_urlsafe(48)
ES256_KID = "test-ec-key"
RS256_KID = "test-rsa-key"
UNAUTHORIZED = {
    "error": {"code": "unauthorized", "message": "Invalid or expired access token."},
}


@pytest.fixture(scope="module")
def ec_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(scope="module")
def rsa_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(algorithm: Any, private_key: Any, kid: str, alg: str) -> dict[str, Any]:
    jwk: dict[str, Any] = algorithm.to_jwk(private_key.public_key(), as_dict=True)
    return jwk | {"kid": kid, "use": "sig", "alg": alg}


@pytest.fixture(autouse=True)
def jwks(
    monkeypatch: pytest.MonkeyPatch,
    ec_private_key: ec.EllipticCurvePrivateKey,
    rsa_private_key: rsa.RSAPrivateKey,
) -> Iterator[dict[str, Any]]:
    """Serve a generated key set instead of fetching Supabase's, so no test hits the network."""
    key_set = {
        "keys": [
            _public_jwk(jwt.algorithms.ECAlgorithm, ec_private_key, ES256_KID, "ES256"),
            _public_jwk(jwt.algorithms.RSAAlgorithm, rsa_private_key, RS256_KID, "RS256"),
        ]
    }
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: key_set)
    deps._jwks_client.cache_clear()
    yield key_set
    deps._jwks_client.cache_clear()


def make_token(
    key: Any = HS256_SECRET,
    *,
    algorithm: str = "HS256",
    headers: dict[str, Any] | None = None,
    **claim_overrides: Any,
) -> str:
    """Sign a token for USER_ID. Pass a claim as None to leave it out."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(USER_ID),
        "aud": "authenticated",
        "role": "authenticated",
        "email": "ada@example.test",
        "iat": now,
        "exp": now + 3600,
    } | claim_overrides
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


def es256_token(key: ec.EllipticCurvePrivateKey, **claim_overrides: Any) -> str:
    return make_token(key, algorithm="ES256", headers={"kid": ES256_KID}, **claim_overrides)


def rs256_token(key: rsa.RSAPrivateKey, **claim_overrides: Any) -> str:
    return make_token(key, algorithm="RS256", headers={"kid": RS256_KID}, **claim_overrides)


@pytest.fixture
def make_app() -> Callable[..., FastAPI]:
    """Return a builder for an app with a `/whoami` route behind `get_current_user`."""

    def build(**settings: Any) -> FastAPI:
        # gallery.main builds the app when it is imported, and that reads the settings. Import
        # it here, after conftest's autouse fixture has set them, instead of at module level.
        from gallery.main import create_app

        app = create_app()
        app.dependency_overrides[deps.get_settings] = lambda: Settings(
            SUPABASE_URL="http://localhost:54321",
            SUPABASE_PUBLISHABLE_KEY="test-key",
            **settings,
        )

        @app.get("/whoami")
        async def whoami(user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict[str, Any]:
            return {"id": str(user.id), "email": user.email, "access_token": user.access_token}

        return app

    return build


@pytest.fixture
def make_client(make_app: Callable[..., FastAPI]) -> Callable[..., TestClient]:
    def build(**settings: Any) -> TestClient:
        # A real server turns an unexpected error into a 500 response. The default client
        # re-raises it into the test instead.
        return TestClient(make_app(**settings), raise_server_exceptions=False)

    return build


def whoami(client: TestClient, token: str | None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.get("/whoami", headers=headers)


# --- accepted tokens ------------------------------------------------------------------------


def test_a_valid_hs256_token_is_accepted(make_client: Callable[..., TestClient]) -> None:
    client = make_client(SUPABASE_JWT_SECRET=HS256_SECRET)
    token = make_token()

    response = whoami(client, token)

    assert response.status_code == 200
    assert response.json() == {
        "id": str(USER_ID),
        "email": "ada@example.test",
        "access_token": token,
    }


def test_a_valid_es256_token_is_accepted_through_the_jwks(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    response = whoami(make_client(), es256_token(ec_private_key))

    assert response.status_code == 200
    assert response.json()["id"] == str(USER_ID)


def test_a_valid_rs256_token_is_accepted_through_the_jwks(
    make_client: Callable[..., TestClient], rsa_private_key: rsa.RSAPrivateKey
) -> None:
    response = whoami(make_client(), rs256_token(rsa_private_key))

    assert response.status_code == 200
    assert response.json()["id"] == str(USER_ID)


def test_an_es256_token_is_still_accepted_when_a_jwt_secret_is_set(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    # `supabase start` can set the shared secret and still sign user tokens with an EC key.
    client = make_client(SUPABASE_JWT_SECRET=HS256_SECRET)

    assert whoami(client, es256_token(ec_private_key)).status_code == 200


def test_the_email_is_optional(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    response = whoami(make_client(), es256_token(ec_private_key, email=None))

    assert response.status_code == 200
    assert response.json()["email"] is None


def test_a_token_that_expired_a_moment_ago_is_accepted_within_the_clock_skew(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    token = es256_token(ec_private_key, exp=int(time.time()) - 3)

    assert whoami(make_client(), token).status_code == 200


# --- rejected tokens ------------------------------------------------------------------------


def assert_unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_missing_token_is_a_401_in_the_error_shape(
    make_client: Callable[..., TestClient],
) -> None:
    response = whoami(make_client(), None)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing bearer token."}
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_non_bearer_scheme_counts_as_a_missing_token(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Missing bearer token."


@pytest.mark.parametrize("garbage", ["garbage", "a.b.c", "not.a.jwt.at-all", "Bearer"])
def test_a_garbage_token_is_a_401_in_the_error_shape(
    make_client: Callable[..., TestClient], garbage: str
) -> None:
    assert_unauthorized(whoami(make_client(SUPABASE_JWT_SECRET=HS256_SECRET), garbage))


def test_an_expired_hs256_token_is_a_401(make_client: Callable[..., TestClient]) -> None:
    token = make_token(exp=int(time.time()) - 3600)

    assert_unauthorized(whoami(make_client(SUPABASE_JWT_SECRET=HS256_SECRET), token))


def test_an_expired_es256_token_is_a_401(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    token = es256_token(ec_private_key, exp=int(time.time()) - 3600)

    assert_unauthorized(whoami(make_client(), token))


@pytest.mark.parametrize("audience", ["anon", "service_role", "https://other.example.test", ""])
def test_the_wrong_audience_is_a_401(
    make_client: Callable[..., TestClient],
    ec_private_key: ec.EllipticCurvePrivateKey,
    audience: str,
) -> None:
    token = es256_token(ec_private_key, aud=audience)

    assert_unauthorized(whoami(make_client(), token))


@pytest.mark.parametrize("claim", ["aud", "exp", "sub"])
def test_a_token_without_a_required_claim_is_a_401(
    make_client: Callable[..., TestClient],
    ec_private_key: ec.EllipticCurvePrivateKey,
    claim: str,
) -> None:
    token = es256_token(ec_private_key, **{claim: None})

    assert_unauthorized(whoami(make_client(), token))


def test_a_subject_that_is_not_a_uuid_is_a_401(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    token = es256_token(ec_private_key, sub="not-a-uuid")

    assert_unauthorized(whoami(make_client(), token))


def test_a_token_signed_with_another_ec_key_is_a_401(
    make_client: Callable[..., TestClient],
) -> None:
    attacker_key = ec.generate_private_key(ec.SECP256R1())

    assert_unauthorized(whoami(make_client(), es256_token(attacker_key)))


def test_a_token_with_a_tampered_payload_is_a_401(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    header, _, signature = es256_token(ec_private_key).split(".")
    forged = es256_token(ec_private_key, sub=str(UUID(int=1))).split(".")[1]

    assert_unauthorized(whoami(make_client(), f"{header}.{forged}.{signature}"))


def test_a_token_with_an_unknown_key_id_is_a_401(
    make_client: Callable[..., TestClient], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    token = make_token(ec_private_key, algorithm="ES256", headers={"kid": "rotated-away"})

    assert_unauthorized(whoami(make_client(), token))


def test_an_hs256_token_with_the_wrong_secret_is_a_401(
    make_client: Callable[..., TestClient],
) -> None:
    token = make_token(secrets.token_urlsafe(48))

    assert_unauthorized(whoami(make_client(SUPABASE_JWT_SECRET=HS256_SECRET), token))


def test_an_hs256_token_is_rejected_when_no_jwt_secret_is_configured(
    make_client: Callable[..., TestClient],
) -> None:
    # Production has no shared secret, so HS256 is not an accepted algorithm there.
    assert_unauthorized(whoami(make_client(), make_token()))


def test_an_unsigned_token_is_a_401(make_client: Callable[..., TestClient]) -> None:
    token = make_token(None, algorithm="none")

    assert_unauthorized(whoami(make_client(SUPABASE_JWT_SECRET=HS256_SECRET), token))


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.mark.parametrize("secret_configured", [False, True])
def test_a_public_key_cannot_be_used_as_an_hmac_secret(
    make_client: Callable[..., TestClient],
    ec_private_key: ec.EllipticCurvePrivateKey,
    secret_configured: bool,
) -> None:
    # The classic algorithm-confusion attack: sign with HS256, using the public key as the
    # secret. PyJWT refuses to build this token, so assemble it by hand.
    public_pem = ec_private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = {"alg": "HS256", "typ": "JWT", "kid": ES256_KID}
    claims = {"sub": str(USER_ID), "aud": "authenticated", "exp": int(time.time()) + 3600}
    signing_input = (
        f"{_base64url(json.dumps(header).encode())}.{_base64url(json.dumps(claims).encode())}"
    )
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    settings = {"SUPABASE_JWT_SECRET": HS256_SECRET} if secret_configured else {}

    assert_unauthorized(whoami(make_client(**settings), f"{signing_input}.{_base64url(signature)}"))


def test_a_rejected_token_never_reaches_the_response_or_the_log(
    make_client: Callable[..., TestClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = make_token(exp=int(time.time()) - 3600)

    with caplog.at_level("DEBUG"):
        response = whoami(make_client(SUPABASE_JWT_SECRET=HS256_SECRET), token)

    assert token not in response.text
    assert token not in caplog.text


def test_an_unreachable_key_server_is_a_503_not_a_401(
    make_client: Callable[..., TestClient],
    ec_private_key: ec.EllipticCurvePrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(self: jwt.PyJWKClient) -> Any:
        raise jwt.PyJWKClientConnectionError("Fail to fetch data from the url")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", unreachable)

    response = whoami(make_client(), es256_token(ec_private_key))

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Sign-in verification is temporarily unavailable.",
        }
    }


# --- per-request clients and providers ---------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        SUPABASE_URL="http://localhost:54321", SUPABASE_PUBLISHABLE_KEY="test-key", **overrides
    )


async def test_the_user_client_carries_the_users_token_and_the_publishable_key() -> None:
    client = await user_client(make_settings(), "user-jwt")

    try:
        for headers in (client.postgrest.headers, client.storage._headers):
            assert headers["Authorization"] == "Bearer user-jwt"
            assert headers["apikey"] == "test-key"
    finally:
        await close_client(client)


async def test_the_user_client_ignores_the_secret_key() -> None:
    client = await user_client(make_settings(SUPABASE_SECRET_KEY="test-secret-key"), "user-jwt")

    try:
        for headers in (client.postgrest.headers, client.storage._headers):
            assert "test-secret-key" not in headers.values()
    finally:
        await close_client(client)


async def test_the_service_client_needs_the_secret_key() -> None:
    with pytest.raises(RuntimeError, match="SUPABASE_SECRET_KEY"):
        await service_client(make_settings())


async def test_the_service_client_uses_the_secret_key() -> None:
    client = await service_client(make_settings(SUPABASE_SECRET_KEY="test-secret-key"))

    try:
        assert client.postgrest.headers["apikey"] == "test-secret-key"
        assert client.postgrest.headers["Authorization"] == "Bearer test-secret-key"
    finally:
        await close_client(client)


def test_the_providers_build_the_adapters_on_the_users_client_and_close_it(
    make_app: Callable[..., FastAPI], ec_private_key: ec.EllipticCurvePrivateKey
) -> None:
    app = make_app(STORAGE_BUCKET="custom-bucket", SUPABASE_SECRET_KEY="test-secret-key")
    seen: dict[str, Any] = {}

    @app.get("/providers")
    async def providers(
        sdk: Annotated[AsyncClient, Depends(deps.get_user_client)],
        images: Annotated[ImageRepo, Depends(deps.get_image_repo)],
        objects: Annotated[ObjectStore, Depends(deps.get_object_store)],
    ) -> dict[str, Any]:
        seen.update(sdk=sdk, images=images, objects=objects)
        return {"authorization": sdk.postgrest.headers["Authorization"]}

    token = es256_token(ec_private_key)
    response = TestClient(app).get("/providers", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"authorization": f"Bearer {token}"}
    assert isinstance(seen["images"], SupabaseImageRepo)
    assert isinstance(seen["objects"], SupabaseObjectStore)
    # Both adapters share the request's one client, and it's closed once the request is done.
    assert seen["images"]._client is seen["sdk"]
    assert seen["objects"]._bucket.id == "custom-bucket"
    assert seen["sdk"].options.httpx_client.is_closed


def test_the_providers_reject_a_request_without_a_token(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app()

    @app.get("/providers")
    async def providers(images: Annotated[ImageRepo, Depends(deps.get_image_repo)]) -> None:
        raise AssertionError("the route must not run without a user")

    assert TestClient(app).get("/providers").status_code == 401


# --- the secret key stays out of the API ---------------------------------------------------------


def test_the_api_and_service_layers_never_mention_the_secret_key() -> None:
    package = Path(deps.__file__).resolve().parents[1]
    sources = [*(package / "api").rglob("*.py"), *(package / "services").rglob("*.py")]

    assert sources
    offenders = [str(path) for path in sources if "SECRET_KEY" in path.read_text()]
    assert offenders == []
