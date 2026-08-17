#!/usr/bin/env bash
# Deploy a validated, authenticated AxonLLM AgentCore first-adopter setup.
#
# Usage:
#   ./deploy-agentcore.sh --config axonllm-agentcore.json --install
#   ./deploy-agentcore.sh --config axonllm-agentcore.json
# Operator-only recovery:
#   ./deploy-agentcore.sh --config axonllm-agentcore.json --bootstrap-cdk
#   ./deploy-agentcore.sh --config axonllm-agentcore.json --reconcile-bootstrap-policies
#   ./deploy-agentcore.sh --config axonllm-agentcore.json --validate-only
#
# The script deliberately has no unauthenticated production option. Local
# anonymous evaluation is available only through:
#   uv run axon setup local-demo --start --acknowledge-non-production

set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/axonllm-uv-cache}"
if ! command -v uv >/dev/null 2>&1; then
    printf 'uv is required: https://docs.astral.sh/uv/\n' >&2
    exit 2
fi

if command -v sha256sum >/dev/null 2>&1; then
    lock_digest=$(sha256sum "${repo_root}/uv.lock")
elif command -v shasum >/dev/null 2>&1; then
    lock_digest=$(shasum -a 256 "${repo_root}/uv.lock")
else
    printf 'sha256sum or shasum is required to isolate deployment dependencies\n' >&2
    exit 2
fi
lock_digest=${lock_digest%% *}

if [[ -n "${AXON_AGENTCORE_UV_ENVIRONMENT:-}" ]]; then
    deployment_environment="${AXON_AGENTCORE_UV_ENVIRONMENT}"
else
    deployment_environment="${UV_CACHE_DIR}/agentcore-environments/${lock_digest}"
    mkdir -p "${UV_CACHE_DIR}/agentcore-environments"
fi
export UV_PROJECT_ENVIRONMENT="${deployment_environment}"
unset UV_NO_SYNC

cd "${repo_root}"
exec uv run --frozen --extra oidc --extra agentcore python \
    -m src.gateway.deployment.agentcore_deploy "$@"
