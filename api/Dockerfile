# syntax=docker/dockerfile:1

# One image serves both the API and the worker. Compose and the deploy pick the command.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=0 makes uv use the image's Python 3.12 instead of fetching one.
# The venv lives in /opt/venv so the runtime stage copies a single directory.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies go first, so this layer is reused until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# The project installs non-editable, so the runtime stage needs no source tree.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12-slim

# A fixed uid keeps file ownership predictable for any volume or host mount added later.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER app
EXPOSE 8000

# urllib instead of curl keeps the image small. The worker has no HTTP port, so compose
# turns this check off for it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "gallery.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
