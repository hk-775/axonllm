#!/usr/bin/env bash
#
# Send one Codex CLI prompt through the local AxonLLM Responses endpoint.

set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 \"prompt\"" >&2
    exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT="$*"
MODEL="${AXON_LOCAL_DEMO_MODEL:-claude-sonnet}"
BASE_URL="${AXON_LOCAL_DEMO_BASE_URL:-http://127.0.0.1:8001}"
RESPONSES_URL="${BASE_URL%/}/v1"
NO_PROXY_VALUE="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost"

echo "AxonLLM:    ${RESPONSES_URL}"
echo "Model:      ${MODEL}"
echo "Live trace: ${BASE_URL%/}/admin/dashboard (click Traces)"

cd "$ROOT_DIR"
env \
    AXONLLM_API_KEY="${AXONLLM_API_KEY:-local-demo}" \
    NO_PROXY="$NO_PROXY_VALUE" \
    no_proxy="$NO_PROXY_VALUE" \
    codex exec \
        --ignore-user-config \
        --ephemeral \
        --sandbox read-only \
        --cd "$ROOT_DIR" \
        --model "$MODEL" \
        -c 'model_provider="axonllm"' \
        -c 'model_providers.axonllm.name="AxonLLM"' \
        -c "model_providers.axonllm.base_url=\"${RESPONSES_URL}\"" \
        -c 'model_providers.axonllm.env_key="AXONLLM_API_KEY"' \
        -c 'model_providers.axonllm.wire_api="responses"' \
        -c 'model_providers.axonllm.request_max_retries=0' \
        -c 'model_providers.axonllm.stream_max_retries=0' \
        -c 'model_providers.axonllm.stream_idle_timeout_ms=120000' \
        -c 'model_providers.axonllm.requires_openai_auth=false' \
        -c 'model_supports_reasoning_summaries=false' \
        -c 'model_reasoning_summary="none"' \
        -c 'features.multi_agent=false' \
        -c 'approval_policy="never"' \
        -c 'web_search="disabled"' \
        "$PROMPT" </dev/null
