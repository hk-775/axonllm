#!/bin/bash
# Deploy AxonLLM to ECS Fargate via CDK
# Usage: ./deploy-fargate.sh [region] [--yes]
#
# Required deployment inputs:
#   AXON_VIEWER_DOMAIN_NAME
#   AXON_VIEWER_CERTIFICATE_ARN
#   AXON_ORIGIN_DOMAIN_NAME
#   AXON_ORIGIN_CERTIFICATE_ARN
#   AXON_APPROVED_HTTPS_PREFIX_LIST_ID
#   AXON_BEDROCK_INVOKE_RESOURCE_ARNS
#   AXON_VERIFIED_IMAGE_URI
#
# Optional:
#   AXON_DYNAMODB_TABLE_NAME (defaults to axonllm-state)
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
#   - A release-verified AMD64 image published to private ECR
#   - AWS CLI configured
#   - Node.js 20+ (for CDK; 22 or 24 preferred). 18 works but every cdk call
#     prints an end-of-life banner that looks like an error and hides the output.
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
# After deploy, set your API keys in the ProviderSecretArn stack output:
#   aws secretsmanager put-secret-value \
#     --secret-id "$PROVIDER_SECRET_ARN" \
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

if [ "$REGION" != "us-east-1" ]; then
    echo "The reference stack must be deployed in us-east-1 (CloudFront WAF scope)." >&2
    exit 2
fi

require_env() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        echo "Required environment variable is not set: ${name}" >&2
        exit 2
    fi
}

require_env AXON_VIEWER_DOMAIN_NAME
require_env AXON_VIEWER_CERTIFICATE_ARN
require_env AXON_ORIGIN_DOMAIN_NAME
require_env AXON_ORIGIN_CERTIFICATE_ARN
require_env AXON_APPROVED_HTTPS_PREFIX_LIST_ID
require_env AXON_BEDROCK_INVOKE_RESOURCE_ARNS
require_env AXON_VERIFIED_IMAGE_URI

if [[ ! "$AXON_APPROVED_HTTPS_PREFIX_LIST_ID" =~ ^pl-[0-9a-fA-F]+$ ]]; then
    echo "AXON_APPROVED_HTTPS_PREFIX_LIST_ID must be an EC2 prefix list id." >&2
    exit 2
fi
if [[ ! "$AXON_VERIFIED_IMAGE_URI" =~ ^[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$ ]]; then
    echo "AXON_VERIFIED_IMAGE_URI must be an immutable private ECR URI in us-east-1." >&2
    exit 2
fi
if [[ "$AXON_VIEWER_CERTIFICATE_ARN" != arn:aws:acm:us-east-1:* ]] ||
   [[ "$AXON_ORIGIN_CERTIFICATE_ARN" != arn:aws:acm:us-east-1:* ]]; then
    echo "Both ACM certificate ARNs must be in us-east-1." >&2
    exit 2
fi

TABLE_NAME="${AXON_DYNAMODB_TABLE_NAME:-axonllm-state}"

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
    --context table_name="$TABLE_NAME" \
    --parameters "AxonLLMStack:ViewerDomainName=${AXON_VIEWER_DOMAIN_NAME}" \
    --parameters "AxonLLMStack:ViewerCertificateArn=${AXON_VIEWER_CERTIFICATE_ARN}" \
    --parameters "AxonLLMStack:OriginDomainName=${AXON_ORIGIN_DOMAIN_NAME}" \
    --parameters "AxonLLMStack:OriginCertificateArn=${AXON_ORIGIN_CERTIFICATE_ARN}" \
    --parameters "AxonLLMStack:ApprovedHttpsPrefixListId=${AXON_APPROVED_HTTPS_PREFIX_LIST_ID}" \
    --parameters "AxonLLMStack:BedrockInvokeResourceArns=${AXON_BEDROCK_INVOKE_RESOURCE_ARNS}" \
    --parameters "AxonLLMStack:VerifiedImageUri=${AXON_VERIFIED_IMAGE_URI}" \
    --require-approval "$APPROVAL" \
    --outputs-file outputs.json

echo ""
echo "==> Deployment complete!"
echo ""

if [ -f outputs.json ]; then
    PUBLIC_URL=$(python3 -c "import json; d=json.load(open('outputs.json')); print(list(d.values())[0].get('CloudFrontURL', 'check AWS console'))" 2>/dev/null || echo "check AWS console")
    echo "Dashboard: ${PUBLIC_URL}/admin/dashboard"
    echo "API:       ${PUBLIC_URL}/api/chat"
    echo ""
fi

echo "Next steps:"
echo "  1. Set API keys in the ProviderSecretArn stack output."
echo "  2. Point ${AXON_VIEWER_DOMAIN_NAME} at the CloudFront distribution."
echo "  3. Configure OIDC and canonical principals before allowing real traffic."
