FROM python:3.12-slim

# uv, pinned — the image build should not change because a new uv shipped.
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

# Dependencies resolve from uv.lock, in their own layer so they are cached until
# the lockfile itself changes. --no-install-project skips the app: it is copied
# below and would otherwise invalidate this layer on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY config/ config/
COPY docs/ docs/
COPY serve_dashboard.py .
COPY agentcore_agent.py .
COPY scripts/ scripts/

RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["python", "serve_dashboard.py"]
