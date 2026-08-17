# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM docker.io/library/python:3.12-slim@sha256:0568e6111802e74c03e8dda76565cdf4b88881d77de0d9b769846e9dfcb8d80a AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:5fe7b2b0499a485ee86e1e0d2154e1556a08dffeb5f72b204895af5212a2069c /uv /uvx /bin/

WORKDIR /project

ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

COPY --from=project pyproject.toml uv.lock ./
RUN uv export \
        --frozen \
        --no-dev \
        --no-emit-project \
        --extra serverless-control \
        --format requirements-txt \
        > /tmp/serverless-control-requirements.txt \
    && uv pip install \
        --target /dependencies \
        --require-hashes \
        --requirement /tmp/serverless-control-requirements.txt

COPY --from=project axonllm/ axonllm/
COPY --from=project src/ src/
COPY --from=project config/ config/
COPY --from=project docs/ docs/
COPY --from=project site/ site/
COPY --from=project scripts/release/build_serverless_control_artifacts.py scripts/release/build_serverless_control_artifacts.py

ARG SOURCE_REVISION
RUN python scripts/release/build_serverless_control_artifacts.py \
      --repository /project \
      --output-directory /artifacts \
      --dependency-root /dependencies \
      --source-revision "${SOURCE_REVISION}"

FROM scratch AS artifacts
COPY --from=builder /artifacts/ /
