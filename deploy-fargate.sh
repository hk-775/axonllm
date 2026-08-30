#!/bin/bash
# Deploy AxonLLM to ECS Fargate via CDK
# Usage: ./deploy-fargate.sh [region] [--yes]
#
# Required deployment inputs:
#   AXON_APPROVED_HTTPS_PREFIX_LIST_ID
#   AXON_BEDROCK_INVOKE_RESOURCE_ARNS
#   AXON_VERIFIED_IMAGE_URI
#
# Custom-domain edge mode (the default) also requires:
#   AXON_VIEWER_DOMAIN_NAME
#   AXON_VIEWER_CERTIFICATE_ARN
#   AXON_ORIGIN_DOMAIN_NAME
#   AXON_ORIGIN_CERTIFICATE_ARN
#
# Optional:
#   AXON_DYNAMODB_TABLE_NAME (defaults to axonllm-state)
#   AXON_RUNTIME_STATE_TABLE_NAME (blank selects the primary table)
#   AXON_RECOVERY_CUTOVER_MODE (true only during a quiesced table switch)
#   AXON_DEPLOYMENT_MODE (staging or production; defaults to staging)
#   AXON_DEPLOYMENT_NAMESPACE (dedicated stack suffix, for example demo)
#   AXON_FARGATE_EDGE_MODE (custom-domain or cloudfront-default)
#   AXON_LOAD_DEMO_DATA (false or true; production requires false)
#   AXON_SCIM_TENANTS_SECRET_ARN (complete us-east-1 Secrets Manager ARN)
#   AXON_PUBLIC_HOSTED_ZONE_ID and AXON_PUBLIC_HOSTED_ZONE_NAME
#
# Production mode also requires:
#   AXON_OIDC_ISSUER
#   AXON_OIDC_AUTHORIZATION_ENDPOINT
#   AXON_OIDC_TOKEN_ENDPOINT
#   AXON_OIDC_USER_INFO_ENDPOINT
#   AXON_OIDC_CLIENT_ID
#   AXON_OIDC_CLIENT_SECRET
#   AXON_OIDC_AUDIENCE
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

require_env AXON_APPROVED_HTTPS_PREFIX_LIST_ID
require_env AXON_BEDROCK_INVOKE_RESOURCE_ARNS
require_env AXON_VERIFIED_IMAGE_URI

EDGE_MODE="${AXON_FARGATE_EDGE_MODE:-custom-domain}"
if [ "$EDGE_MODE" != "custom-domain" ] &&
   [ "$EDGE_MODE" != "cloudfront-default" ]; then
    echo "AXON_FARGATE_EDGE_MODE must be custom-domain or cloudfront-default." >&2
    exit 2
fi

DEPLOYMENT_NAMESPACE="${AXON_DEPLOYMENT_NAMESPACE:-}"
if [ -n "$DEPLOYMENT_NAMESPACE" ] &&
   [[ ! "$DEPLOYMENT_NAMESPACE" =~ ^[a-z]([a-z0-9-]{0,14}[a-z0-9])?$ ]]; then
    echo "AXON_DEPLOYMENT_NAMESPACE must be 1-16 lowercase letters, digits, or internal hyphens." >&2
    exit 2
fi
STACK_ID="AxonLLMStack"
if [ -n "$DEPLOYMENT_NAMESPACE" ]; then
    STACK_ID="${STACK_ID}-${DEPLOYMENT_NAMESPACE}"
fi

DEPLOYMENT_MODE="${AXON_DEPLOYMENT_MODE:-staging}"
if [ "$DEPLOYMENT_MODE" != "staging" ] &&
   [ "$DEPLOYMENT_MODE" != "production" ]; then
    echo "AXON_DEPLOYMENT_MODE must be staging or production." >&2
    exit 2
fi
if [ "$EDGE_MODE" = "cloudfront-default" ] &&
   [ "$DEPLOYMENT_MODE" != "staging" ]; then
    echo "cloudfront-default edge mode is restricted to staging." >&2
    exit 2
fi
if [ "$EDGE_MODE" = "custom-domain" ]; then
    require_env AXON_VIEWER_DOMAIN_NAME
    require_env AXON_VIEWER_CERTIFICATE_ARN
    require_env AXON_ORIGIN_DOMAIN_NAME
    require_env AXON_ORIGIN_CERTIFICATE_ARN
fi
if [ "$DEPLOYMENT_MODE" = "production" ]; then
    require_env AXON_OIDC_ISSUER
    require_env AXON_OIDC_AUTHORIZATION_ENDPOINT
    require_env AXON_OIDC_TOKEN_ENDPOINT
    require_env AXON_OIDC_USER_INFO_ENDPOINT
    require_env AXON_OIDC_CLIENT_ID
    require_env AXON_OIDC_CLIENT_SECRET
    require_env AXON_OIDC_AUDIENCE
fi

if [[ ! "$AXON_APPROVED_HTTPS_PREFIX_LIST_ID" =~ ^pl-[0-9a-fA-F]+$ ]]; then
    echo "AXON_APPROVED_HTTPS_PREFIX_LIST_ID must be an EC2 prefix list id." >&2
    exit 2
fi
if [[ ! "$AXON_VERIFIED_IMAGE_URI" =~ ^[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$ ]]; then
    echo "AXON_VERIFIED_IMAGE_URI must be an immutable private ECR URI in us-east-1." >&2
    exit 2
fi
if [ "$EDGE_MODE" = "custom-domain" ]; then
    if [[ "$AXON_VIEWER_CERTIFICATE_ARN" != arn:aws:acm:us-east-1:* ]] ||
       [[ "$AXON_ORIGIN_CERTIFICATE_ARN" != arn:aws:acm:us-east-1:* ]]; then
        echo "Both ACM certificate ARNs must be in us-east-1." >&2
        exit 2
    fi
fi

if [ -n "${AXON_DYNAMODB_TABLE_NAME:-}" ]; then
    TABLE_NAME="$AXON_DYNAMODB_TABLE_NAME"
elif [ -n "$DEPLOYMENT_NAMESPACE" ]; then
    TABLE_NAME="axonllm-${DEPLOYMENT_NAMESPACE}-state"
else
    TABLE_NAME="axonllm-state"
fi
RUNTIME_TABLE_NAME="${AXON_RUNTIME_STATE_TABLE_NAME:-}"
RECOVERY_CUTOVER_MODE="${AXON_RECOVERY_CUTOVER_MODE:-false}"
LOAD_DEMO_DATA="${AXON_LOAD_DEMO_DATA:-false}"
SCIM_SECRET_ARN="${AXON_SCIM_TENANTS_SECRET_ARN:-}"
HOSTED_ZONE_ID="${AXON_PUBLIC_HOSTED_ZONE_ID:-}"
HOSTED_ZONE_NAME="${AXON_PUBLIC_HOSTED_ZONE_NAME:-}"
if [ -n "$SCIM_SECRET_ARN" ] &&
   [[ ! "$SCIM_SECRET_ARN" =~ ^arn:aws:secretsmanager:us-east-1:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$ ]]; then
    echo "AXON_SCIM_TENANTS_SECRET_ARN must be a complete us-east-1 secret ARN." >&2
    exit 2
fi
if { [ -n "$HOSTED_ZONE_ID" ] && [ -z "$HOSTED_ZONE_NAME" ]; } ||
   { [ -z "$HOSTED_ZONE_ID" ] && [ -n "$HOSTED_ZONE_NAME" ]; }; then
    echo "AXON_PUBLIC_HOSTED_ZONE_ID and AXON_PUBLIC_HOSTED_ZONE_NAME must be set together." >&2
    exit 2
fi
if [ "$RECOVERY_CUTOVER_MODE" != "false" ] &&
   [ "$RECOVERY_CUTOVER_MODE" != "true" ]; then
    echo "AXON_RECOVERY_CUTOVER_MODE must be false or true." >&2
    exit 2
fi
if [ "$LOAD_DEMO_DATA" != "false" ] &&
   [ "$LOAD_DEMO_DATA" != "true" ]; then
    echo "AXON_LOAD_DEMO_DATA must be false or true." >&2
    exit 2
fi
if [ "$DEPLOYMENT_MODE" = "production" ] &&
   [ "$LOAD_DEMO_DATA" != "false" ]; then
    echo "AXON_LOAD_DEMO_DATA must be false in production." >&2
    exit 2
fi

# "broadening" still prompts, and only when a change widens IAM or network
# access; "never" prompts for nothing. Anything else CDK would reject.
if [ "$ASSUME_YES" = true ]; then
    APPROVAL=never
else
    APPROVAL=broadening
fi

echo "==> Deploying AxonLLM to ECS Fargate in ${REGION}..."
echo "    Stack: ${STACK_ID}"
echo "    Edge:  ${EDGE_MODE}"
echo "    Seed:  ${LOAD_DEMO_DATA}"
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
CONTEXT=(
    --context "region=$REGION"
    --context "table_name=$TABLE_NAME"
    --context "fargate_edge_mode=$EDGE_MODE"
)
if [ -n "$DEPLOYMENT_NAMESPACE" ]; then
    CONTEXT+=(--context "deployment_namespace=$DEPLOYMENT_NAMESPACE")
fi
if [ -n "$SCIM_SECRET_ARN" ]; then
    CONTEXT+=(--context "scim_tenants_secret_arn=$SCIM_SECRET_ARN")
fi

PARAMETERS=(
    --parameters "${STACK_ID}:DeploymentMode=$DEPLOYMENT_MODE"
    --parameters "${STACK_ID}:LoadDemoData=$LOAD_DEMO_DATA"
    --parameters "${STACK_ID}:ApprovedHttpsPrefixListId=${AXON_APPROVED_HTTPS_PREFIX_LIST_ID}"
    --parameters "${STACK_ID}:BedrockInvokeResourceArns=${AXON_BEDROCK_INVOKE_RESOURCE_ARNS}"
    --parameters "${STACK_ID}:VerifiedImageUri=${AXON_VERIFIED_IMAGE_URI}"
    --parameters "${STACK_ID}:RuntimeStateTableName=$RUNTIME_TABLE_NAME"
    --parameters "${STACK_ID}:RecoveryCutoverMode=$RECOVERY_CUTOVER_MODE"
    --parameters "${STACK_ID}:PublicHostedZoneId=$HOSTED_ZONE_ID"
    --parameters "${STACK_ID}:PublicHostedZoneName=$HOSTED_ZONE_NAME"
)
if [ "$EDGE_MODE" = "custom-domain" ]; then
    PARAMETERS+=(
        --parameters "${STACK_ID}:ViewerDomainName=${AXON_VIEWER_DOMAIN_NAME}"
        --parameters "${STACK_ID}:ViewerCertificateArn=${AXON_VIEWER_CERTIFICATE_ARN}"
        --parameters "${STACK_ID}:OriginDomainName=${AXON_ORIGIN_DOMAIN_NAME}"
        --parameters "${STACK_ID}:OriginCertificateArn=${AXON_ORIGIN_CERTIFICATE_ARN}"
    )
fi
if [ "$DEPLOYMENT_MODE" = "production" ]; then
    PARAMETERS+=(
        --parameters "${STACK_ID}:OidcIssuer=${AXON_OIDC_ISSUER}"
        --parameters "${STACK_ID}:OidcAuthorizationEndpoint=${AXON_OIDC_AUTHORIZATION_ENDPOINT}"
        --parameters "${STACK_ID}:OidcTokenEndpoint=${AXON_OIDC_TOKEN_ENDPOINT}"
        --parameters "${STACK_ID}:OidcUserInfoEndpoint=${AXON_OIDC_USER_INFO_ENDPOINT}"
        --parameters "${STACK_ID}:OidcClientId=${AXON_OIDC_CLIENT_ID}"
        --parameters "${STACK_ID}:OidcClientSecret=${AXON_OIDC_CLIENT_SECRET}"
        --parameters "${STACK_ID}:OidcAudience=${AXON_OIDC_AUDIENCE}"
    )
fi

npx cdk deploy "$STACK_ID" \
    "${CONTEXT[@]}" \
    "${PARAMETERS[@]}" \
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
if [ "$EDGE_MODE" = "custom-domain" ]; then
    echo "  2. Point ${AXON_VIEWER_DOMAIN_NAME} at the CloudFront distribution."
else
    echo "  2. Use the generated CloudFrontURL output directly."
fi
echo "  3. Mint an AxonLLM API key before using protected APIs."
