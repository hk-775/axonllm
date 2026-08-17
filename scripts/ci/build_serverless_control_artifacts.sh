#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
output_directory=${1:-}
source_revision=${2:-}
if [[ -z "${output_directory}" || -z "${source_revision}" ]]; then
  printf 'usage: %s OUTPUT_DIRECTORY SOURCE_REVISION\n' "$0" >&2
  exit 2
fi
if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'source revision must be a full lowercase Git commit SHA\n' >&2
  exit 2
fi
if [[ -e "${output_directory}" ]]; then
  printf 'refusing to reuse artifact output path: %s\n' \
    "${output_directory}" >&2
  exit 1
fi

output_parent=$(dirname "${output_directory}")
mkdir -p "${output_parent}"
work_directory=$(mktemp -d "${output_parent}/.serverless-artifacts.XXXXXX")
trap 'rm -rf "${work_directory}"' EXIT
staged_output="${work_directory}/output"

docker buildx build \
  --pull \
  --platform linux/arm64 \
  --file "${repo_root}/infra/serverless-control-artifacts/Dockerfile" \
  --build-context "project=${repo_root}" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --output "type=local,dest=${staged_output}" \
  "${repo_root}"

python3 "${repo_root}/scripts/release/verify_serverless_control_artifacts.py" \
  --directory "${staged_output}" \
  --source-revision "${source_revision}"

mv "${staged_output}" "${output_directory}"
printf 'serverless control artifacts created: %s\n' "${output_directory}"
