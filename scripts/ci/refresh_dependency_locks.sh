#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "${repo_root}"

uv lock
uv export \
  --frozen \
  --no-dev \
  --no-emit-project \
  --extra agentcore \
  --extra oidc \
  --format requirements-txt \
  >requirements.txt
uv pip compile \
  --generate-hashes \
  --no-header \
  --output-file infra/requirements.txt \
  --python-version 3.12 \
  --upgrade \
  --universal \
  infra/requirements.in \
  >/dev/null
