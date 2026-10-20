# syntax=docker/dockerfile:1

# One image serves both the API and the worker. Compose and the deploy pick the command.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=0 makes uv use the image's Python 3.12 instead of fetching one.
# The venv lives in /opt/venv so the runtime stage copies a single directory.
# ORT_DISABLE_TELEMETRY keeps onnxruntime (which loads the embedding model below) from sending usage
# telemetry.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    ORT_DISABLE_TELEMETRY=1

WORKDIR /app

# Dependencies go first, so this layer is reused until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# The worker's embedding model is downloaded here, at build time, so the runtime never downloads it.
# It sits after the dependencies and before the source, so a code change doesn't fetch it again, and
# a new EMBEDDING_MODEL (compose passes it as a build arg) does. The API ships it too, because the
# two services share this image, but never loads it.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
# fastembed writes a metadata file readable by root only, which the app user would then skip with a
# warning, so make the whole folder world-readable.
RUN /opt/venv/bin/python -c "import sys; from fastembed import TextEmbedding; TextEmbedding(model_name=sys.argv[1], cache_dir='/opt/models')" "$EMBEDDING_MODEL" \
 && chmod -R a+rX /opt/models

# The project installs non-editable, so the runtime stage needs no source tree.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12-slim

# A fixed uid keeps file ownership predictable for any volume or host mount added later.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# HF_HUB_OFFLINE makes "the runtime never downloads the model" a rule: a model missing from
# /opt/models (a changed EMBEDDING_MODEL without a rebuild) fails at once instead of fetching it
# into a folder the app user can't write to.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    ORT_DISABLE_TELEMETRY=1

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

WORKDIR /app
USER app
EXPOSE 8000

# urllib instead of curl keeps the image small. The worker has no HTTP port, so compose
# turns this check off for it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "gallery.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
