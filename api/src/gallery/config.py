"""Environment configuration. Every setting the API and worker read is declared here."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings, named exactly like their environment variables.

    Secret-bearing fields are excluded from ``repr`` so a stray ``logger.info(settings)``
    cannot leak them.
    """

    # The repo-root .env is the shared file (docker compose reads it too); api/.env overrides it.
    # Empty values (`KEY=` as copied from .env.example) count as unset, so defaults apply.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- Supabase ---
    SUPABASE_URL: str
    SUPABASE_PUBLISHABLE_KEY: str
    # Worker only. The API never holds this key.
    SUPABASE_SECRET_KEY: str | None = Field(default=None, repr=False)
    # Derived from SUPABASE_URL when unset (see _derive_jwks_url).
    SUPABASE_JWKS_URL: str | None = None
    # HS256 fallback for local `supabase start`. Leave unset in production (JWKS is used).
    SUPABASE_JWT_SECRET: str | None = Field(default=None, repr=False)

    # --- Storage and upload limits (API) ---
    STORAGE_BUCKET: str = "images"
    SIGNED_URL_TTL_SECONDS: int = 3600
    MAX_UPLOAD_MB: int = 10
    MAX_IMAGE_PIXELS: int = 40_000_000
    # How many uploads are decoded at once. Each holds tens of megabytes while it decodes.
    UPLOAD_PROCESSING_CONCURRENCY: int = Field(default=2, ge=1)

    # --- AI analysis (worker) ---
    ANALYZER: Literal["fake", "anthropic", "openai_compat"] = "fake"
    ANTHROPIC_API_KEY: str | None = Field(default=None, repr=False)
    AI_EFFORT: Literal["low", "medium", "high"] = "low"
    # Used only by the openai_compat adapter.
    AI_BASE_URL: str = "https://openrouter.ai/api/v1"
    AI_API_KEY: str | None = Field(default=None, repr=False)
    # Empty until #6 picks the production model, for example "claude-haiku-5-5".
    AI_MODEL: str = ""
    AI_TIMEOUT_SECONDS: float = 30.0

    # --- Tag verification (worker, optional) ---
    # none keeps the analyzer's tags as they are. jev checks them against the image with
    # TypeSafe Jev, whose image input is still experimental. It never fails a job.
    TAG_VERIFIER: Literal["none", "jev"] = "none"
    TYPESAFE_API_KEY: str | None = Field(default=None, repr=False)
    JEV_MODEL: str = "jev-1.14.0"
    # Tags the verifier scores below this are dropped, unless fewer than five tags would remain.
    TAG_MIN_PROBABILITY: float = Field(default=0.5, ge=0.0, le=1.0)
    # The budget for the whole verification call. When it runs out the tags stay as they were.
    TAG_VERIFIER_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0.0)

    # --- Embeddings (worker) ---
    # none stores no embeddings, so "find similar" scores on tag words alone. fastembed runs a small
    # local ONNX model inside the worker process and stores its vectors through pgvector.
    EMBEDDER: Literal["none", "fastembed"] = "none"
    # The vectors in the database come from one model. A different model needs a new calibration of
    # search_embedding_floor() (see docs/architecture.md section 8) and a backfill.
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    # Where fastembed looks for the model. The Docker image downloads it here at build time.
    EMBEDDING_CACHE_DIR: str = "/opt/models"

    # --- Worker loop ---
    WORKER_CONCURRENCY: int = 4
    WORKER_POLL_MIN_SECONDS: float = 1.0
    WORKER_POLL_MAX_SECONDS: float = 10.0
    JOB_MAX_ATTEMPTS: int = 3
    JOB_STALE_AFTER_SECONDS: int = 300

    # --- Logging and error reporting (API and worker) ---
    LOG_LEVEL: str = "INFO"
    # Unset disables Sentry entirely.
    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str = "development"
    # 0.0 keeps Sentry to errors only: no performance traces.
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    # Git SHA, set by the deploy script.
    RELEASE: str | None = None

    @model_validator(mode="after")
    def _derive_jwks_url(self) -> Self:
        if self.SUPABASE_JWKS_URL is None:
            base_url = self.SUPABASE_URL.rstrip("/")
            self.SUPABASE_JWKS_URL = f"{base_url}/auth/v1/.well-known/jwks.json"
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
