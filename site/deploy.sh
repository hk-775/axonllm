#!/usr/bin/env bash
#
# Deploy the AxonLLM landing page to S3 + CloudFront.
#
#   ./deploy.sh                                  # CloudFront domain only
#   ./deploy.sh axonllm.ai Z1234567890ABC        # custom domain + Route53
#
# Both arguments go together: a certificate with no DNS record never finishes
# validation, so passing a domain without its zone id hangs the deploy rather
# than failing fast.
set -euo pipefail

DOMAIN="${1:-}"
ZONE_ID="${2:-}"

cd "$(dirname "$0")"

if [[ -n "$DOMAIN" && -z "$ZONE_ID" ]]; then
    echo "error: domain given without a hosted zone id" >&2
    echo "usage: $0 [domain hosted_zone_id]" >&2
    exit 1
fi

if ! command -v cdk >/dev/null 2>&1; then
    echo "error: aws-cdk not found. npm install -g aws-cdk" >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv not found. curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

if [[ ! -d .venv ]]; then
    echo "==> creating virtualenv"
    uv venv .venv
fi

uv pip install --quiet --python .venv aws-cdk-lib 'constructs>=10.0.0'
# shellcheck source=/dev/null
source .venv/bin/activate

CONTEXT=()
if [[ -n "$DOMAIN" ]]; then
    CONTEXT+=(-c "domain_name=$DOMAIN" -c "hosted_zone_id=$ZONE_ID")
    echo "==> deploying with custom domain: $DOMAIN"
else
    echo "==> deploying to a CloudFront domain (no custom domain)"
fi

cd infra

echo "==> synth"
cdk synth --quiet "${CONTEXT[@]}"

echo "==> deploy"
cdk deploy --require-approval never "${CONTEXT[@]}"

echo
echo "==> done. The SiteURL output above is the live page."
