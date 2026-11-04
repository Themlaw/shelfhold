# syntax=docker/dockerfile:1.24@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# Shelfhold image: multi-stage, base pinned by digest (Dependabot bumps the digests), uid 1000.
#
# Frontend: the SPA is built only when the context has frontend/package.json. The CI passes
# --build-arg WITH_FRONTEND=true in that case (the default); false builds an API-only image.
ARG WITH_FRONTEND=true

FROM ghcr.io/astral-sh/uv:0.13@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS python-base

# --- Python dependencies and package, into a relocatable virtualenv ---
FROM python-base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY backend/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Development target (compose.dev.yaml): source mounted, hot reload ---
FROM builder AS dev
ENV PATH="/opt/venv/bin:$PATH" PYTHONPATH=/src/src SHELFHOLD_HOST=0.0.0.0
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "shelfhold.main:app_from_env", "--factory", "--reload", "--reload-dir", "/src/src", "--host", "0.0.0.0", "--port", "8080"]

# --- Frontend: either nothing (default) or the static build ---
FROM scratch AS frontend-false

FROM node:24-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend ./
COPY open-api /app/open-api
RUN npm run build

FROM scratch AS frontend-true
COPY --from=frontend-build /app/frontend/build /

# hadolint ignore=DL3006
FROM frontend-${WITH_FRONTEND} AS frontend

# --- Runtime ---
FROM python-base AS runtime
ARG VERSION=0.0.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Shelfhold" \
      org.opencontainers.image.description="Shelfhold, the household library workshop for Kavita" \
      org.opencontainers.image.source="https://github.com/Themlaw/shelfhold" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN groupadd --gid 1000 shelfhold \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin shelfhold \
 && mkdir /data /store \
 && chown 1000:1000 /data /store

COPY --from=builder /opt/venv /opt/venv
COPY --from=frontend / /app/frontend/
COPY docker/healthcheck.py /usr/local/bin/shelfhold-healthcheck

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    SHELFHOLD_HOST=0.0.0.0 \
    SHELFHOLD_DATA_DIR=/data \
    SHELFHOLD_STORE_DIR=/store \
    SHELFHOLD_FRONTEND_DIR=/app/frontend

USER 1000:1000
VOLUME ["/data", "/store"]
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/usr/local/bin/shelfhold-healthcheck"]
CMD ["shelfhold", "serve"]
