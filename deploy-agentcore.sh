#!/usr/bin/env bash
# Deploy a validated, authenticated AxonLLM AgentCore first-adopter setup.
#
# Usage:
#   ./deploy-agentcore.sh --config axonllm-agentcore.json [--bootstrap-cdk]
#   ./deploy-agentcore.sh --config axonllm-agentcore.json --validate-only
#
# The script deliberately has no unauthenticated production option. Local
# anonymous evaluation is available only through:
#   uv run axon setup local-demo --start --acknowledge-non-production

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/axonllm-uv-cache}"
if ! command -v uv >/dev/null 2>&1; then
    printf 'uv is required: https://docs.astral.sh/uv/\\n' >&2
    exit 2
fi

cd "${repo_root}"
exec uv run --frozen --no-sync python \
    -m src.gateway.deployment.agentcore_deploy "$@"
