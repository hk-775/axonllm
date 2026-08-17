# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM docker.io/library/python:3.12-slim@sha256:0568e6111802e74c03e8dda76565cdf4b88881d77de0d9b769846e9dfcb8d80a

COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:5fe7b2b0499a485ee86e1e0d2154e1556a08dffeb5f72b204895af5212a2069c /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/tmp \
    AXON_AUTH_MODE=ENFORCE \
    AXON_DEPLOYMENT_PROFILE=production \
    AXON_REQUIRE_CANONICAL_IDENTITY=true \
    AXON_LOAD_DEMO_DATA=false \
    AXON_NO_BROWSER=true \
    AXON_SERVER_HOST=0.0.0.0 \
    AXON_SERVER_PORT=8000

COPY --from=project pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    --extra server --extra oidc --extra otel

COPY --from=project src/ src/
COPY --from=project config/ config/
COPY --from=project docs/ docs/
COPY --from=project serve_dashboard.py .
COPY --from=project agentcore_agent.py .
COPY --from=project scripts/ scripts/
COPY --from=project site/ site/

RUN uv sync --frozen --no-dev --extra server --extra oidc --extra otel \
    && groupadd --gid 10001 axon \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin axon \
    && chown -R root:root /app \
    && chmod -R a-w /app

EXPOSE 8000

USER 10001:10001

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["python", "-m", "src.gateway.standalone"]
