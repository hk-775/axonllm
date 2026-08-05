#!/bin/bash
# Deploy AxonLLM to ECS Fargate via CDK
# Usage: ./deploy-fargate.sh [region]
#
# Prerequisites:
#   - Docker running
#   - AWS CLI configured
#   - Node.js installed (for CDK)
#   - uv installed (https://docs.astral.sh/uv/)
#
# Before first deploy:
#   1. cd infra && uv pip install -r requirements.txt
#   2. cdk bootstrap aws://ACCOUNT_ID/REGION
#
# After deploy, set your API keys in Secrets Manager:
#   aws secretsmanager put-secret-value \
#     --secret-id axonllm/api-keys \
#     --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-...","OPENAI_API_KEY":"sk-..."}'

set -euo pipefail

REGION="${1:-us-east-1}"

echo "==> Deploying AxonLLM to ECS Fargate in ${REGION}..."
echo ""

cd "$(dirname "$0")/infra"

# Install CDK dependencies if needed
if [ ! -d ".venv" ]; then
    echo "==> Setting up CDK virtual environment..."
    uv venv .venv
    uv pip install -q --python .venv -r requirements.txt
fi
source .venv/bin/activate

# Synthesize and deploy
echo "==> Running cdk deploy..."
npx cdk deploy \
    --context region="$REGION" \
    --require-approval broadening \
    --outputs-file outputs.json

echo ""
echo "==> Deployment complete!"
echo ""

if [ -f outputs.json ]; then
    ALB_URL=$(python3 -c "import json; d=json.load(open('outputs.json')); print(list(d.values())[0].get('ServiceServiceURL', 'check AWS console'))" 2>/dev/null || echo "check AWS console")
    echo "Dashboard: ${ALB_URL}/admin/dashboard"
    echo "API:       ${ALB_URL}/api/chat"
    echo ""
fi

echo "Next steps:"
echo "  1. Set API keys: aws secretsmanager put-secret-value --secret-id axonllm/api-keys --secret-string '{\"ANTHROPIC_API_KEY\":\"sk-ant-...\",\"OPENAI_API_KEY\":\"sk-...\"}'"
echo "  2. (Optional) Add HTTPS: attach an ACM certificate to the ALB in the AWS Console"
echo "  3. (Optional) Add OIDC: set AXON_OIDC_ISSUER and AXON_OIDC_AUDIENCE in the task environment"
