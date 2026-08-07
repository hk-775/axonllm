#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "${repo_root}"

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

uv export \
  --frozen \
  --no-dev \
  --no-emit-project \
  --extra agentcore \
  --extra oidc \
  --format requirements-txt \
  >"${work_dir}/requirements.txt"

cp infra/requirements.txt "${work_dir}/infra-requirements.txt"
uv pip compile \
  --generate-hashes \
  --no-header \
  --output-file "${work_dir}/infra-requirements.txt" \
  --python-version 3.12 \
  --universal \
  infra/requirements.in \
  >/dev/null

cmp --silent requirements.txt "${work_dir}/requirements.txt" || {
  printf 'requirements.txt is stale; regenerate it with scripts/ci/refresh_dependency_locks.sh\n' >&2
  exit 1
}

cmp --silent infra/requirements.txt "${work_dir}/infra-requirements.txt" || {
  printf 'infra/requirements.txt is stale; regenerate it with scripts/ci/refresh_dependency_locks.sh\n' >&2
  exit 1
}
