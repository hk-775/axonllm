#!/bin/bash
# Deploy AxonLLM to ECS Fargate via CDK
# Usage: ./deploy-fargate.sh [region] [--yes]
#
# --yes skips CDK's approval prompt for security-sensitive changes (IAM roles,
# security group rules). Without it the script cannot run unattended at all: CDK
# needs a terminal to ask, so in CI it fails with "Stack includes
# security-sensitive updates, but terminal (TTY) is not attached". Setting CI=true
# has the same effect, which is what most CI providers export already.
#
# Only pass --yes when you have reviewed the diff (`cd infra && npx cdk diff`)
# and accept the IAM and network grants it lists. The prompt is a consent gate,
# not an authentication one — your AWS credentials being valid is a separate
# question from whether you meant to create those specific roles.
#
# Prerequisites:
#   - Docker running
#   - AWS CLI configured
#   - Node.js installed (for CDK)
#   - uv installed (https://docs.astral.sh/uv/)
#
# Before first deploy:
#   1. cd infra && npx cdk bootstrap aws://ACCOUNT_ID/REGION
#
# That is the only prerequisite step. This script creates infra/.venv and installs
# requirements.txt itself (see below), so there is nothing to pip-install by hand.
# Use `npx cdk`, not `cdk` — the CLI is an npm package and nothing installs it
# globally.
#
# After deploy, set your API keys in Secrets Manager:
#   aws secretsmanager put-secret-value \
#     --secret-id axonllm/api-keys \
#     --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-...","OPENAI_API_KEY":"sk-..."}'

set -euo pipefail

REGION=""
# CI=true means no human is watching, so prompting would only hang or crash.
ASSUME_YES="${CI:-false}"

for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=true ;;
        -*) echo "Unknown option: $arg" >&2; exit 2 ;;
        *)
            if [ -n "$REGION" ]; then
                echo "Unexpected argument: $arg (region already set to $REGION)" >&2
                exit 2
            fi
            REGION="$arg"
            ;;
    esac
done
REGION="${REGION:-us-east-1}"

# "broadening" still prompts, and only when a change widens IAM or network
# access; "never" prompts for nothing. Anything else CDK would reject.
if [ "$ASSUME_YES" = true ]; then
    APPROVAL=never
else
    APPROVAL=broadening
fi

echo "==> Deploying AxonLLM to ECS Fargate in ${REGION}..."
if [ "$APPROVAL" = never ]; then
    echo "    Approval prompts disabled (--yes or CI=true)."
fi
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
    --require-approval "$APPROVAL" \
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
