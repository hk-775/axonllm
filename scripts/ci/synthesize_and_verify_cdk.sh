#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
out_dir=${1:-}
if [[ -z "${out_dir}" ]]; then
  printf 'usage: %s OUT_DIR\n' "$0" >&2
  exit 2
fi
if [[ -e "${out_dir}" ]]; then
  printf 'refusing to reuse CDK output path: %s\n' "${out_dir}" >&2
  exit 1
fi

node_major=$(node --version | sed -E 's/^v([0-9]+).*/\1/')
if [[ ! "${node_major}" =~ ^[0-9]+$ ]] || ((node_major < 22)); then
  printf 'CDK synthesis requires Node 22 or newer; found %s\n' "$(node --version)" >&2
  exit 1
fi

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT
venv_dir="${work_dir}/infra-venv"

uv venv --python 3.12 "${venv_dir}"
uv pip sync \
  --python "${venv_dir}/bin/python" \
  --require-hashes \
  "${repo_root}/infra/requirements.txt"

export CDK_OUTDIR="${out_dir}"
export JSII_RUNTIME_PACKAGE_CACHE_ROOT="${JSII_RUNTIME_PACKAGE_CACHE_ROOT:-${work_dir}/jsii-cache}"
(
  cd "${repo_root}/infra"
  "${venv_dir}/bin/python" app.py
)

"${repo_root}/scripts/ci/verify_cdk_asset.py" "${out_dir}"
test -f "${out_dir}/AxonLLMStack.template.json"
