"""Production Bedrock AgentCore deployment for the AxonLLM agent entrypoint."""

import json
import re

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CfnResource,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_bedrockagentcore as agentcore,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    custom_resources as cr,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_sqs as sqs,
)
from constructs import Construct


_AGENTCORE_MAX_SESSION_SECONDS = 4 * 60 * 60
_FACADE_IDENTITY_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Identity-Token"
_RECOVERY_PROPAGATION_MARGIN_SECONDS = 5 * 60
_RECOVERY_MIN_QUIESCENCE_SECONDS = _AGENTCORE_MAX_SESSION_SECONDS + _RECOVERY_PROPAGATION_MARGIN_SECONDS
_RUNTIME_DYNAMODB_STANDARD_ACTIONS = [
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:UpdateItem",
]
_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS = [
    "dynamodb:TransactWriteItems",
]
_RUNTIME_DYNAMODB_ACTIONS = [
    *_RUNTIME_DYNAMODB_STANDARD_ACTIONS,
    *_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS,
]
_RUNTIME_SQS_ACTIONS = [
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
]
_AGENTCORE_ENABLED_PROVIDERS = ",".join(
    (
        "anthropic",
        "bedrock",
        "bedrock-mantle",
        "fireworks",
        "google_ai",
        "groq",
        "openai",
        "together",
        "xai",
    )
)
_PROVIDER_SECRET_FIELDS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "GCP_CREDENTIALS_JSON",
    "GCP_PROJECT_ID",
    "GCP_LOCATION",
    "VERTEX_AI_ENDPOINT",
    "GOOGLE_AI_API_KEY",
    "COHERE_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "AI21_API_KEY",
)
_PROVIDER_NAME_PATTERN = (
    r"^(?:ai21|anthropic|azure_openai|bedrock|bedrock-mantle|cohere|"
    r"fireworks|google_ai|groq|openai|together|vertex_ai|xai)"
    r"(?:,(?:ai21|anthropic|azure_openai|bedrock|bedrock-mantle|cohere|"
    r"fireworks|google_ai|groq|openai|together|vertex_ai|xai))*$"
)


_AGENTCORE_RECOVERY_GUARD = """\
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


control = boto3.client("bedrock-agentcore-control")
cloudformation = boto3.client("cloudformation")
ecs = boto3.client("ecs")
autoscaling = boto3.client("application-autoscaling")

_PHYSICAL_ID = "AxonLLMAgentCoreRecoveryGuard"
_BLOCKED_MODES = {"quiesced", "selected"}
_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)


def _pages(client, method_name, result_name, **arguments):
    results = []
    token = None
    while True:
        request = dict(arguments)
        if token:
            request["nextToken"] = token
        response = getattr(client, method_name)(**request)
        page = response.get(result_name, [])
        if not isinstance(page, list):
            raise RuntimeError(f"{method_name} returned malformed results")
        results.extend(page)
        token = response.get("nextToken")
        if not token:
            return results


def _runtime_id(runtime_name):
    matches = [
        runtime
        for runtime in _pages(
            control,
            "list_agent_runtimes",
            "agentRuntimes",
            maxResults=100,
        )
        if runtime.get("agentRuntimeName") == runtime_name
    ]
    if len(matches) != 1 or not matches[0].get("agentRuntimeId"):
        raise RuntimeError(
            "recovery guard could not resolve exactly one AgentCore runtime"
        )
    return matches[0]["agentRuntimeId"]


def _assert_no_runtime_endpoints(runtime_name):
    runtime_id = _runtime_id(runtime_name)
    endpoints = _pages(
        control,
        "list_agent_runtime_endpoints",
        "runtimeEndpoints",
        agentRuntimeId=runtime_id,
        maxResults=100,
    )
    if endpoints:
        summary = sorted(
            f"{item.get('name', 'unknown')}:{item.get('status', 'unknown')}"
            for item in endpoints
        )
        raise RuntimeError(
            "AgentCore recovery requires every runtime endpoint removed: "
            + ", ".join(summary)
        )


def _control_plane_outputs(stack_name):
    try:
        response = cloudformation.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if (
            error.get("Code") == "ValidationError"
            and "does not exist" in error.get("Message", "")
        ):
            return None
        raise
    stacks = response.get("Stacks", [])
    if len(stacks) != 1:
        raise RuntimeError(
            "recovery guard could not resolve the control-plane stack"
        )
    outputs = {
        item.get("OutputKey"): item.get("OutputValue")
        for item in stacks[0].get("Outputs", [])
    }
    required = {
        "AgentCoreStackName",
        "ClusterName",
        "PrimaryStateTableName",
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "SelectedRuntimeStateTableName",
        "ServiceName",
    }
    missing = sorted(
        name for name in required if name not in outputs
    )
    if missing:
        raise RuntimeError(
            "control-plane stack is missing recovery outputs: "
            + ", ".join(missing)
        )
    return outputs


def _assert_table_namespace(primary, selected):
    if selected == primary:
        return
    if not selected.startswith(f"{primary}-restore-validation-"):
        raise RuntimeError(
            "AgentCore recovery table is outside the restore-validation "
            "namespace"
        )


def _assert_control_plane_quiesced(stack_name):
    outputs = _control_plane_outputs(stack_name)
    if outputs is None:
        return None
    resource_id = (
        f"service/{outputs['ClusterName']}/{outputs['ServiceName']}"
    )
    targets = autoscaling.describe_scalable_targets(
        ServiceNamespace="ecs",
        ResourceIds=[resource_id],
        ScalableDimension="ecs:service:DesiredCount",
    ).get("ScalableTargets", [])
    if len(targets) != 1:
        raise RuntimeError(
            "recovery requires exactly one control-plane scalable target"
        )
    target = targets[0]
    suspended = target.get("SuspendedState", {})
    if target.get("MinCapacity") != 0 or not all(
        suspended.get(key) is True for key in _SUSPENSION_KEYS
    ):
        raise RuntimeError(
            "recovery requires the control plane at minimum capacity zero "
            "with every scaling path suspended"
        )
    response = ecs.describe_services(
        cluster=outputs["ClusterName"],
        services=[outputs["ServiceName"]],
    )
    if response.get("failures") or len(response.get("services", [])) != 1:
        raise RuntimeError(
            "recovery guard could not resolve the control-plane service"
        )
    service = response["services"][0]
    counts = {
        name: service.get(name)
        for name in ("desiredCount", "pendingCount", "runningCount")
    }
    if any(value != 0 for value in counts.values()):
        raise RuntimeError(
            "recovery requires a fully quiesced control plane: "
            f"{counts}"
        )
    return outputs


def _assert_control_plane_recovery_state(
    current,
    previous,
    transition,
    outputs,
):
    if outputs is None:
        return
    if (
        outputs["AgentCoreStackName"] != current["AgentCoreStackName"]
        or outputs["PrimaryStateTableName"] != current["PrimaryTable"]
    ):
        raise RuntimeError(
            "control-plane recovery ownership does not match AgentCore"
        )

    mode = current["Mode"]
    selected = current["SelectedTable"]
    approval = current["ApprovalId"]
    if transition == ("selected", "quiesced"):
        expected = (
            "selected",
            previous["SelectedTable"],
            previous["ApprovalId"],
        )
    elif transition == ("validation", "normal"):
        expected = (
            "selected",
            selected,
            approval,
        )
    elif transition == ("quiesced", "normal"):
        expected = (
            "quiesced",
            selected,
            previous["ApprovalId"],
        )
    else:
        expected_mode = {
            "normal": "normal",
            "quiesced": "quiesced",
            "selected": "selected",
            "validation": "selected",
        }[mode]
        expected = (expected_mode, selected, approval)
    actual = (
        outputs["RecoveryCutoverMode"],
        outputs["SelectedRuntimeStateTableName"],
        outputs["RecoveryApprovalId"],
    )
    if actual != expected:
        raise RuntimeError(
            "control-plane recovery state does not authorize this "
            f"AgentCore transition: expected {expected}, found {actual}"
        )


def _quiesced_epoch(physical_id):
    prefix = f"{_PHYSICAL_ID}:"
    if not physical_id.startswith(prefix):
        raise RuntimeError("recovery quiescence evidence is missing")
    try:
        value = int(physical_id[len(prefix):])
    except ValueError as exc:
        raise RuntimeError(
            "recovery quiescence evidence is malformed"
        ) from exc
    if value < 1:
        raise RuntimeError("recovery quiescence evidence is malformed")
    return value


def _result(physical_id, quiesced_at=None):
    if quiesced_at is None:
        timestamp = "not-quiesced"
    else:
        timestamp = datetime.fromtimestamp(
            quiesced_at,
            tz=timezone.utc,
        ).isoformat()
    return {
        "PhysicalResourceId": physical_id,
        "Data": {"QuiescedAt": timestamp},
    }


def handler(event, _context):
    if event["RequestType"] == "Delete":
        return _result(event.get("PhysicalResourceId", _PHYSICAL_ID))

    current = event["ResourceProperties"]
    mode = current.get("Mode")
    primary = current.get("PrimaryTable", "")
    target = current.get("SelectedTable", "")
    approval = current.get("ApprovalId", "")
    if not primary or not target:
        raise RuntimeError("AgentCore recovery table ownership is missing")
    _assert_table_namespace(primary, target)
    if event["RequestType"] == "Create":
        if mode != "normal" or target != primary or approval:
            raise RuntimeError(
                "a new AgentCore stack must start on the primary table "
                "in normal mode without a recovery approval"
            )
        return _result(_PHYSICAL_ID)

    previous = event.get("OldResourceProperties", {})
    for immutable in ("AgentCoreStackName", "PrimaryTable"):
        if current.get(immutable) != previous.get(immutable):
            raise RuntimeError(
                f"AgentCore recovery ownership changed: {immutable}"
            )
    old_mode = previous.get("Mode")
    old_target = previous.get("SelectedTable", "")
    old_approval = previous.get("ApprovalId", "")
    target_changed = target != old_target
    transition = (old_mode, mode)
    allowed = {
        ("normal", "normal"),
        ("normal", "quiesced"),
        ("quiesced", "quiesced"),
        ("quiesced", "normal"),
        ("quiesced", "selected"),
        ("selected", "selected"),
        ("selected", "quiesced"),
        ("selected", "validation"),
        ("validation", "validation"),
        ("validation", "normal"),
    }
    if transition not in allowed:
        raise RuntimeError(
            f"unsupported AgentCore recovery transition: "
            f"{old_mode} -> {mode}"
        )

    if target_changed and transition not in {
        ("quiesced", "selected"),
        ("selected", "quiesced"),
    }:
        raise RuntimeError(
            "AgentCore state table changes require a blocked "
            "quiesced -> selected transition"
        )
    if transition == ("quiesced", "selected") and not target_changed:
        raise RuntimeError(
            "selected mode requires an approved state table change"
        )
    if transition == ("selected", "validation") and target_changed:
        raise RuntimeError(
            "validation must use the table already fixed in selected mode"
        )

    if mode == "quiesced" and old_mode == "normal":
        if not approval or approval == old_approval:
            raise RuntimeError(
                "entering recovery requires a new non-empty approval ID"
            )
    elif mode in {"selected", "validation"}:
        if not approval or approval != old_approval:
            raise RuntimeError(
                "recovery approval ID changed during a protected transition"
            )

    must_be_quiesced = (
        mode in _BLOCKED_MODES
        or old_mode in _BLOCKED_MODES
        or old_mode == "validation"
    )
    if must_be_quiesced:
        control_plane = _assert_control_plane_quiesced(
            current["ControlPlaneStackName"]
        )
    else:
        control_plane = _control_plane_outputs(
            current["ControlPlaneStackName"]
        )
    _assert_control_plane_recovery_state(
        current,
        previous,
        transition,
        control_plane,
    )
    if mode in _BLOCKED_MODES:
        _assert_no_runtime_endpoints(current["RuntimeName"])

    physical_id = event.get("PhysicalResourceId", _PHYSICAL_ID)
    quiesced_at = None
    if mode == "quiesced" and old_mode == "normal":
        quiesced_at = int(time.time())
        physical_id = f"{_PHYSICAL_ID}:{quiesced_at}"
    elif old_mode in _BLOCKED_MODES:
        quiesced_at = _quiesced_epoch(physical_id)

    if transition == ("quiesced", "selected"):
        minimum = int(current["MinimumQuiescenceSeconds"])
        elapsed = int(time.time()) - quiesced_at
        if elapsed < minimum:
            raise RuntimeError(
                "AgentCore recovery quiescence is too recent: "
                f"{elapsed}s elapsed, {minimum}s required"
            )

    if mode == "normal":
        physical_id = _PHYSICAL_ID
        quiesced_at = None
    return _result(physical_id, quiesced_at)
"""


class AxonLLMAgentCoreStack(Stack):
    """Contained AgentCore runtime with tenant-safe identity and state."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bootstrap_qualifier: str = "axprod",
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        removal_policy = RemovalPolicy.DESTROY if deployment_namespace else RemovalPolicy.RETAIN
        deletion_protection = not bool(deployment_namespace)
        runtime_ingress = self.node.try_get_context("runtime_ingress")
        if runtime_ingress is None:
            runtime_ingress = "direct-jwt" if deployment_namespace else "facade"
        if runtime_ingress not in {"direct-jwt", "facade"}:
            raise ValueError("runtime_ingress must be 'direct-jwt' or 'facade'")
        runtime_suffix = deployment_namespace.replace("-", "_")
        runtime_name = f"axonllm_{runtime_suffix}" if runtime_suffix else "axonllm"
        control_plane_stack_name = f"AxonLLMControlPlaneStack{physical_suffix}"
        rehearsal_control_table_arn = (
            CfnParameter(
                self,
                "RehearsalControlTableArn",
                type="String",
                allowed_pattern=(
                    rf"^arn:aws:dynamodb:{re.escape(self.region)}:"
                    rf"{('[0-9]{12}' if Token.is_unresolved(self.account) else re.escape(self.account))}:"
                    r"table/axonllm-rehearsal-control-ledger$"
                ),
                constraint_description=(
                    "must be the retained rehearsal-control ledger ARN in this stack's AWS region and account"
                ),
                description=(
                    "Exact retained rehearsal-control ledger ARN used only by an isolated qualification runtime"
                ),
            )
            if deployment_namespace
            else None
        )

        oidc_issuer = CfnParameter(
            self,
            "OidcIssuer",
            type="String",
            min_length=1,
            allowed_pattern=r"^https://[^?#\s]+$",
            description="Exact issuer accepted by AxonLLM for OIDC bearer tokens",
        )
        oidc_discovery_url = CfnParameter(
            self,
            "OidcDiscoveryUrl",
            type="String",
            min_length=1,
            allowed_pattern=(r"^https://[^?#\s]+/\.well-known/openid-configuration$"),
            description="OIDC discovery URL used by the AgentCore JWT authorizer",
        )
        oidc_client_ids = CfnParameter(
            self,
            "OidcClientIds",
            type="CommaDelimitedList",
            allowed_pattern=r"^[^\s,]+$",
            description="OIDC client IDs allowed to invoke the runtime",
        )
        oidc_audiences = CfnParameter(
            self,
            "OidcAudiences",
            type="CommaDelimitedList",
            allowed_pattern=r"^[^\s,]+$",
            description="OIDC audiences allowed to invoke the runtime",
        )
        oidc_tenant_claim = CfnParameter(
            self,
            "OidcTenantClaim",
            type="String",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^\S+$",
            description=("Signed OIDC claim containing the AxonLLM tenant hint"),
        )
        oidc_project_claim = CfnParameter(
            self,
            "OidcProjectClaim",
            type="String",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^\S+$",
            description=("Signed OIDC claim containing the AxonLLM project hint"),
        )
        deployment_experience = CfnParameter(
            self,
            "DeploymentExperience",
            type="String",
            allowed_values=["axonllm", "ostiari"],
            description=("Product experience that owns identity, administration, and the human-facing control plane"),
        )
        approved_https_prefix_list_id = CfnParameter(
            self,
            "ApprovedHttpsPrefixListId",
            type="String",
            allowed_pattern=r"^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=(
                "Managed prefix list containing approved OIDC and provider "
                "HTTPS destinations, including the regional Bedrock Mantle "
                "API endpoint and every configured HTTP provider"
            ),
        )
        bedrock_invoke_resource_arns = CfnParameter(
            self,
            "BedrockInvokeResourceArns",
            type="CommaDelimitedList",
            allowed_pattern=(
                r"^arn:[a-z0-9-]+:bedrock:[a-z0-9-]+:"
                r"(?:[0-9]{12})?:(?:foundation-model|inference-profile|"
                r"application-inference-profile|custom-model|provisioned-model|"
                r"imported-model)/[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
            ),
            constraint_description=(
                "each value must be a concrete Bedrock model or inference-profile ARN without wildcards"
            ),
            description=(
                "Comma-separated Bedrock model or inference-profile ARNs "
                "that AxonLLM may invoke. Cross-region inference profiles "
                "must include every concrete destination foundation-model ARN."
            ),
        )
        verified_image_uri = CfnParameter(
            self,
            "VerifiedImageUri",
            type="String",
            allowed_pattern=(
                rf"^[0-9]{{12}}\.dkr\.ecr\.{self.region}\.amazonaws\.com/"
                r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@"
                r"sha256:[0-9a-f]{64}$"
            ),
            constraint_description=(
                f"must be an immutable private ECR URI in {self.region} ending in @sha256:<64 lowercase hex characters>"
            ),
            description=("Immutable ARM64 AgentCore image emitted by the release deployment verification gate"),
        )
        enabled_providers = CfnParameter(
            self,
            "EnabledProviders",
            type="String",
            default=_AGENTCORE_ENABLED_PROVIDERS,
            min_length=1,
            max_length=512,
            allowed_pattern=_PROVIDER_NAME_PATTERN,
            constraint_description=("must be a comma-separated list of supported provider names"),
            description=("Exact provider allowlist certified for this runtime version"),
        )
        provider_secret_version = CfnParameter(
            self,
            "ProviderSecretVersion",
            type="String",
            default="bootstrap",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^[A-Za-z0-9-]+$",
            constraint_description=("must be a Secrets Manager version identifier or bootstrap"),
            description=(
                "Provider secret version bound into this AgentCore runtime "
                "revision; changing it forces a fresh runtime version"
            ),
        )
        alarm_notification_email = CfnParameter(
            self,
            "AlarmNotificationEmail",
            type="String",
            min_length=3,
            max_length=320,
            allowed_pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            constraint_description="must be a valid notification email",
            description=("Required confirmed destination for production alarms"),
        )
        candidate_endpoint_name = CfnParameter(
            self,
            "CandidateEndpointName",
            type="String",
            min_length=42,
            max_length=42,
            allowed_pattern=r"^candidate_[0-9a-f]{32}$",
            constraint_description=("must be a fresh high-entropy candidate endpoint name"),
            description=("Unpredictable endpoint qualifier generated for this certification deployment"),
        )
        publish_candidate_endpoint = CfnParameter(
            self,
            "PublishCandidateEndpoint",
            type="String",
            default="true",
            allowed_values=["true", "false"],
            description=(
                "Publish the candidate endpoint for the current runtime "
                "version after provider credentials have been synchronized"
            ),
        )
        publish_production_endpoint = CfnParameter(
            self,
            "PublishProductionEndpoint",
            type="String",
            default="false",
            allowed_values=["true", "false"],
            description=("Publish the production endpoint only for an explicitly certified runtime version"),
        )
        production_runtime_version = CfnParameter(
            self,
            "ProductionRuntimeVersion",
            type="String",
            default="",
            max_length=32,
            allowed_pattern=r"^$|^[1-9][0-9]{0,31}$",
            constraint_description=("must be empty or an exact positive AgentCore runtime version"),
            description=("Exact certified runtime version targeted by production"),
        )
        image_account_id = Fn.select(
            0,
            Fn.split(".", verified_image_uri.value_as_string),
        )
        image_repository_name = Fn.select(
            0,
            Fn.split(
                "@",
                Fn.select(
                    1,
                    Fn.split(
                        ".amazonaws.com/",
                        verified_image_uri.value_as_string,
                    ),
                ),
            ),
        )
        verified_image_repository_arn = self.format_arn(
            service="ecr",
            region=self.region,
            account=image_account_id,
            resource="repository",
            resource_name=image_repository_name,
        )

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=2,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                    map_public_ip_on_launch=False,
                ),
                ec2.SubnetConfiguration(
                    name="Runtime",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )
        runtime_security_group = ec2.SecurityGroup(
            self,
            "RuntimeSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM AgentCore explicitly approved egress",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.udp(53),
            "DNS to the VPC resolver",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(53),
            "DNS fallback to the VPC resolver",
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.prefix_list(approved_https_prefix_list_id.value_as_string),
            ec2.Port.tcp(443),
            "HTTPS to explicitly approved external destinations",
        )

        dynamodb_endpoint = vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
        )
        prefix_lookup_logs = logs.LogGroup(
            self,
            "PrefixLookupLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        dynamodb_prefix_list = cr.AwsCustomResource(
            self,
            "DynamoDbPrefixList",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="describeManagedPrefixLists",
                parameters={
                    "Filters": [
                        {
                            "Name": "prefix-list-name",
                            "Values": [f"com.amazonaws.{self.region}.dynamodb"],
                        }
                    ]
                },
                output_paths=["PrefixLists.0.PrefixListId"],
                physical_resource_id=cr.PhysicalResourceId.from_response("PrefixLists.0.PrefixListId"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["ec2:DescribeManagedPrefixLists"],
                        resources=["*"],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
            log_group=prefix_lookup_logs,
            timeout=Duration.seconds(30),
        )
        runtime_security_group.add_egress_rule(
            ec2.Peer.prefix_list(dynamodb_prefix_list.get_response_field("PrefixLists.0.PrefixListId")),
            ec2.Port.tcp(443),
            "DynamoDB through the VPC gateway endpoint",
        )

        endpoint_security_group = ec2.SecurityGroup(
            self,
            "EndpointSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="Private AWS service endpoints for AgentCore",
        )
        endpoint_security_group.add_ingress_rule(
            runtime_security_group,
            ec2.Port.tcp(443),
            "HTTPS from the AgentCore runtime",
        )
        runtime_security_group.add_egress_rule(
            endpoint_security_group,
            ec2.Port.tcp(443),
            "AWS services through private interface endpoints",
        )
        bedrock_endpoint = vpc.add_interface_endpoint(
            "BedrockRuntimeEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        sns_managed_key = kms.Alias.from_alias_name(
            self,
            "SnsManagedKey",
            "alias/aws/sns",
        )
        provider_secret = secretsmanager.Secret(
            self,
            "ProviderCredentials",
            description=("AxonLLM AgentCore HTTP-provider credentials and endpoints"),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {field_name: "" for field_name in _PROVIDER_SECRET_FIELDS},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                generate_string_key="placeholder",
            ),
            removal_policy=removal_policy,
        )
        secrets_endpoint = vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        secrets_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[provider_secret.secret_arn],
            )
        )
        state_table_name = (
            self.node.try_get_context("agentcore_table_name") or f"axonllm-agentcore-state{physical_suffix}"
        )
        restore_table_marker = "-restore-validation-"
        restore_table_suffix_limit = 255 - len(state_table_name) - len(restore_table_marker)
        if restore_table_suffix_limit < 21:
            raise ValueError(
                "AgentCore state table name must be at most 214 characters to preserve the PITR validation suffix"
            )
        restored_state_table_pattern = (
            rf"^$|^{re.escape(state_table_name)}"
            rf"{restore_table_marker}[A-Za-z0-9_.-]"
            rf"{{1,{restore_table_suffix_limit}}}$"
        )
        runtime_state_table_name = CfnParameter(
            self,
            "RuntimeStateTableName",
            type="String",
            default="",
            allowed_pattern=restored_state_table_pattern,
            constraint_description=(
                f"must be blank or a PITR validation table derived from the {state_table_name} primary table"
            ),
            description=("Optional restored state table selected through the reviewed AgentCore recovery workflow"),
        )
        recovery_cutover_mode = CfnParameter(
            self,
            "RecoveryCutoverMode",
            type="String",
            default="normal",
            allowed_values=[
                "normal",
                "quiesced",
                "selected",
                "validation",
            ],
            description=("AgentCore recovery phase; table changes are accepted only from quiesced to selected"),
        )
        recovery_approval_id = CfnParameter(
            self,
            "RecoveryApprovalId",
            type="String",
            default="",
            max_length=128,
            allowed_pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$",
            constraint_description=("must be blank or a 3-128 character change/incident ID"),
            description=("Reviewed change or incident identifier bound to a recovery cutover and rollback"),
        )
        use_recovered_state = CfnCondition(
            self,
            "UseRecoveredState",
            expression=Fn.condition_not(
                Fn.condition_equals(
                    runtime_state_table_name.value_as_string,
                    "",
                )
            ),
        )
        production_endpoint_enabled = CfnCondition(
            self,
            "ProductionEndpointEnabled",
            expression=Fn.condition_and(
                Fn.condition_equals(
                    recovery_cutover_mode.value_as_string,
                    "normal",
                ),
                Fn.condition_equals(
                    publish_production_endpoint.value_as_string,
                    "true",
                ),
                Fn.condition_not(
                    Fn.condition_equals(
                        production_runtime_version.value_as_string,
                        "",
                    )
                ),
            ),
        )
        candidate_endpoint_enabled = CfnCondition(
            self,
            "CandidateEndpointEnabled",
            expression=Fn.condition_and(
                Fn.condition_equals(
                    recovery_cutover_mode.value_as_string,
                    "normal",
                ),
                Fn.condition_equals(
                    publish_candidate_endpoint.value_as_string,
                    "true",
                ),
            ),
        )
        recovery_quiesced = CfnCondition(
            self,
            "RecoveryQuiesced",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "quiesced",
            ),
        )
        recovery_selected = CfnCondition(
            self,
            "RecoverySelected",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "selected",
            ),
        )
        recovery_validation = CfnCondition(
            self,
            "RecoveryValidation",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "validation",
            ),
        )
        recovery_access_blocked = CfnCondition(
            self,
            "RecoveryAccessBlocked",
            expression=Fn.condition_or(
                recovery_quiesced,
                recovery_selected,
            ),
        )
        state_table = dynamodb.Table(
            self,
            "StateTable",
            table_name=state_table_name,
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            deletion_protection=deletion_protection,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)
            ),
            time_to_live_attribute="expires_at",
            removal_policy=removal_policy,
        )
        selected_state_table_name = Token.as_string(
            Fn.condition_if(
                use_recovered_state.logical_id,
                runtime_state_table_name.value_as_string,
                state_table.table_name,
            )
        )
        selected_state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name,
        )
        event_dead_letter_queue = sqs.Queue(
            self,
            "SecurityEventDeadLetterQueue",
            fifo=True,
            content_based_deduplication=False,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            removal_policy=removal_policy,
        )
        event_outbox_queue = sqs.Queue(
            self,
            "SecurityEventOutboxQueue",
            fifo=True,
            content_based_deduplication=False,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            receive_message_wait_time=Duration.seconds(20),
            visibility_timeout=Duration.minutes(2),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=event_dead_letter_queue,
            ),
            removal_policy=removal_policy,
        )
        security_event_topic = sns.Topic(
            self,
            "SecurityEventTopic",
            display_name="AxonLLM AgentCore durable security events",
            fifo=True,
            content_based_deduplication=False,
            enforce_ssl=True,
            master_key=sns_managed_key,
        )
        security_event_log_group = logs.LogGroup(
            self,
            "SecurityEventLogGroup",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        security_event_log_stream = logs.LogStream(
            self,
            "SecurityEventLogStream",
            log_group=security_event_log_group,
            log_stream_name="events",
        )
        security_event_log_stream.apply_removal_policy(removal_policy)
        sqs_endpoint = vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        sqs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "sqs:ChangeMessageVisibility",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:ReceiveMessage",
                    "sqs:SendMessage",
                ],
                resources=[event_outbox_queue.queue_arn],
            )
        )
        sns_endpoint = vpc.add_interface_endpoint(
            "SnsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SNS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        sns_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        logs_endpoint = vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        logs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    security_event_log_group.log_group_arn,
                    f"{security_event_log_group.log_group_arn}:*",
                ],
            )
        )
        dynamodb_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "dynamodb:BatchGetItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:ConditionCheckItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:TransactWriteItems",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        bedrock_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_invoke_resource_arns.value_as_list,
            )
        )

        application_logs = logs.LogGroup(
            self,
            "ApplicationLogs",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        usage_logs = logs.LogGroup(
            self,
            "UsageLogs",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )

        runtime_execution_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name=Fn.join(
                "-",
                [
                    "axonllm-agentcore-runtime",
                    *([deployment_namespace] if deployment_namespace else []),
                    self.region,
                ],
            ),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:bedrock-agentcore:"
                            f"{self.region}:{self.account}:"
                            f"runtime/{runtime_name}*"
                        )
                    },
                },
            ),
            description="Execution role for Bedrock Agent Core Runtime",
            max_session_duration=Duration.hours(8),
        )
        runtime_artifact = agentcore.AgentRuntimeArtifact.from_image_uri(verified_image_uri.value_as_string)
        blocked_authorizer_value = Fn.join(
            ":",
            [
                "axonllm-recovery-blocked",
                Fn.select(2, Fn.split("/", self.stack_id)),
            ],
        )
        selected_oidc_client_ids = Token.as_list(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                [blocked_authorizer_value],
                oidc_client_ids.value_as_list,
            )
        )
        selected_oidc_audiences = Token.as_list(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                [blocked_authorizer_value],
                oidc_audiences.value_as_list,
            )
        )
        runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=runtime_name,
            description="Tenant-isolated AxonLLM production runtime",
            agent_runtime_artifact=runtime_artifact,
            execution_role=runtime_execution_role,
            authorizer_configuration=(
                agentcore.RuntimeAuthorizerConfiguration.using_iam()
                if runtime_ingress == "facade"
                else agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                    oidc_discovery_url.value_as_string,
                    selected_oidc_client_ids,
                    selected_oidc_audiences,
                )
            ),
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=([_FACADE_IDENTITY_HEADER] if runtime_ingress == "facade" else ["Authorization"])
            ),
            environment_variables={
                "AWS_DEFAULT_REGION": self.region,
                "AXON_AWS_ACCOUNT_ID": self.account,
                "AXON_BEDROCK_REGION": self.region,
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                "AXON_DYNAMODB_TABLE": selected_state_table_name,
                "AXON_EVENT_OUTBOX_QUEUE_URL": event_outbox_queue.queue_url,
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (security_event_topic.topic_arn),
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (security_event_log_group.log_group_arn),
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_EXPERIENCE_OWNER": (deployment_experience.value_as_string),
                "AXON_EXECUTION_TARGET": "agentcore",
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_OIDC_ISSUER": oidc_issuer.value_as_string,
                "AXON_OIDC_AUDIENCE": Fn.join(
                    ",",
                    oidc_audiences.value_as_list,
                ),
                "AXON_OIDC_TENANT_CLAIM": (oidc_tenant_claim.value_as_string),
                "AXON_OIDC_PROJECT_CLAIM": (oidc_project_claim.value_as_string),
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_AGENTCORE_FACADE_IDENTITY_ALLOWED": ("true" if runtime_ingress == "facade" else "false"),
                "AXON_ENABLED_PROVIDERS": (enabled_providers.value_as_string),
                "AXON_PROVIDER_SECRET_ARN": provider_secret.secret_arn,
                "AXON_PROVIDER_SECRET_VERSION": (provider_secret_version.value_as_string),
                **(
                    {
                        "AXON_LAUNCH_REHEARSAL_TABLE": (rehearsal_control_table_arn.value_as_string),
                        "AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT": "true",
                    }
                    if rehearsal_control_table_arn is not None
                    else {}
                ),
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(10),
                max_lifetime=Duration.hours(4),
            ),
            logging_configs=[
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.APPLICATION_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(application_logs),
                ),
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.USAGE_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(usage_logs),
                ),
            ],
            network_configuration=(
                agentcore.RuntimeNetworkConfiguration.using_vpc(
                    self,
                    vpc=vpc,
                    security_groups=[runtime_security_group],
                    vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                )
            ),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            tracing_enabled=True,
            tags={
                "Application": "AxonLLM",
                "Environment": "production",
            },
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[verified_image_repository_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSelectedStateTable",
                actions=_RUNTIME_DYNAMODB_STANDARD_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        if rehearsal_control_table_arn is not None:
            runtime.add_to_role_policy(
                iam.PolicyStatement(
                    sid="UseLaunchRehearsalControlLedger",
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                    ],
                    resources=[rehearsal_control_table_arn.value_as_string],
                )
            )
            dynamodb_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[runtime.role],
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                    ],
                    resources=[rehearsal_control_table_arn.value_as_string],
                )
            )
        transaction_policy = iam.Policy(
            self,
            "RuntimeDynamoTransactionPolicy",
            statements=[
                iam.PolicyStatement(
                    sid="TransactWithSelectedStateTable",
                    actions=_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[
                        selected_state_table_arn,
                        f"{selected_state_table_arn}/index/*",
                    ],
                )
            ],
        )
        transaction_policy.attach_to_role(runtime.role)
        cfn_transaction_policy = transaction_policy.node.default_child
        if not isinstance(cfn_transaction_policy, iam.CfnPolicy):
            raise TypeError("runtime transaction policy has no CfnPolicy child")
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_transaction_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadProviderCredentials",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[provider_secret.secret_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventOutbox",
                actions=_RUNTIME_SQS_ACTIONS,
                resources=[event_outbox_queue.queue_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishSecurityEvents",
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventTopicKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sns.{self.region}.{self.url_suffix}"),
                        "kms:EncryptionContext:aws:sns:topicArn": (security_event_topic.topic_arn),
                    }
                },
            )
        )
        security_event_log_group.grant_write(runtime.role)
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_invoke_resource_arns.value_as_list,
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-mantle:CreateInference",
                    "bedrock-mantle:ListModels",
                ],
                resources=["*"],
            )
        )
        recovery_deny_resource = Token.as_string(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                "*",
                self.format_arn(
                    service="dynamodb",
                    resource="table",
                    resource_name=("__axonllm_recovery_access_not_blocked__"),
                ),
            )
        )
        recovery_deny_policy = iam.Policy(
            self,
            "RecoveryStateAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateAccessDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_RUNTIME_DYNAMODB_STANDARD_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_deny_policy.attach_to_role(runtime.role)
        cfn_recovery_deny_policy = recovery_deny_policy.node.default_child
        if not isinstance(cfn_recovery_deny_policy, iam.CfnPolicy):
            raise TypeError("recovery deny policy has no CfnPolicy child")
        recovery_transaction_deny_policy = iam.Policy(
            self,
            "RecoveryStateTransactionAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateTransactionsDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_transaction_deny_policy.attach_to_role(runtime.role)
        cfn_recovery_transaction_deny_policy = recovery_transaction_deny_policy.node.default_child
        if not isinstance(
            cfn_recovery_transaction_deny_policy,
            iam.CfnPolicy,
        ):
            raise TypeError("recovery transaction deny policy has no CfnPolicy child")
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_recovery_transaction_deny_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )

        recovery_guard_handler_logs = logs.LogGroup(
            self,
            "RecoveryGuardHandlerLogs",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        recovery_guard_handler = lambda_.Function(
            self,
            "RecoveryGuardHandler",
            description=("Blocks unsafe AgentCore DynamoDB recovery transitions"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_AGENTCORE_RECOVERY_GUARD),
            timeout=Duration.seconds(60),
            log_group=recovery_guard_handler_logs,
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:ListAgentRuntimeEndpoints",
                    "bedrock-agentcore:ListAgentRuntimes",
                ],
                resources=["*"],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=(f"{control_plane_stack_name}/*"),
                    )
                ],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=[
                    self.format_arn(
                        service="ecs",
                        resource="service",
                        resource_name="*/*",
                    )
                ],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["application-autoscaling:DescribeScalableTargets"],
                resources=["*"],
            )
        )
        recovery_guard_provider_logs = logs.LogGroup(
            self,
            "RecoveryGuardProviderLogs",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        recovery_guard_provider = cr.Provider(
            self,
            "RecoveryGuardProvider",
            on_event_handler=recovery_guard_handler,
            log_group=recovery_guard_provider_logs,
        )
        recovery_guard = CustomResource(
            self,
            "RecoveryGuard",
            service_token=recovery_guard_provider.service_token,
            properties={
                "AgentCoreStackName": self.stack_name,
                "ApprovalId": recovery_approval_id.value_as_string,
                "ControlPlaneStackName": control_plane_stack_name,
                "MinimumQuiescenceSeconds": str(_RECOVERY_MIN_QUIESCENCE_SECONDS),
                "Mode": recovery_cutover_mode.value_as_string,
                "PrimaryTable": state_table.table_name,
                "RuntimeName": runtime_name,
                "SelectedTable": selected_state_table_name,
            },
        )
        recovery_guard_resource = recovery_guard.node.default_child
        if not isinstance(recovery_guard_resource, CfnResource):
            raise TypeError("recovery guard has no CloudFormation child")
        recovery_guard_resource.add_dependency(cfn_recovery_deny_policy)
        recovery_guard_resource.add_dependency(cfn_recovery_transaction_deny_policy)
        cfn_runtime = runtime.node.default_child
        if not isinstance(cfn_runtime, agentcore.CfnRuntime):
            raise TypeError("AgentCore runtime has no CfnRuntime child")
        cfn_runtime.add_dependency(recovery_guard_resource)

        production_endpoint = runtime.add_endpoint(
            "production",
            description="AxonLLM production endpoint",
            version=production_runtime_version.value_as_string,
        )
        candidate_endpoint = agentcore.RuntimeEndpoint(
            self,
            "CandidateRuntimeEndpoint",
            agent_runtime_id=runtime.agent_runtime_id,
            endpoint_name=candidate_endpoint_name.value_as_string,
            description="AxonLLM pre-production certification endpoint",
            agent_runtime_version=runtime.agent_runtime_version,
        )
        recovery_endpoint = runtime.add_endpoint(
            "recovery",
            description="AxonLLM recovery-validation endpoint",
            version=runtime.agent_runtime_version,
        )
        cfn_production_endpoint = production_endpoint.node.default_child
        cfn_candidate_endpoint = candidate_endpoint.node.default_child
        cfn_recovery_endpoint = recovery_endpoint.node.default_child
        if (
            not isinstance(
                cfn_production_endpoint,
                agentcore.CfnRuntimeEndpoint,
            )
            or not isinstance(
                cfn_candidate_endpoint,
                agentcore.CfnRuntimeEndpoint,
            )
            or not isinstance(
                cfn_recovery_endpoint,
                agentcore.CfnRuntimeEndpoint,
            )
        ):
            raise TypeError("AgentCore endpoint has no CfnRuntimeEndpoint child")
        cfn_production_endpoint.cfn_options.condition = production_endpoint_enabled
        cfn_candidate_endpoint.cfn_options.condition = candidate_endpoint_enabled
        cfn_recovery_endpoint.cfn_options.condition = recovery_validation
        cfn_production_endpoint.add_dependency(recovery_guard_resource)
        cfn_candidate_endpoint.add_dependency(recovery_guard_resource)
        cfn_recovery_endpoint.add_dependency(recovery_guard_resource)

        alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="AxonLLM AgentCore production alarms",
        )
        alarm_topic.add_subscription(
            sns_subscriptions.EmailSubscription(
                alarm_notification_email.value_as_string,
            )
        )
        alarm_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowAccountCloudWatchAlarms",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[alarm_topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": (f"arn:{self.partition}:cloudwatch:{self.region}:{self.account}:alarm:*")
                    },
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )
        system_errors = runtime.metric_system_errors(
            period=Duration.minutes(5),
            statistic="Sum",
        )
        throttles = runtime.metric_throttles(
            period=Duration.minutes(5),
            statistic="Sum",
        )
        latency = runtime.metric_latency(
            period=Duration.minutes(5),
            statistic="p95",
        )
        state_operations = [
            "GetItem",
            "Query",
            "Scan",
            "PutItem",
            "UpdateItem",
            "DeleteItem",
            "TransactGetItems",
            "TransactWriteItems",
        ]
        throttle_metrics = {
            operation.lower(): cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="ThrottledRequests",
                dimensions_map={
                    "Operation": operation,
                    "TableName": selected_state_table_name,
                },
                period=Duration.minutes(5),
                statistic="Sum",
            )
            for operation in state_operations
        }
        dynamodb_throttles = cloudwatch.MathExpression(
            expression=" + ".join(throttle_metrics),
            using_metrics=throttle_metrics,
            period=Duration.minutes(5),
            label="Sum of throttled requests across all operations",
        )
        alarms = [
            cloudwatch.Alarm(
                self,
                "RuntimeSystemErrorsAlarm",
                metric=system_errors,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AgentCore reported AxonLLM system errors",
            ),
            cloudwatch.Alarm(
                self,
                "RuntimeThrottlesAlarm",
                metric=throttles,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AgentCore throttled AxonLLM invocations",
            ),
            cloudwatch.Alarm(
                self,
                "DynamoDbThrottlesAlarm",
                metric=dynamodb_throttles,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AgentCore state requests were throttled",
            ),
            cloudwatch.Alarm(
                self,
                "SecurityEventDeadLettersAlarm",
                metric=(
                    event_dead_letter_queue.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(1),
                        statistic="Maximum",
                    )
                ),
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=(cloudwatch.TreatMissingData.NOT_BREACHING),
                alarm_description=("A security event exhausted delivery retries"),
            ),
        ]
        security_event_dead_letters_alarm = alarms[-1]
        alarm_action = cloudwatch_actions.SnsAction(alarm_topic)
        for alarm in alarms:
            alarm.add_alarm_action(alarm_action)
            alarm.add_ok_action(alarm_action)

        dashboard = cloudwatch.Dashboard(
            self,
            "OperationsDashboard",
            dashboard_name=(f"AxonLLM-AgentCore-Production{physical_suffix}"),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="AgentCore invocations",
                left=[
                    runtime.metric_invocations(
                        period=Duration.minutes(5),
                        statistic="Sum",
                    )
                ],
                right=[system_errors, throttles],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="AgentCore latency",
                left=[latency],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="DynamoDB throttles",
                left=[dynamodb_throttles],
                width=12,
            ),
        )

        CfnOutput(
            self,
            "RuntimeArn",
            value=runtime.agent_runtime_arn,
            export_name=Fn.join(
                ":",
                [self.stack_name, "RuntimeArn"],
            ),
        )
        CfnOutput(
            self,
            "RuntimeVersion",
            value=runtime.agent_runtime_version,
        )
        runtime_endpoint_name_output = CfnOutput(
            self,
            "RuntimeEndpointName",
            value="production",
        )
        runtime_endpoint_name_output.condition = production_endpoint_enabled
        candidate_endpoint_name_output = CfnOutput(
            self,
            "CandidateRuntimeEndpointName",
            value=candidate_endpoint_name.value_as_string,
        )
        candidate_endpoint_name_output.condition = candidate_endpoint_enabled
        enabled_providers_output = CfnOutput(
            self,
            "EnabledProvidersOutput",
            value=enabled_providers.value_as_string,
        )
        enabled_providers_output.override_logical_id("EnabledProviders")
        provider_secret_version_output = CfnOutput(
            self,
            "ProviderSecretVersionOutput",
            value=provider_secret_version.value_as_string,
        )
        provider_secret_version_output.override_logical_id("ProviderSecretVersion")
        approved_https_prefix_list_output = CfnOutput(
            self,
            "ApprovedHttpsPrefixListIdOutput",
            value=approved_https_prefix_list_id.value_as_string,
        )
        approved_https_prefix_list_output.override_logical_id("ApprovedHttpsPrefixListId")
        bedrock_invoke_resource_arns_output = CfnOutput(
            self,
            "BedrockInvokeResourceArnsOutput",
            value=Fn.join(
                ",",
                bedrock_invoke_resource_arns.value_as_list,
            ),
        )
        bedrock_invoke_resource_arns_output.override_logical_id("BedrockInvokeResourceArns")
        deployment_experience_output = CfnOutput(
            self,
            "DeploymentExperienceOutput",
            value=deployment_experience.value_as_string,
        )
        deployment_experience_output.override_logical_id("DeploymentExperience")
        CfnOutput(
            self,
            "DeploymentExecution",
            value="agentcore",
        )
        CfnOutput(
            self,
            "RuntimeIngressMode",
            value=runtime_ingress,
        )
        alarm_notification_email_output = CfnOutput(
            self,
            "AlarmNotificationEmailOutput",
            value=alarm_notification_email.value_as_string,
        )
        alarm_notification_email_output.override_logical_id("AlarmNotificationEmail")
        CfnOutput(
            self,
            "RuntimeExecutionRoleArn",
            value=runtime.role.role_arn,
            description="Execution role assumed by the AgentCore runtime",
        )
        runtime_endpoint_output = CfnOutput(
            self,
            "RuntimeEndpointArn",
            value=production_endpoint.agent_runtime_endpoint_arn,
        )
        runtime_endpoint_output.condition = production_endpoint_enabled
        production_runtime_version_output = CfnOutput(
            self,
            "ProductionRuntimeVersionOutput",
            value=production_runtime_version.value_as_string,
        )
        production_runtime_version_output.override_logical_id("ProductionRuntimeVersion")
        production_runtime_version_output.condition = production_endpoint_enabled
        candidate_runtime_version_output = CfnOutput(
            self,
            "CandidateRuntimeVersion",
            value=runtime.agent_runtime_version,
        )
        candidate_runtime_version_output.condition = candidate_endpoint_enabled
        candidate_endpoint_output = CfnOutput(
            self,
            "CandidateRuntimeEndpointArn",
            value=candidate_endpoint.agent_runtime_endpoint_arn,
        )
        candidate_endpoint_output.condition = candidate_endpoint_enabled
        recovery_endpoint_output = CfnOutput(
            self,
            "RecoveryRuntimeEndpointArn",
            value=recovery_endpoint.agent_runtime_endpoint_arn,
        )
        recovery_endpoint_output.condition = recovery_validation
        CfnOutput(
            self,
            "StateTableName",
            value=state_table.table_name,
            export_name=Fn.join(
                ":",
                [self.stack_name, "StateTableName"],
            ),
        )
        CfnOutput(
            self,
            "AgentCoreStackName",
            value=self.stack_name,
        )
        CfnOutput(
            self,
            "SelectedRuntimeStateTableName",
            value=selected_state_table_name,
        )
        recovery_mode_output = CfnOutput(
            self,
            "RecoveryCutoverModeOutput",
            value=recovery_cutover_mode.value_as_string,
        )
        recovery_mode_output.override_logical_id("RecoveryCutoverMode")
        recovery_approval_output = CfnOutput(
            self,
            "RecoveryApprovalIdOutput",
            value=recovery_approval_id.value_as_string,
        )
        recovery_approval_output.override_logical_id("RecoveryApprovalId")
        CfnOutput(
            self,
            "RecoveryQuiescedAt",
            value=recovery_guard.get_att_string("QuiescedAt"),
        )
        CfnOutput(
            self,
            "RecoveryMinimumQuiescenceSeconds",
            value=str(_RECOVERY_MIN_QUIESCENCE_SECONDS),
        )
        CfnOutput(
            self,
            "SecurityEventOutboxQueueUrl",
            value=event_outbox_queue.queue_url,
            export_name=Fn.join(
                ":",
                [self.stack_name, "SecurityEventOutboxQueueUrl"],
            ),
        )
        CfnOutput(
            self,
            "SecurityEventOutboxQueueArn",
            value=event_outbox_queue.queue_arn,
            export_name=Fn.join(
                ":",
                [self.stack_name, "SecurityEventOutboxQueueArn"],
            ),
        )
        CfnOutput(
            self,
            "SecurityEventDeadLetterQueueUrl",
            value=event_dead_letter_queue.queue_url,
        )
        CfnOutput(
            self,
            "SecurityEventDeadLetterQueueArn",
            value=event_dead_letter_queue.queue_arn,
        )
        CfnOutput(
            self,
            "SecurityEventDeadLettersAlarmArn",
            value=security_event_dead_letters_alarm.alarm_arn,
        )
        CfnOutput(
            self,
            "SecurityEventTopicArn",
            value=security_event_topic.topic_arn,
            export_name=Fn.join(
                ":",
                [self.stack_name, "SecurityEventTopicArn"],
            ),
        )
        CfnOutput(
            self,
            "SecurityEventLogGroupArn",
            value=security_event_log_group.log_group_arn,
            export_name=Fn.join(
                ":",
                [self.stack_name, "SecurityEventLogGroupArn"],
            ),
        )
        CfnOutput(
            self,
            "ProviderSecretArn",
            value=provider_secret.secret_arn,
        )
        CfnOutput(
            self,
            "AlarmTopicArn",
            value=alarm_topic.topic_arn,
        )
        CfnOutput(
            self,
            "OperationsDashboardName",
            value=dashboard.dashboard_name,
        )
        CfnOutput(
            self,
            "RuntimeImageUri",
            value=verified_image_uri.value_as_string,
        )
