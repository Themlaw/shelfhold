# syntax=docker/dockerfile:1.24@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# Shelfhold image: multi-stage, base pinned by digest (Dependabot bumps the digests), uid 1000.
# /data and /store are 0700: the companion (same image) finds them unreadable, as it must.
# A second user, uid 1001, exists for the plugin runner (the companion container of isolated
# plugins, command `shelfhold plugin-runner`): the core never runs as that uid, nor it as the core's.
#
# Frontend: the SPA is built only when the context has frontend/package.json. The CI passes
# --build-arg WITH_FRONTEND=true in that case (the default); false builds an API-only image.
ARG WITH_FRONTEND=true

FROM ghcr.io/astral-sh/uv:0.13@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS python-base

# --- Python dependencies and package, into a relocatable virtualenv ---
FROM python-base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# The uv workspace root is the build context root: `backend` (the host) and `sdk`
# (shelfhold-sdk, MIT) are members; the lockfile lives at the root. The SDK wheel is built
# from the same sources and installed with the host (`--no-editable` below).
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/
COPY sdk/pyproject.toml sdk/
COPY sdk/src sdk/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project backend --locked --no-dev --no-install-project
COPY backend/src backend/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project backend --locked --no-dev --no-editable

# --- Development target (compose.dev.yaml): source mounted, hot reload ---
FROM builder AS dev
ENV PATH="/opt/venv/bin:$PATH" PYTHONPATH=/src/backend/src SHELFHOLD_HOST=0.0.0.0
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "shelfhold.main:app_from_env", "--factory", "--reload", "--reload-dir", "/src/backend/src", "--host", "0.0.0.0", "--port", "8080"]

# --- Frontend: either nothing (default) or the static build ---
FROM scratch AS frontend-false

FROM node:24-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS frontend-build
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
ARG RELEASE=false
LABEL org.opencontainers.image.title="Shelfhold" \
      org.opencontainers.image.description="Shelfhold, the household library workshop for Kavita" \
      org.opencontainers.image.source="https://github.com/Themlaw/shelfhold" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN groupadd --gid 1000 shelfhold \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin shelfhold \
 && groupadd --gid 1001 plugin-runner \
 && useradd --uid 1001 --gid 1001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin plugin-runner \
 && mkdir /data /store /data/plugins /plugins /plugin-rpc \
 && chown 1000:1000 /data /store /data/plugins \
 && chmod 0700 /data /store \
 && chown 1001:1001 /plugin-rpc

COPY --from=builder /opt/venv /opt/venv
# the installer of `shelfhold plugin install` (it unpacks a hashed wheel, nothing else): without
# it the image cannot install a plugin at all
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=frontend / /app/frontend/
COPY docker/healthcheck.py /usr/local/bin/shelfhold-healthcheck

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SHELFHOLD_BUILD_REVISION="${REVISION}" \
    SHELFHOLD_BUILD_RELEASE="${RELEASE}" \
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
CMD ["shelfhold", "serve", "--migrate"]
