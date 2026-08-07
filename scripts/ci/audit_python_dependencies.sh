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
  --extra oidc \
  --extra otel \
  --extra saml \
  --format requirements-txt \
  >"${work_dir}/runtime-requirements.txt"

ignore_arguments=()
while IFS= read -r line; do
  [[ -z "${line}" || "${line}" == \#* ]] && continue
  if [[ ! "${line}" =~ ^(PYSEC|GHSA|CVE)-[A-Za-z0-9-]+$ ]]; then
    printf 'invalid pip-audit exception: %s\n' "${line}" >&2
    exit 1
  fi
  ignore_arguments+=(--ignore-vuln "${line}")
done <.github/pip-audit-ignore.txt

audit_requirements() {
  local label=$1
  local requirements_file=$2

  printf 'Auditing %s dependencies\n' "${label}"
  uv run --frozen --no-sync pip-audit \
    --disable-pip \
    --progress-spinner off \
    --require-hashes \
    "${ignore_arguments[@]}" \
    --requirement "${requirements_file}"
}

audit_requirements "gateway runtime" "${work_dir}/runtime-requirements.txt"
audit_requirements "AgentCore runtime" requirements.txt
audit_requirements "CDK tooling" infra/requirements.txt
