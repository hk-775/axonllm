# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM docker.io/library/python:3.12-slim@sha256:d657ab0ade19f404a6ccc883ab399540de667aff751748ce23c07330c5a89e64

# uv, pinned — the image build should not change because a new uv shipped.
COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:0ca776d5bd774b0f8a9092100166ac46bf93386da17b8bf626f8e60b1f2d1c77 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    AXON_AUTH_MODE=ENFORCE \
    AXON_DEPLOYMENT_PROFILE=production \
    AXON_REQUIRE_CANONICAL_IDENTITY=true

# Dependencies resolve from uv.lock, in their own layer so they are cached until
# the lockfile itself changes. --no-install-project skips the app: it is copied
# below and would otherwise invalidate this layer on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra oidc --extra otel

COPY src/ src/
COPY config/ config/
COPY docs/ docs/
COPY serve_dashboard.py .
COPY agentcore_agent.py .
COPY scripts/ scripts/

# The landing page and its sibling pages. Without this the gateway's root path
# 404s in the container while working locally, and README's first instruction
# ("docker compose up", then open localhost:8000) lands on a stub. The handler
# degrades rather than raising, so nothing else breaks — which is exactly why
# the gap was invisible. site/infra is excluded in .dockerignore.
COPY site/ site/

RUN uv sync --frozen --no-dev --extra oidc --extra otel \
    && groupadd --gid 10001 axon \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin axon \
    && chown -R root:root /app \
    && chmod -R a-w /app

EXPOSE 8000

USER 10001:10001

CMD ["python", "serve_dashboard.py"]
