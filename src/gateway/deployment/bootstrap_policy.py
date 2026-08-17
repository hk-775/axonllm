"""Repository-owned IAM policy for AxonLLM CloudFormation execution."""

from __future__ import annotations

import json
from typing import Any


POLICY_NAME_PREFIX = "AxonLLMAgentCoreCloudFormationExecution"
BOUNDARY_NAME_PREFIX = "AxonLLMAgentCoreServiceBoundary"
BOOTSTRAP_BOUNDARY_NAME_PREFIX = "AxonLLMAgentCoreBootstrapBoundary"
EXECUTION_POLICY_PART_COUNT = 3
IAM_MANAGED_POLICY_SIZE_LIMIT = 6_144
_EXECUTION_POLICY_TARGET_SIZE = 5_900

PRODUCTION_QUALIFIER = "axprod"
QUALIFICATION_QUALIFIER = "axqual"
EXTERNAL_QUALIFIER = "axext"

_REGIONAL_ACTIONS = (
    "acm:DescribeCertificate",
    "application-autoscaling:DeleteScalingPolicy",
    "application-autoscaling:DeregisterScalableTarget",
    "application-autoscaling:DescribeScalableTargets",
    "application-autoscaling:DescribeScalingPolicies",
    "application-autoscaling:PutScalingPolicy",
    "application-autoscaling:RegisterScalableTarget",
    "bedrock-agentcore:CreateAgentRuntime",
    "bedrock-agentcore:CreateAgentRuntimeEndpoint",
    "bedrock-agentcore:CreateWorkloadIdentity",
    "bedrock-agentcore:DeleteAgentRuntime",
    "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
    "bedrock-agentcore:DeleteWorkloadIdentity",
    "bedrock-agentcore:GetAgentRuntime",
    "bedrock-agentcore:GetAgentRuntimeEndpoint",
    "bedrock-agentcore:AllowVendedLogDeliveryForResource",
    "bedrock-agentcore:ListAgentRuntimeEndpoints",
    "bedrock-agentcore:ListAgentRuntimeVersions",
    "bedrock-agentcore:ListAgentRuntimes",
    "bedrock-agentcore:ListTagsForResource",
    "bedrock-agentcore:TagResource",
    "bedrock-agentcore:UntagResource",
    "bedrock-agentcore:UpdateAgentRuntime",
    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
    "cloudformation:DescribeStacks",
    "cloudwatch:DeleteAlarms",
    "cloudwatch:DeleteDashboards",
    "cloudwatch:DescribeAlarms",
    "cloudwatch:GetDashboard",
    "cloudwatch:ListTagsForResource",
    "cloudwatch:PutDashboard",
    "cloudwatch:PutMetricAlarm",
    "cloudwatch:TagResource",
    "cloudwatch:UntagResource",
    "cognito-idp:CreateUserPool",
    "cognito-idp:CreateUserPoolClient",
    "cognito-idp:CreateUserPoolDomain",
    "cognito-idp:DeleteUserPool",
    "cognito-idp:DeleteUserPoolClient",
    "cognito-idp:DeleteUserPoolDomain",
    "cognito-idp:DescribeUserPool",
    "cognito-idp:DescribeUserPoolClient",
    "cognito-idp:DescribeUserPoolDomain",
    "cognito-idp:GetUserPoolMfaConfig",
    "cognito-idp:ListTagsForResource",
    "cognito-idp:SetUserPoolMfaConfig",
    "cognito-idp:TagResource",
    "cognito-idp:UntagResource",
    "cognito-idp:UpdateUserPool",
    "cognito-idp:UpdateUserPoolClient",
    "dynamodb:CreateTable",
    "dynamodb:DeleteTable",
    "dynamodb:DescribeContinuousBackups",
    "dynamodb:DescribeContributorInsights",
    "dynamodb:DescribeKinesisStreamingDestination",
    "dynamodb:DescribeTable",
    "dynamodb:DescribeTimeToLive",
    "dynamodb:GetResourcePolicy",
    "dynamodb:ListTagsOfResource",
    "dynamodb:TagResource",
    "dynamodb:UntagResource",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateTimeToLive",
    "ec2:AllocateAddress",
    "ec2:AssociateRouteTable",
    "ec2:AttachInternetGateway",
    "ec2:AuthorizeSecurityGroupEgress",
    "ec2:AuthorizeSecurityGroupIngress",
    "ec2:CreateInternetGateway",
    "ec2:CreateNatGateway",
    "ec2:CreateNetworkInterface",
    "ec2:CreateRoute",
    "ec2:CreateRouteTable",
    "ec2:CreateSecurityGroup",
    "ec2:CreateSubnet",
    "ec2:CreateTags",
    "ec2:CreateVpc",
    "ec2:CreateVpcEndpoint",
    "ec2:DeleteInternetGateway",
    "ec2:DeleteNatGateway",
    "ec2:DeleteNetworkInterface",
    "ec2:DeleteRoute",
    "ec2:DeleteRouteTable",
    "ec2:DeleteSecurityGroup",
    "ec2:DeleteSubnet",
    "ec2:DeleteTags",
    "ec2:DeleteVpc",
    "ec2:DeleteVpcEndpoints",
    "ec2:DescribeAddresses",
    "ec2:DescribeAvailabilityZones",
    "ec2:DescribeInternetGateways",
    "ec2:DescribeManagedPrefixLists",
    "ec2:DescribeNatGateways",
    "ec2:DescribeNetworkAcls",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DescribeRouteTables",
    "ec2:DescribeSecurityGroupRules",
    "ec2:DescribeSecurityGroups",
    "ec2:DescribeSubnets",
    "ec2:DescribeTags",
    "ec2:DescribeVpcAttribute",
    "ec2:DescribeVpcEndpoints",
    "ec2:DescribeVpcs",
    "ec2:DetachInternetGateway",
    "ec2:DisassociateRouteTable",
    "ec2:ModifySubnetAttribute",
    "ec2:ModifyVpcAttribute",
    "ec2:ModifyVpcEndpoint",
    "ec2:ReleaseAddress",
    "ec2:RevokeSecurityGroupEgress",
    "ec2:RevokeSecurityGroupIngress",
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:DescribeImages",
    "ecr:GetAuthorizationToken",
    "ecr:GetDownloadUrlForLayer",
    "ecs:CreateCluster",
    "ecs:CreateService",
    "ecs:DeleteCluster",
    "ecs:DeleteService",
    "ecs:DeregisterTaskDefinition",
    "ecs:DescribeClusters",
    "ecs:DescribeServices",
    "ecs:DescribeTaskDefinition",
    "ecs:ListTagsForResource",
    "ecs:RegisterTaskDefinition",
    "ecs:TagResource",
    "ecs:UntagResource",
    "ecs:UpdateCluster",
    "ecs:UpdateClusterSettings",
    "ecs:UpdateService",
    "elasticloadbalancing:AddTags",
    "elasticloadbalancing:CreateListener",
    "elasticloadbalancing:CreateLoadBalancer",
    "elasticloadbalancing:CreateRule",
    "elasticloadbalancing:CreateTargetGroup",
    "elasticloadbalancing:DeleteListener",
    "elasticloadbalancing:DeleteLoadBalancer",
    "elasticloadbalancing:DeleteRule",
    "elasticloadbalancing:DeleteTargetGroup",
    "elasticloadbalancing:DescribeListeners",
    "elasticloadbalancing:DescribeLoadBalancerAttributes",
    "elasticloadbalancing:DescribeLoadBalancers",
    "elasticloadbalancing:DescribeRules",
    "elasticloadbalancing:DescribeTargetGroupAttributes",
    "elasticloadbalancing:DescribeTargetGroups",
    "elasticloadbalancing:DescribeTags",
    "elasticloadbalancing:ModifyListener",
    "elasticloadbalancing:ModifyLoadBalancerAttributes",
    "elasticloadbalancing:ModifyRule",
    "elasticloadbalancing:ModifyTargetGroup",
    "elasticloadbalancing:ModifyTargetGroupAttributes",
    "elasticloadbalancing:RemoveTags",
    "elasticloadbalancing:SetSecurityGroups",
    "elasticloadbalancing:SetSubnets",
    "lambda:AddPermission",
    "lambda:CreateFunction",
    "lambda:DeleteFunction",
    "lambda:GetCodeSigningConfig",
    "lambda:GetFunction",
    "lambda:GetFunctionCodeSigningConfig",
    "lambda:GetFunctionConfiguration",
    "lambda:GetFunctionRecursionConfig",
    "lambda:GetFunctionScalingConfig",
    "lambda:GetPolicy",
    "lambda:GetRuntimeManagementConfig",
    "lambda:ListTags",
    "lambda:RemovePermission",
    "lambda:TagResource",
    "lambda:UntagResource",
    "lambda:UpdateFunctionCode",
    "lambda:UpdateFunctionConfiguration",
    "logs:CreateDelivery",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:DeleteDelivery",
    "logs:DeleteDeliveryDestination",
    "logs:DeleteDeliveryDestinationPolicy",
    "logs:DeleteDeliverySource",
    "logs:DeleteLogGroup",
    "logs:DeleteLogStream",
    "logs:DeleteResourcePolicy",
    "logs:DeleteRetentionPolicy",
    "logs:DescribeDeliveries",
    "logs:DescribeDeliveryDestinations",
    "logs:DescribeDeliverySources",
    "logs:DescribeIndexPolicies",
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:DescribeResourcePolicies",
    "logs:GetDelivery",
    "logs:GetDeliveryDestination",
    "logs:GetDeliveryDestinationPolicy",
    "logs:GetDeliverySource",
    "logs:GetDataProtectionPolicy",
    "logs:ListTagsForResource",
    "logs:PutDeliveryDestination",
    "logs:PutDeliveryDestinationPolicy",
    "logs:PutDeliverySource",
    "logs:PutResourcePolicy",
    "logs:PutRetentionPolicy",
    "logs:TagResource",
    "logs:UntagResource",
    "logs:UpdateDeliveryConfiguration",
    "secretsmanager:CreateSecret",
    "secretsmanager:DeleteSecret",
    "secretsmanager:DescribeSecret",
    "secretsmanager:GetRandomPassword",
    "secretsmanager:GetResourcePolicy",
    "secretsmanager:PutResourcePolicy",
    "secretsmanager:RemoveRegionsFromReplication",
    "secretsmanager:ReplicateSecretToRegions",
    "secretsmanager:TagResource",
    "secretsmanager:UntagResource",
    "secretsmanager:UpdateSecret",
    "sns:CreateTopic",
    "sns:DeleteTopic",
    "sns:GetDataProtectionPolicy",
    "sns:GetSubscriptionAttributes",
    "sns:GetTopicAttributes",
    "sns:ListSubscriptionsByTopic",
    "sns:ListTagsForResource",
    "sns:SetSubscriptionAttributes",
    "sns:SetTopicAttributes",
    "sns:Subscribe",
    "sns:TagResource",
    "sns:Unsubscribe",
    "sns:UntagResource",
    "sqs:CreateQueue",
    "sqs:DeleteQueue",
    "sqs:GetQueueAttributes",
    "sqs:GetQueueUrl",
    "sqs:ListQueueTags",
    "sqs:SetQueueAttributes",
    "sqs:TagQueue",
    "sqs:UntagQueue",
    "vpc-lattice:AssociateViaAWSService",
    "vpc-lattice:CreateServiceNetworkResourceAssociation",
    "vpc-lattice:GetResourceConfiguration",
    "vpc-lattice:GetServiceNetworkResourceAssociation",
    "vpc-lattice:ListServiceNetworkResourceAssociations",
    "wafv2:CreateIPSet",
    "wafv2:CreateWebACL",
    "wafv2:DeleteIPSet",
    "wafv2:DeleteWebACL",
    "wafv2:GetIPSet",
    "wafv2:GetWebACL",
    "wafv2:GetWebACLForResource",
    "wafv2:ListIPSets",
    "wafv2:ListTagsForResource",
    "wafv2:ListWebACLs",
    "wafv2:TagResource",
    "wafv2:UntagResource",
    "wafv2:UpdateIPSet",
    "wafv2:UpdateWebACL",
    "xray:DeleteResourcePolicy",
    "xray:ListResourcePolicies",
    "xray:PutResourcePolicy",
)

_GLOBAL_ACTIONS = (
    "cloudfront:CreateDistribution",
    "cloudfront:CreateFunction",
    "cloudfront:CreateVpcOrigin",
    "cloudfront:DeleteDistribution",
    "cloudfront:DeleteFunction",
    "cloudfront:DeleteVpcOrigin",
    "cloudfront:DescribeFunction",
    "cloudfront:GetDistribution",
    "cloudfront:GetDistributionConfig",
    "cloudfront:GetFunction",
    "cloudfront:GetVpcOrigin",
    "cloudfront:ListDistributions",
    "cloudfront:ListFunctions",
    "cloudfront:ListTagsForResource",
    "cloudfront:ListVpcOrigins",
    "cloudfront:PublishFunction",
    "cloudfront:TagResource",
    "cloudfront:UntagResource",
    "cloudfront:UpdateDistribution",
    "cloudfront:UpdateFunction",
    "cloudfront:UpdateVpcOrigin",
    "route53:ChangeResourceRecordSets",
    "route53:GetChange",
    "route53:GetHostedZone",
    "route53:ListResourceRecordSets",
    "s3:CreateBucket",
    "s3:DeleteBucket",
    "s3:DeleteBucketPolicy",
    "s3:GetBucketAcl",
    "s3:GetBucketLocation",
    "s3:GetBucketOwnershipControls",
    "s3:GetBucketPolicy",
    "s3:GetBucketPolicyStatus",
    "s3:GetBucketPublicAccessBlock",
    "s3:GetBucketTagging",
    "s3:GetBucketVersioning",
    "s3:GetEncryptionConfiguration",
    "s3:GetLifecycleConfiguration",
    "s3:ListBucket",
    "s3:PutBucketOwnershipControls",
    "s3:PutBucketPolicy",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketTagging",
    "s3:PutEncryptionConfiguration",
    "s3:PutLifecycleConfiguration",
)

_IAM_ROLE_READ_ACTIONS = (
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListAttachedRolePolicies",
    "iam:ListRolePolicies",
)

_IAM_ROLE_CLEANUP_ACTIONS = (
    "iam:DeleteRole",
    "iam:DeleteRolePermissionsBoundary",
    "iam:DeleteRolePolicy",
)

_IAM_ROLE_MANAGEMENT_ACTIONS = (
    "iam:PutRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:UpdateRole",
    "iam:UpdateRoleDescription",
)

_APPROVED_MANAGED_ROLE_POLICY = "policy/service-role/AWSLambdaBasicExecutionRole"
_IAM_PASS_SERVICES = (
    "bedrock-agentcore.amazonaws.com",
    "ecs-tasks.amazonaws.com",
    "lambda.amazonaws.com",
)

_APPLICATION_TAG = "Application"
_TRUST_DOMAIN_TAG = "AxonLLMTrustDomain"


def qualifier_for_namespace(namespace: str | None) -> str:
    """Return the isolated CDK qualifier for a deployment namespace."""
    if not namespace:
        return PRODUCTION_QUALIFIER
    if namespace in {"external", "external-oidc"}:
        return EXTERNAL_QUALIFIER
    return QUALIFICATION_QUALIFIER


def toolkit_stack_name(qualifier: str) -> str:
    """Return the deterministic CDK toolkit stack name."""
    return f"AxonLLMToolkit-{qualifier}"


def policy_name(region: str, *, qualifier: str = PRODUCTION_QUALIFIER) -> str:
    """Return the deterministic managed-policy name for one launch region."""
    return f"{POLICY_NAME_PREFIX}-{qualifier}-{region}"


def policy_part_name(
    region: str,
    *,
    part: int,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> str:
    """Return one deterministic execution-policy part name."""
    if not isinstance(part, int) or isinstance(part, bool) or not (1 <= part <= EXECUTION_POLICY_PART_COUNT):
        raise ValueError(f"execution policy part must be between 1 and {EXECUTION_POLICY_PART_COUNT}")
    return f"{policy_name(region, qualifier=qualifier)}-part{part}"


def policy_part_arn(
    *,
    partition: str,
    account_id: str,
    region: str,
    part: int,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> str:
    """Return one deterministic execution-policy part ARN."""
    return f"arn:{partition}:iam::{account_id}:policy/{policy_part_name(region, part=part, qualifier=qualifier)}"


def boundary_name(
    region: str,
    *,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> str:
    """Return the exact service-role boundary name for one trust domain."""
    return f"{BOUNDARY_NAME_PREFIX}-{qualifier}-{region}"


def boundary_arn(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> str:
    """Return the exact service-role permissions-boundary ARN."""
    return f"arn:{partition}:iam::{account_id}:policy/{boundary_name(region, qualifier=qualifier)}"


def _role_resources(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str,
) -> list[str]:
    if qualifier == PRODUCTION_QUALIFIER:
        stack_names = (
            "AxonLLMAgentCoreStack",
            "AxonLLMControlPlaneStack",
            "AxonLLMIdentityStack",
        )
        explicit_names = (f"axonllm-agentcore-runtime-{region}",)
    elif qualifier == EXTERNAL_QUALIFIER:
        stack_names = (
            "AxonLLMAgentCoreStack-external",
            "AxonLLMAgentCoreStack-external-oidc",
        )
        explicit_names = (
            f"axonllm-agentcore-runtime-external-{region}",
            f"axonllm-agentcore-runtime-external-oidc-{region}",
        )
    else:
        stack_names = (
            "AxonLLMAgentCoreStack-managed",
            "AxonLLMControlPlaneStack-managed",
            "AxonLLMIdentityStack-managed",
            "AxonLLMLaunchWorkersStack-managed",
        )
        explicit_names = (
            "AxonLLMLaunchWorkerExecutionRole-managed",
            f"axonllm-agentcore-runtime-managed-{region}",
        )
    names = {
        *(f"{stack_name[:25]}-*" for stack_name in stack_names),
        *explicit_names,
    }
    return [f"arn:{partition}:iam::{account_id}:role/{name}" for name in sorted(names)]


def _untagged_cdk_provider_role_resources(
    *,
    partition: str,
    account_id: str,
    qualifier: str,
) -> list[str]:
    if qualifier == PRODUCTION_QUALIFIER:
        stack_names = (
            "AxonLLMAgentCoreStack",
            "AxonLLMControlPlaneStack",
        )
    elif qualifier == EXTERNAL_QUALIFIER:
        stack_names = (
            "AxonLLMAgentCoreStack-external",
            "AxonLLMAgentCoreStack-external-oidc",
        )
    else:
        stack_names = (
            "AxonLLMAgentCoreStack-managed",
            "AxonLLMControlPlaneStack-managed",
        )
    return sorted({(f"arn:{partition}:iam::{account_id}:role/{stack_name[:25]}-Custom*") for stack_name in stack_names})


def boundary_document(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> dict[str, Any]:
    """Build the mandatory anti-escalation boundary for service roles."""
    del account_id, qualifier
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowReviewedServicePermissions",
                "Effect": "Allow",
                "Action": "*",
                "Resource": "*",
            },
            {
                "Sid": "DenyIdentityAndAccountAdministration",
                "Effect": "Deny",
                "Action": [
                    "account:*",
                    "iam:*",
                    "organizations:*",
                    "sso:*",
                    "sso-admin:*",
                ],
                "Resource": "*",
            },
            {
                "Sid": "DenyControlPlaneCreation",
                "Effect": "Deny",
                "Action": [
                    "cloudformation:CreateChangeSet",
                    "cloudformation:CreateStack",
                    "cloudformation:DeleteStack",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:ImportStacksToStackSet",
                    "cloudformation:UpdateStack",
                    "cloudformation:UpdateStackSet",
                    "kms:CancelKeyDeletion",
                    "kms:CreateAlias",
                    "kms:CreateGrant",
                    "kms:CreateKey",
                    "kms:DeleteAlias",
                    "kms:DisableKey",
                    "kms:PutKeyPolicy",
                    "kms:ScheduleKeyDeletion",
                    "kms:Sign",
                    "lambda:CreateFunction",
                    "lambda:UpdateFunctionCode",
                ],
                "Resource": "*",
            },
            {
                "Sid": "DenyReleaseEvidenceAccess",
                "Effect": "Deny",
                "Action": "s3:*",
                "Resource": [
                    (f"arn:{partition}:s3:::axonllm-deployment-evidence-*"),
                    (f"arn:{partition}:s3:::axonllm-deployment-evidence-*/*"),
                ],
            },
            {
                "Sid": "DenyCrossRegionUse",
                "Effect": "Deny",
                "Action": "*",
                "Resource": "*",
                "Condition": {
                    "Null": {
                        "aws:RequestedRegion": "false",
                    },
                    "StringNotEquals": {
                        "aws:RequestedRegion": region,
                    },
                },
            },
        ],
    }


def bootstrap_boundary_name(
    region: str,
    *,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> str:
    """Return the boundary name attached to isolated CDK bootstrap roles."""
    return f"{BOOTSTRAP_BOUNDARY_NAME_PREFIX}-{qualifier}-{region}"


def bootstrap_boundary_arn(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> str:
    """Return the exact CDK bootstrap-role permissions-boundary ARN."""
    return f"arn:{partition}:iam::{account_id}:policy/{bootstrap_boundary_name(region, qualifier=qualifier)}"


def bootstrap_boundary_document(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> dict[str, Any]:
    """Build defense-in-depth limits for the CDK toolkit roles."""
    del account_id, region, qualifier
    approved_managed_policy = f"arn:{partition}:iam::aws:{_APPROVED_MANAGED_ROLE_POLICY}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAttachedBootstrapPermissions",
                "Effect": "Allow",
                "Action": "*",
                "Resource": "*",
            },
            {
                "Sid": "DenyAccountAndIdentityEscalation",
                "Effect": "Deny",
                "Action": [
                    "account:*",
                    "iam:AddUserToGroup",
                    "iam:AttachGroupPolicy",
                    "iam:AttachUserPolicy",
                    "iam:CreateAccessKey",
                    "iam:CreateGroup",
                    "iam:CreateLoginProfile",
                    "iam:CreatePolicy",
                    "iam:CreatePolicyVersion",
                    "iam:CreateUser",
                    "iam:PutGroupPolicy",
                    "iam:PutUserPolicy",
                    "iam:SetDefaultPolicyVersion",
                    "iam:UpdateAssumeRolePolicy",
                    "organizations:*",
                    "sso:*",
                    "sso-admin:*",
                ],
                "Resource": "*",
            },
            {
                "Sid": "DenyUnapprovedRolePolicyAttachments",
                "Effect": "Deny",
                "Action": "iam:AttachRolePolicy",
                "Resource": "*",
                "Condition": {
                    "ArnNotEquals": {
                        "iam:PolicyARN": approved_managed_policy,
                    }
                },
            },
            {
                "Sid": "DenyReleaseSigningAndEvidence",
                "Effect": "Deny",
                "Action": [
                    "kms:Sign",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                ],
                "Resource": [
                    (f"arn:{partition}:s3:::axonllm-deployment-evidence-*/*"),
                    f"arn:{partition}:kms:*:*:alias/axonllm/*signing*",
                ],
            },
        ],
    }


def policy_document(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> dict[str, Any]:
    """Build the exact bounded policy accepted by the AgentCore launcher."""
    role_resources = _role_resources(
        partition=partition,
        account_id=account_id,
        region=region,
        qualifier=qualifier,
    )
    required_boundary = boundary_arn(
        partition=partition,
        account_id=account_id,
        region=region,
        qualifier=qualifier,
    )
    untagged_provider_roles = _untagged_cdk_provider_role_resources(
        partition=partition,
        account_id=account_id,
        qualifier=qualifier,
    )
    role_lifecycle_resources = role_resources
    approved_managed_policy = f"arn:{partition}:iam::aws:{_APPROVED_MANAGED_ROLE_POLICY}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RegionalAxonLLMInfrastructure",
                "Effect": "Allow",
                "Action": list(_REGIONAL_ACTIONS),
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": region}},
            },
            {
                "Sid": "GlobalAxonLLMInfrastructure",
                "Effect": "Allow",
                "Action": list(_GLOBAL_ACTIONS),
                "Resource": "*",
            },
            {
                "Sid": "ReadCdkDeploymentAssets",
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": (f"arn:{partition}:s3:::cdk-{qualifier}-assets-{account_id}-{region}/*"),
            },
            {
                "Sid": "ReadCdkBootstrapVersion",
                "Effect": "Allow",
                "Action": "ssm:GetParameters",
                "Resource": (f"arn:{partition}:ssm:{region}:{account_id}:parameter/cdk-bootstrap/{qualifier}/version"),
                "Condition": {
                    "StringEquals": {
                        "aws:RequestedRegion": region,
                    }
                },
            },
            {
                "Sid": "CreateBoundedAxonLLMServiceRoles",
                "Effect": "Allow",
                "Action": "iam:CreateRole",
                "Resource": role_resources,
                "Condition": {
                    "StringEquals": {
                        "iam:PermissionsBoundary": required_boundary,
                        f"aws:RequestTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:RequestTag/{_TRUST_DOMAIN_TAG}": qualifier,
                    }
                },
            },
            {
                "Sid": "SetRequiredServiceRoleBoundary",
                "Effect": "Allow",
                "Action": "iam:PutRolePermissionsBoundary",
                "Resource": role_resources,
                "Condition": {
                    "StringEquals": {
                        "iam:PermissionsBoundary": required_boundary,
                        f"aws:ResourceTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:ResourceTag/{_TRUST_DOMAIN_TAG}": qualifier,
                    }
                },
            },
            {
                "Sid": "CreateOrBoundCdkProviderRoles",
                "Effect": "Allow",
                "Action": [
                    "iam:CreateRole",
                    "iam:PutRolePermissionsBoundary",
                ],
                "Resource": untagged_provider_roles,
                "Condition": {
                    "StringEquals": {
                        "iam:PermissionsBoundary": required_boundary,
                    }
                },
            },
            {
                "Sid": "ManageBoundedAxonLLMServiceRoles",
                "Effect": "Allow",
                "Action": list(_IAM_ROLE_MANAGEMENT_ACTIONS),
                "Resource": role_resources,
                "Condition": {
                    "StringEquals": {
                        f"aws:ResourceTag/{_APPLICATION_TAG}": "AxonLLM",
                        f"aws:ResourceTag/{_TRUST_DOMAIN_TAG}": qualifier,
                    }
                },
            },
            {
                "Sid": "ReadAndCleanUpAxonLLMServiceRoles",
                "Effect": "Allow",
                "Action": [
                    *_IAM_ROLE_READ_ACTIONS,
                    *_IAM_ROLE_CLEANUP_ACTIONS,
                ],
                "Resource": role_lifecycle_resources,
            },
            {
                "Sid": "ManageApprovedRolePolicyAttachment",
                "Effect": "Allow",
                "Action": [
                    "iam:AttachRolePolicy",
                    "iam:DetachRolePolicy",
                ],
                "Resource": role_lifecycle_resources,
                "Condition": {
                    "ArnEquals": {
                        "iam:PolicyARN": approved_managed_policy,
                    }
                },
            },
            {
                "Sid": "ManageBoundedCdkProviderRoles",
                "Effect": "Allow",
                "Action": list(_IAM_ROLE_MANAGEMENT_ACTIONS),
                "Resource": untagged_provider_roles,
            },
            {
                "Sid": "DenyChangingApplicationTag",
                "Effect": "Deny",
                "Action": "iam:TagRole",
                "Resource": role_resources,
                "Condition": {
                    "ForAnyValue:StringEquals": {
                        "aws:TagKeys": _APPLICATION_TAG,
                    },
                    "StringNotEquals": {
                        f"aws:RequestTag/{_APPLICATION_TAG}": "AxonLLM",
                    },
                },
            },
            {
                "Sid": "DenyChangingTrustDomainTag",
                "Effect": "Deny",
                "Action": "iam:TagRole",
                "Resource": role_resources,
                "Condition": {
                    "ForAnyValue:StringEquals": {
                        "aws:TagKeys": _TRUST_DOMAIN_TAG,
                    },
                    "StringNotEquals": {
                        f"aws:RequestTag/{_TRUST_DOMAIN_TAG}": qualifier,
                    },
                },
            },
            {
                "Sid": "DenyRemovingBoundaryTags",
                "Effect": "Deny",
                "Action": "iam:UntagRole",
                "Resource": role_resources,
                "Condition": {
                    "ForAnyValue:StringEquals": {
                        "aws:TagKeys": [
                            _APPLICATION_TAG,
                            _TRUST_DOMAIN_TAG,
                        ]
                    }
                },
            },
            {
                "Sid": "PassAxonLLMServiceRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": role_resources,
                "Condition": {"StringEquals": {"iam:PassedToService": list(_IAM_PASS_SERVICES)}},
            },
            {
                "Sid": "DenyCrossNamespaceRoleManagement",
                "Effect": "Deny",
                "Action": [
                    "iam:CreateRole",
                    "iam:PutRolePermissionsBoundary",
                    "iam:PutRolePolicy",
                ],
                "NotResource": role_resources,
            },
            {
                "Sid": "CreateRequiredServiceLinkedRoles",
                "Effect": "Allow",
                "Action": "iam:CreateServiceLinkedRole",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "iam:AWSServiceName": [
                            "bedrock-agentcore.amazonaws.com",
                            "cloudfront.amazonaws.com",
                            "ecs.amazonaws.com",
                            "ecs.application-autoscaling.amazonaws.com",
                            "email.cognito-idp.amazonaws.com",
                            "elasticloadbalancing.amazonaws.com",
                        ]
                    }
                },
            },
        ],
    }


def _policy_document_size(statements: list[dict[str, Any]]) -> int:
    return len(
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": statements,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _split_oversized_statement(
    statement: dict[str, Any],
) -> list[dict[str, Any]]:
    if _policy_document_size([statement]) <= _EXECUTION_POLICY_TARGET_SIZE:
        return [statement]
    actions = statement.get("Action")
    sid = statement.get("Sid")
    if not isinstance(actions, list) or not actions or not isinstance(sid, str):
        raise ValueError("an oversized bootstrap-policy statement cannot be partitioned")

    chunks: list[list[str]] = []
    current: list[str] = []
    for action in actions:
        candidate = {
            **statement,
            "Sid": f"{sid}Part{len(chunks) + 1}",
            "Action": [*current, action],
        }
        if current and _policy_document_size([candidate]) > _EXECUTION_POLICY_TARGET_SIZE:
            chunks.append(current)
            current = [action]
        else:
            current.append(action)
    chunks.append(current)
    return [
        {
            **statement,
            "Sid": f"{sid}Part{index}",
            "Action": chunk,
        }
        for index, chunk in enumerate(chunks, start=1)
    ]


def policy_documents(
    *,
    partition: str,
    account_id: str,
    region: str,
    qualifier: str = PRODUCTION_QUALIFIER,
) -> tuple[dict[str, Any], ...]:
    """Partition the bounded execution policy into IAM-sized documents."""
    complete = policy_document(
        partition=partition,
        account_id=account_id,
        region=region,
        qualifier=qualifier,
    )
    statements: list[dict[str, Any]] = []
    for statement in complete["Statement"]:
        statements.extend(_split_oversized_statement(statement))

    documents: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for statement in statements:
        if current and _policy_document_size([*current, statement]) > _EXECUTION_POLICY_TARGET_SIZE:
            documents.append(
                {
                    "Version": "2012-10-17",
                    "Statement": current,
                }
            )
            current = [statement]
        else:
            current.append(statement)
    if current:
        documents.append(
            {
                "Version": "2012-10-17",
                "Statement": current,
            }
        )

    if len(documents) != EXECUTION_POLICY_PART_COUNT or any(
        _policy_document_size(document["Statement"]) > IAM_MANAGED_POLICY_SIZE_LIMIT for document in documents
    ):
        raise ValueError(
            f"bootstrap execution policy no longer fits its reviewed {EXECUTION_POLICY_PART_COUNT}-part IAM contract"
        )
    return tuple(documents)
