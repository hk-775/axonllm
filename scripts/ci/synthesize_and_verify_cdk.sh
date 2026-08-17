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

mkdir -m 0700 "${out_dir}"

verify_target() {
  local target=$1
  local stack_name=$2
  local namespace=${3:-}
  local target_out="${out_dir}/${target}"
  local context="{\"deployment_target\":\"${target}\",\"region\":\"us-east-1\"}"
  if [[ -n "${namespace}" ]]; then
    context="${context%?},\"deployment_namespace\":\"${namespace}\"}"
  fi

  (
    export CDK_CONTEXT_JSON="${context}"
    export CDK_OUTDIR="${target_out}"
    export JSII_RUNTIME_PACKAGE_CACHE_ROOT="${JSII_RUNTIME_PACKAGE_CACHE_ROOT:-${work_dir}/jsii-cache}"
    cd "${repo_root}/infra"
    "${venv_dir}/bin/python" app.py
  )

  local template="${target_out}/${stack_name}.template.json"
  local asset_manifest="${target_out}/${stack_name}.assets.json"
  test -f "${template}"
  test -f "${asset_manifest}"
  "${venv_dir}/bin/python" -c '
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
target = sys.argv[2]
manifest = json.loads(path.read_text(encoding="utf-8"))
docker_images = manifest.get("dockerImages")
if docker_images != {}:
    count = len(docker_images) if isinstance(docker_images, dict) else "invalid"
    raise SystemExit(f"expected zero CDK Docker assets, found {count}")
print(f"{target} CDK synthesis verified: zero Docker assets")
' "${asset_manifest}" "${target}"

  # CDK emits redundant DependsOn entries for several L2 constructs. The
  # production AgentCore facade also retains direct-JWT parameters for isolated
  # qualification namespaces, so those two parameters are intentionally unused
  # in the default facade synthesis. All other findings remain fatal.
  if [[ "${target}" == "agentcore" ]]; then
    "${venv_dir}/bin/cfn-lint" -i W2001 W3005 -t "${template}"
  else
    "${venv_dir}/bin/cfn-lint" -i W3005 -t "${template}"
  fi
}

verify_target "fargate" "AxonLLMStack"
verify_target "agentcore" "AxonLLMAgentCoreStack"
verify_target "identity" "AxonLLMIdentityStack"
verify_target "control-plane" "AxonLLMControlPlaneStack"
verify_target "release-foundation" "AxonLLMReleaseFoundationStack"
verify_target "launch-workers" "AxonLLMLaunchWorkersStack-managed" "managed"
