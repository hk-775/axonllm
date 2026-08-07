FROM python:3.12-slim

# uv, pinned — the image build should not change because a new uv shipped.
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    AXON_AUTH_MODE=ENFORCE

# Dependencies resolve from uv.lock, in their own layer so they are cached until
# the lockfile itself changes. --no-install-project skips the app: it is copied
# below and would otherwise invalidate this layer on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra oidc --extra saml --extra otel

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

RUN uv sync --frozen --no-dev --extra oidc --extra saml --extra otel \
    && groupadd --gid 10001 axon \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin axon \
    && chown -R root:root /app \
    && chmod -R a-w /app

EXPOSE 8000

USER 10001:10001

CMD ["python", "serve_dashboard.py"]
