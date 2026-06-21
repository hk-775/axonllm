#!/bin/bash
# Deploy AxonLLM to AWS App Runner via ECR
# Usage: ./deploy.sh [region]
#
# Prerequisites:
#   - Docker running
#   - AWS CLI configured (aws sts get-caller-identity should work)
#   - Sufficient IAM permissions for ECR + App Runner

set -euo pipefail

REGION="${1:-us-east-1}"
REPO_NAME="axon-llm"
SERVICE_NAME="axon-llm"
IMAGE_TAG="latest"
export AWS_PROFILE="default"

# API keys should be set in your environment before running this script:
# export ANTHROPIC_API_KEY=sk-ant-your-key
# export OPENAI_API_KEY=sk-your-key

 #Workaround: use a minimal config to avoid broken profiles
 
TEMP_CONFIG=$(mktemp)
echo -e "[default]\nregion = ${REGION}\noutput = json" > "$TEMP_CONFIG"
export AWS_CONFIG_FILE="$TEMP_CONFIG"

echo "==> Getting AWS account ID..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"

echo "==> Creating ECR repository (if not exists)..."
aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" 2>/dev/null \
  || aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION" --image-scanning-configuration scanOnPush=true

echo "==> Logging into ECR..."
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Building Docker image..."
docker build --platform linux/amd64 -t "${REPO_NAME}:${IMAGE_TAG}" .

echo "==> Tagging and pushing to ECR..."
docker tag "${REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"

echo "==> Checking for existing App Runner service..."
EXISTING=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn" --output text 2>/dev/null || true)

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "==> Updating existing App Runner service..."
  aws apprunner update-service \
    --service-arn "$EXISTING" \
    --source-configuration "{
      \"ImageRepository\": {
        \"ImageIdentifier\": \"${ECR_URI}:${IMAGE_TAG}\",
        \"ImageRepositoryType\": \"ECR\",
        \"ImageConfiguration\": {
          \"Port\": \"8000\"
        }
      },
      \"AutoDeploymentsEnabled\": false,
      \"AuthenticationConfiguration\": {
        \"AccessRoleArn\": \"$(aws apprunner list-services --region $REGION --query \"ServiceSummaryList[?ServiceName=='${SERVICE_NAME}']\" --output json | python3 -c 'import sys,json; print(\"\")')\"
      }
    }" \
    --region "$REGION" 2>/dev/null || echo "Update may require manual intervention — check the App Runner console."
else
  echo ""
  echo "==> No existing App Runner service found."
  echo ""
  echo "Image pushed to: ${ECR_URI}:${IMAGE_TAG}"
  echo ""
  echo "Create the App Runner service in the AWS Console:"
  echo "  1. Go to https://console.aws.amazon.com/apprunner"
  echo "  2. Click 'Create service'"
  echo "  3. Source: Container registry → Amazon ECR"
  echo "  4. Image URI: ${ECR_URI}:${IMAGE_TAG}"
  echo "  5. Port: 8000"
  echo "  6. Create and use a new service role (for ECR access)"
  echo "  7. Click 'Create & deploy'"
  echo ""
  echo "Or create via CLI (requires an ECR access role ARN):"
  echo ""
  echo "  aws apprunner create-service \\"
  echo "    --service-name ${SERVICE_NAME} \\"
  echo "    --source-configuration '{\"ImageRepository\":{\"ImageIdentifier\":\"${ECR_URI}:${IMAGE_TAG}\",\"ImageRepositoryType\":\"ECR\",\"ImageConfiguration\":{\"Port\":\"8000\"}},\"AutoDeploymentsEnabled\":false,\"AuthenticationConfiguration\":{\"AccessRoleArn\":\"YOUR_ECR_ACCESS_ROLE_ARN\"}}' \\"
  echo "    --instance-configuration '{\"Cpu\":\"1024\",\"Memory\":\"2048\"}' \\"
  echo "    --region ${REGION}"
fi

echo ""
echo "Done! Once deployed, your dashboard will be at:"
echo "  https://<service-id>.${REGION}.awsapprunner.com/admin/dashboard"
