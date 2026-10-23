"""FastAPI dependencies: settings, the signed-in user, and the adapters built on their token.

Every database and storage call carries the user's JWT, so RLS is the authorization layer. This
module never touches the secret key.
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gallery import config
from gallery.adapters.embeddings import DATABASE_DIMENSIONS, build_embedder
from gallery.adapters.supabase_client import (
    AsyncClient,
    anon_client,
    close_client,
    database_is_reachable,
    user_client,
)
from gallery.adapters.supabase_images import SupabaseImageRepo
from gallery.adapters.supabase_shares import SupabaseShareRepo
from gallery.adapters.supabase_site_settings import SupabaseSiteSettingsRepo
from gallery.adapters.supabase_storage import SupabaseObjectStore
from gallery.config import Settings
from gallery.domain.errors import AuthError
from gallery.ports import ImageRepo, ObjectStore, ShareRepo, SiteSettingsRepo, TextEmbedder

logger = logging.getLogger(__name__)

# Supabase signs the tokens of signed-in users with this audience.
_AUDIENCE = "authenticated"
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
# Absorbs the clock drift between Supabase and this host, so a fresh token isn't "not yet valid".
_CLOCK_SKEW_SECONDS = 10
# A hung key server must not hold a request open for the library's 30 s default.
_JWKS_TIMEOUT_SECONDS = 5

_bearer = HTTPBearer(auto_error=False)

# Answers whether the database responds. It needs no user, only the publishable key.
DatabaseCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The signed-in user, plus the token to call Supabase with on their behalf."""

    id: UUID
    email: str | None
    access_token: str


def get_settings() -> Settings:
    """Return the process-wide settings. Tests replace it through `dependency_overrides`."""
    return config.get_settings()


@lru_cache
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    # One client per URL for the life of the process: it caches the key set, which keeps a
    # network round trip off the hot path.
    return jwt.PyJWKClient(jwks_url, timeout=_JWKS_TIMEOUT_SECONDS)


def _decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    """Verify the signature, expiry and audience of `token` and return its claims.

    The token's own `alg` header only picks which key to verify with. Each branch then accepts
    just the algorithms that key can check, so a token can't talk its way into a weaker one.
    Blocking: a JWKS cache miss fetches the key set over the network.
    """
    algorithm = jwt.get_unverified_header(token).get("alg")
    key: Any
    if algorithm == "HS256" and settings.SUPABASE_JWT_SECRET:
        # Local `supabase start` stacks that still sign with the shared secret.
        key, algorithms = settings.SUPABASE_JWT_SECRET, ["HS256"]
    elif algorithm in _ASYMMETRIC_ALGORITHMS:
        jwks_url = settings.SUPABASE_JWKS_URL
        assert jwks_url is not None  # Settings derives it from SUPABASE_URL when unset
        key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        algorithms = list(_ASYMMETRIC_ALGORITHMS)
    else:
        # Includes "none", and HS256 when no secret is configured (production).
        raise jwt.InvalidAlgorithmError(f"Unsupported token algorithm: {algorithm}")
    return jwt.decode(
        token,
        key,
        algorithms=algorithms,
        audience=_AUDIENCE,
        leeway=_CLOCK_SKEW_SECONDS,
        options={"require": ["exp", "sub", "aud"]},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUser:
    """Verify the Bearer token and return its user. Raises `AuthError` (401) when it's not valid."""
    if credentials is None:
        raise AuthError("Missing bearer token.")
    token = credentials.credentials
    try:
        claims = await run_in_threadpool(_decode_access_token, token, settings)
        user_id = UUID(claims["sub"])
    except jwt.PyJWKClientConnectionError as error:
        # Our dependency is down, not the user's token. Retrying later may work.
        logger.warning("could not fetch the token signing keys", exc_info=error)
        raise HTTPException(
            status_code=503, detail="Sign-in verification is temporarily unavailable."
        ) from error
    except (jwt.PyJWTError, ValueError) as error:
        logger.info("rejected an access token", extra={"reason": type(error).__name__})
        raise AuthError("Invalid or expired access token.") from error
    email = claims.get("email")
    return CurrentUser(
        id=user_id, email=email if isinstance(email, str) else None, access_token=token
    )


async def get_user_client(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[AsyncClient]:
    """Yield a Supabase client that acts as the user, and close it when the request ends."""
    client = await user_client(settings, user.access_token)
    try:
        yield client
    finally:
        await close_client(client)


async def get_anon_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[AsyncClient]:
    """Yield a Supabase client that acts as nobody (`anon`), for the routes that need no sign-in."""
    client = await anon_client(settings)
    try:
        yield client
    finally:
        await close_client(client)


async def get_image_repo(client: Annotated[AsyncClient, Depends(get_user_client)]) -> ImageRepo:
    return SupabaseImageRepo(client)


async def get_share_repo(client: Annotated[AsyncClient, Depends(get_user_client)]) -> ShareRepo:
    return SupabaseShareRepo(client)


async def get_site_settings_repo(
    client: Annotated[AsyncClient, Depends(get_user_client)],
) -> SiteSettingsRepo:
    return SupabaseSiteSettingsRepo(client)


async def get_public_site_settings_repo(
    client: Annotated[AsyncClient, Depends(get_anon_client)],
) -> SiteSettingsRepo:
    """The settings repo as `anon`. Only `signups_allowed` works through it."""
    return SupabaseSiteSettingsRepo(client)


async def get_object_store(
    client: Annotated[AsyncClient, Depends(get_user_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ObjectStore:
    return SupabaseObjectStore(client, settings.STORAGE_BUCKET)


class LazyEmbedder:
    """A TextEmbedder that builds the real one when it is first asked to embed.

    Most API requests never touch the model, so the process imports fastembed (and with it
    onnxruntime) and loads the model on the first text search instead of at startup. Both happen
    in a thread, so the event loop keeps serving. A failed build isn't remembered: the next
    search tries again, and the service falls back to words only meanwhile.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._embedder: TextEmbedder | None = None
        self._build_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._settings.EMBEDDING_MODEL

    @property
    def dimensions(self) -> int:
        # build_embedder refuses a model with any other size.
        return DATABASE_DIMENSIONS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        embedder = self._embedder
        if embedder is None:
            embedder = await asyncio.to_thread(self._build)
        return await embedder.embed(texts)

    def _build(self) -> TextEmbedder:
        with self._build_lock:
            if self._embedder is None:
                built = build_embedder(self._settings)
                if built is None:
                    raise ValueError("EMBEDDER=none builds no embedder")
                self._embedder = built
            return self._embedder


# One per model setting, for the life of the process, so the model is loaded once.
_query_embedders: dict[tuple[str, str, str], LazyEmbedder] = {}


async def get_query_embedder(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TextEmbedder | None:
    """Return the embedder for text search, or None when `EMBEDDER` is "none".

    Nothing is loaded here: the model comes in on the first search that embeds a query.
    """
    if settings.EMBEDDER == "none":
        return None
    key = (settings.EMBEDDER, settings.EMBEDDING_MODEL, settings.EMBEDDING_CACHE_DIR)
    if key not in _query_embedders:
        _query_embedders[key] = LazyEmbedder(settings)
    return _query_embedders[key]


def get_database_check(settings: Annotated[Settings, Depends(get_settings)]) -> DatabaseCheck:
    return partial(database_is_reachable, settings)
