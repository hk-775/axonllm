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
  local output_name=${4:-${target}}
  local extra_context=${5:-}
  local target_out="${out_dir}/${output_name}"
  local context="{\"deployment_target\":\"${target}\",\"region\":\"us-east-1\"}"
  if [[ "${target}" == "managed-network" ]]; then
    context="$(
      printf '%s' \
        '{"deployment_target":"managed-network","region":"us-east-1",' \
        '"deployment_profile":"production",' \
        '"managed_network_egress_mode":"endpoints-only",' \
        '"managed_network_vpc_cidr":"10.42.0.0/16",' \
        '"managed_network_availability_zones":["us-east-1a","us-east-1c"],' \
        '"managed_network_availability_zone_ids":["use1-az4","use1-az1"]}'
    )"
  fi
  if [[ -n "${namespace}" ]]; then
    context="${context%?},\"deployment_namespace\":\"${namespace}\"}"
  fi
  if [[ -n "${extra_context}" ]]; then
    context="${context%?},${extra_context}}"
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

  # CDK emits redundant DependsOn entries for several L2 constructs. W3005 is
  # informational; all schema and IAM-action findings remain fatal.
  local lint_ignores=(W3005)
  if [[ "${target}" == "managed-network" ]]; then
    # AgentCore supports AZ IDs, but CDK VPC synthesis requires their
    # account-specific names. Preflight resolves and binds both values.
    lint_ignores+=(W3010)
  fi
  if [[ "${target}" == "serverless-control-plane" ]]; then
    # The DynamoDB action is accepted by IAM but absent from cfn-lint's
    # current action catalog. CloudFront standard logging also requires the
    # legacy S3 log-delivery ACL represented by W3045.
    lint_ignores+=(W3037 W3045)
  fi
  if [[ "${target}" == "agentcore-parked" ]] ||
    [[ "${target}" == "managed-network-parked" ]]
  then
    # The parked shell uses a condition that is deliberately always false so
    # the template remains schema-valid without creating a sentinel resource.
    lint_ignores+=(W8003)
  fi
  "${venv_dir}/bin/cfn-lint" -i "${lint_ignores[@]}" -t "${template}"
}

verify_target "fargate" "AxonLLMStack"
verify_target "application-state" "AxonLLMApplicationStateStack"
verify_target "managed-network" "AxonLLMManagedNetworkStack"
verify_target "managed-network-parked" "AxonLLMManagedNetworkStack"
verify_target "agentcore" "AxonLLMAgentCoreStack"
verify_target "agentcore-parked" "AxonLLMAgentCoreStack"
verify_target "identity" "AxonLLMIdentityStack"
verify_target "control-plane" "AxonLLMControlPlaneStack"
verify_target \
  "control-plane" \
  "AxonLLMControlPlaneStack" \
  "" \
  "control-plane-edge" \
  '"edge_cutover_enabled":true'
verify_target "serverless-control-plane" "AxonLLMServerlessControlPlaneStack"
verify_target \
  "serverless-control-plane" \
  "AxonLLMServerlessControlPlaneStack" \
  "" \
  "serverless-control-plane-edge" \
  '"edge_attachment_enabled":true'
verify_target "serverless-workers" "AxonLLMServerlessWorkersStack"
verify_target "release-foundation" "AxonLLMReleaseFoundationStack"
verify_target "launch-workers" "AxonLLMLaunchWorkersStack-managed" "managed"
