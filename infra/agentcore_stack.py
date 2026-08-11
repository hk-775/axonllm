"""Production Bedrock AgentCore deployment for the AxonLLM agent entrypoint."""

import json
import math
import re
from dataclasses import dataclass

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_backup as backup,
    aws_bedrockagentcore as agentcore,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    custom_resources as cr,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
    aws_sns as sns,
    aws_sqs as sqs,
)
from constructs import Construct


_ATHENA_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ATHENA_ROLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)
ATHENA_QUERY_ACTIONS = [
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:GetWorkGroup",
    "athena:StartQueryExecution",
    "athena:StopQueryExecution",
]
ATHENA_ASSUME_ROLE_ACTIONS = [
    "sts:AssumeRole",
    "sts:SetSourceIdentity",
    "sts:TagSession",
]
_MAX_ATHENA_BINDINGS_CHARACTERS = 2_048


@dataclass(frozen=True)
class AthenaInfrastructureConfig:
    """Deployment-bound query role allow-list and execution limits."""

    bindings_json: str
    role_arns: tuple[str, ...]
    timeout_seconds: str
    max_rows: str
    max_result_bytes: str
    max_bytes_scanned: str
    poll_interval_seconds: str
    project_rpm: str
    principal_rpm: str
    project_concurrency: str
    principal_concurrency: str
    project_scan_bytes_per_minute: str
    principal_scan_bytes_per_minute: str
    max_datasources_per_tenant: str

    @property
    def enabled(self) -> bool:
        return bool(self.role_arns)

    def environment(self) -> dict[str, str]:
        return {
            "AXON_ATHENA_QUERY_ENABLED": (
                "true" if self.enabled else "false"
            ),
            "AXON_ATHENA_QUERY_BINDINGS": self.bindings_json,
            "AXON_ATHENA_QUERY_TIMEOUT_SECONDS": self.timeout_seconds,
            "AXON_ATHENA_QUERY_MAX_ROWS": self.max_rows,
            "AXON_ATHENA_QUERY_MAX_RESULT_BYTES": self.max_result_bytes,
            "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED": (
                self.max_bytes_scanned
            ),
            "AXON_ATHENA_QUERY_POLL_INTERVAL_SECONDS": (
                self.poll_interval_seconds
            ),
            "AXON_ATHENA_QUERY_PROJECT_RPM": self.project_rpm,
            "AXON_ATHENA_QUERY_PRINCIPAL_RPM": self.principal_rpm,
            "AXON_ATHENA_QUERY_PROJECT_CONCURRENCY": (
                self.project_concurrency
            ),
            "AXON_ATHENA_QUERY_PRINCIPAL_CONCURRENCY": (
                self.principal_concurrency
            ),
            "AXON_ATHENA_QUERY_PROJECT_SCAN_BYTES_PER_MINUTE": (
                self.project_scan_bytes_per_minute
            ),
            "AXON_ATHENA_QUERY_PRINCIPAL_SCAN_BYTES_PER_MINUTE": (
                self.principal_scan_bytes_per_minute
            ),
            "AXON_ATHENA_QUERY_MAX_DATASOURCES_PER_TENANT": (
                self.max_datasources_per_tenant
            ),
            "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        }


def load_athena_infrastructure_config(
    construct: Construct,
) -> AthenaInfrastructureConfig:
    """Validate CDK context before it can become runtime authority."""

    raw_bindings = construct.node.try_get_context("athena_query_bindings")
    if raw_bindings in (None, ""):
        bindings: object = []
    elif isinstance(raw_bindings, str):
        try:
            bindings = json.loads(raw_bindings)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "athena_query_bindings must be valid JSON"
            ) from exc
    else:
        bindings = raw_bindings
    if not isinstance(bindings, list):
        raise ValueError("athena_query_bindings must be a JSON array")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(
                f"athena_query_bindings[{index}] must be an object"
            )
        expected = {"tenant_id", "project_id", "role_arn"}
        if set(binding) != expected:
            raise ValueError(
                f"athena_query_bindings[{index}] must contain exactly "
                "tenant_id, project_id, and role_arn"
            )
        tenant_id = binding["tenant_id"]
        project_id = binding["project_id"]
        role_arn = binding["role_arn"]
        if (
            not isinstance(tenant_id, str)
            or _ATHENA_IDENTIFIER.fullmatch(tenant_id) is None
        ):
            raise ValueError(
                f"athena_query_bindings[{index}].tenant_id is invalid"
            )
        if (
            not isinstance(project_id, str)
            or _ATHENA_IDENTIFIER.fullmatch(project_id) is None
        ):
            raise ValueError(
                f"athena_query_bindings[{index}].project_id is invalid"
            )
        if (
            not isinstance(role_arn, str)
            or "*" in role_arn
            or _ATHENA_ROLE_ARN.fullmatch(role_arn) is None
        ):
            raise ValueError(
                f"athena_query_bindings[{index}].role_arn must be a "
                "concrete IAM role ARN"
            )
        identity = (tenant_id, project_id, role_arn)
        if identity in seen:
            raise ValueError("athena_query_bindings contains a duplicate")
        seen.add(identity)
        normalized.append(
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "role_arn": role_arn,
            }
        )

    bindings_json = json.dumps(
        normalized,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(bindings_json) > _MAX_ATHENA_BINDINGS_CHARACTERS:
        raise ValueError(
            "athena_query_bindings exceeds the AgentCore "
            "2,048-character environment value limit"
        )

    def integer_limit(
        context_name: str,
        default: int,
        minimum: int,
        maximum: int | None = None,
    ) -> str:
        value = construct.node.try_get_context(context_name)
        resolved = default if value in (None, "") else value
        if (
            isinstance(resolved, str)
            and re.fullmatch(r"[0-9]+", resolved) is not None
        ):
            resolved = int(resolved)
        if (
            isinstance(resolved, bool)
            or not isinstance(resolved, int)
            or resolved < minimum
            or (maximum is not None and resolved > maximum)
        ):
            if maximum is None:
                raise ValueError(
                    f"{context_name} must be at least {minimum}"
                )
            raise ValueError(
                f"{context_name} must be between {minimum} and {maximum}"
            )
        return str(resolved)

    def float_limit(
        context_name: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> str:
        value = construct.node.try_get_context(context_name)
        resolved = default if value in (None, "") else value
        if isinstance(resolved, str):
            try:
                resolved = float(resolved)
            except ValueError:
                pass
        if (
            isinstance(resolved, bool)
            or not isinstance(resolved, (int, float))
            or not math.isfinite(resolved)
            or not minimum <= resolved <= maximum
        ):
            raise ValueError(
                f"{context_name} must be between {minimum} and {maximum}"
            )
        return f"{resolved:g}"

    max_bytes_scanned = integer_limit(
        "athena_query_max_bytes_scanned",
        1024 * 1024 * 1024,
        1,
    )
    project_rpm = integer_limit(
        "athena_query_project_rpm",
        30,
        1,
        10_000,
    )
    principal_rpm = integer_limit(
        "athena_query_principal_rpm",
        10,
        1,
        10_000,
    )
    project_concurrency = integer_limit(
        "athena_query_project_concurrency",
        5,
        1,
        100,
    )
    principal_concurrency = integer_limit(
        "athena_query_principal_concurrency",
        2,
        1,
        100,
    )
    project_scan_bytes_per_minute = integer_limit(
        "athena_query_project_scan_bytes_per_minute",
        5 * 1024 * 1024 * 1024,
        1,
    )
    principal_scan_bytes_per_minute = integer_limit(
        "athena_query_principal_scan_bytes_per_minute",
        2 * 1024 * 1024 * 1024,
        1,
    )
    max_datasources_per_tenant = integer_limit(
        "athena_query_max_datasources_per_tenant",
        500,
        1,
        10_000,
    )
    if int(principal_rpm) > int(project_rpm):
        raise ValueError(
            "athena_query_principal_rpm must not exceed "
            "athena_query_project_rpm"
        )
    if int(principal_concurrency) > int(project_concurrency):
        raise ValueError(
            "athena_query_principal_concurrency must not exceed "
            "athena_query_project_concurrency"
        )
    if int(principal_scan_bytes_per_minute) > int(
        project_scan_bytes_per_minute
    ):
        raise ValueError(
            "principal query scan budget must not exceed project budget"
        )
    if int(max_bytes_scanned) > int(
        principal_scan_bytes_per_minute
    ):
        raise ValueError(
            "athena_query_max_bytes_scanned must fit within the "
            "principal aggregate scan budget"
        )

    return AthenaInfrastructureConfig(
        bindings_json=bindings_json,
        role_arns=tuple(
            sorted({binding["role_arn"] for binding in normalized})
        ),
        timeout_seconds=float_limit(
            "athena_query_timeout_seconds",
            30.0,
            0.001,
            300.0,
        ),
        max_rows=integer_limit(
            "athena_query_max_rows",
            1000,
            1,
            10_000,
        ),
        max_result_bytes=integer_limit(
            "athena_query_max_result_bytes",
            1024 * 1024,
            1024,
            16 * 1024 * 1024,
        ),
        max_bytes_scanned=max_bytes_scanned,
        poll_interval_seconds=float_limit(
            "athena_query_poll_interval_seconds",
            0.25,
            0.05,
            5.0,
        ),
        project_rpm=project_rpm,
        principal_rpm=principal_rpm,
        project_concurrency=project_concurrency,
        principal_concurrency=principal_concurrency,
        project_scan_bytes_per_minute=(
            project_scan_bytes_per_minute
        ),
        principal_scan_bytes_per_minute=(
            principal_scan_bytes_per_minute
        ),
        max_datasources_per_tenant=max_datasources_per_tenant,
    )


class AxonLLMAgentCoreStack(Stack):
    """Contained AgentCore runtime with tenant-safe identity and state."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        query_config = load_athena_infrastructure_config(self)

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
            allowed_pattern=(
                r"^https://[^?#\s]+/\.well-known/openid-configuration$"
            ),
            description="OIDC discovery URL used by the AgentCore JWT authorizer",
        )
        oidc_client_id = CfnParameter(
            self,
            "OidcClientId",
            type="String",
            min_length=1,
            description="OIDC client ID allowed to invoke the runtime",
        )
        oidc_audience = CfnParameter(
            self,
            "OidcAudience",
            type="String",
            min_length=1,
            description="OIDC audience allowed to invoke the runtime",
        )
        oidc_tenant_claim = CfnParameter(
            self,
            "OidcTenantClaim",
            type="String",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^\S+$",
            description=(
                "Signed OIDC claim containing the AxonLLM tenant hint"
            ),
        )
        oidc_project_claim = CfnParameter(
            self,
            "OidcProjectClaim",
            type="String",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^\S+$",
            description=(
                "Signed OIDC claim containing the AxonLLM project hint"
            ),
        )
        approved_https_prefix_list_id = CfnParameter(
            self,
            "ApprovedHttpsPrefixListId",
            type="String",
            allowed_pattern=r"^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=(
                "Managed prefix list containing approved OIDC and provider "
                "HTTPS destinations"
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
                "each value must be a concrete Bedrock model or inference-profile "
                "ARN without wildcards"
            ),
            description=(
                "Comma-separated Bedrock model or inference-profile ARNs "
                "that AxonLLM may invoke"
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
                f"must be an immutable private ECR URI in {self.region} "
                "ending in @sha256:<64 lowercase hex characters>"
            ),
            description=(
                "Immutable ARM64 AgentCore image emitted by the release "
                "deployment verification gate"
            ),
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
            ec2.Peer.prefix_list(
                approved_https_prefix_list_id.value_as_string
            ),
            ec2.Port.tcp(443),
            "HTTPS to explicitly approved external destinations",
        )

        dynamodb_endpoint = vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[
                ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                )
            ],
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
                            "Values": [
                                f"com.amazonaws.{self.region}.dynamodb"
                            ],
                        }
                    ]
                },
                output_paths=["PrefixLists.0.PrefixListId"],
                physical_resource_id=cr.PhysicalResourceId.from_response(
                    "PrefixLists.0.PrefixListId"
                ),
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
            ec2.Peer.prefix_list(
                dynamodb_prefix_list.get_response_field(
                    "PrefixLists.0.PrefixListId"
                )
            ),
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
            subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )
        athena_endpoint = None
        sts_endpoint = None
        if query_config.enabled:
            athena_endpoint = vpc.add_interface_endpoint(
                "AthenaEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.ATHENA,
                open=False,
                private_dns_enabled=True,
                security_groups=[endpoint_security_group],
                subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
            )
            sts_endpoint = vpc.add_interface_endpoint(
                "StsEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.STS,
                open=False,
                private_dns_enabled=True,
                security_groups=[endpoint_security_group],
                subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
            )

        data_key = kms.Key(
            self,
            "DataKey",
            alias="alias/axonllm/agentcore-data",
            description="Encrypts AxonLLM AgentCore state and logs",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
            pending_window=Duration.days(30),
        )
        data_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogsEncryption",
                principals=[
                    iam.ServicePrincipal(
                        f"logs.{self.region}.{self.url_suffix}"
                    )
                ],
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:{self.partition}:logs:{self.region}:"
                            f"{self.account}:log-group:*"
                        )
                    }
                },
            )
        )
        data_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmEncryption",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],
            )
        )
        state_table_name = (
            self.node.try_get_context("agentcore_table_name")
            or "axonllm-agentcore-state"
        )
        if len(state_table_name) > 214:
            raise ValueError(
                "AgentCore state table name must be at most 214 characters "
                "to preserve the PITR validation suffix"
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
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=data_key,
            deletion_protection=True,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                )
            ),
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.RETAIN,
        )
        event_dead_letter_queue = sqs.Queue(
            self,
            "SecurityEventDeadLetterQueue",
            fifo=True,
            content_based_deduplication=False,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=data_key,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )
        event_outbox_queue = sqs.Queue(
            self,
            "SecurityEventOutboxQueue",
            fifo=True,
            content_based_deduplication=False,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=data_key,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            receive_message_wait_time=Duration.seconds(20),
            visibility_timeout=Duration.minutes(2),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=event_dead_letter_queue,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        security_event_topic = sns.Topic(
            self,
            "SecurityEventTopic",
            display_name="AxonLLM AgentCore durable security events",
            fifo=True,
            content_based_deduplication=False,
            enforce_ssl=True,
            master_key=data_key,
        )
        security_event_log_group = logs.LogGroup(
            self,
            "SecurityEventLogGroup",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        logs.LogStream(
            self,
            "SecurityEventLogStream",
            log_group=security_event_log_group,
            log_stream_name="events",
        )
        sqs_endpoint = vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
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
            subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
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
            subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
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
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    state_table.table_arn,
                    f"{state_table.table_arn}/index/*",
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

        backup_key = kms.Key(
            self,
            "BackupKey",
            alias="alias/axonllm/agentcore-backups",
            description="Encrypts scheduled AxonLLM AgentCore backups",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
            pending_window=Duration.days(30),
        )
        backup_vault = backup.BackupVault(
            self,
            "StateBackupVault",
            backup_vault_name=Fn.join(
                "-",
                [
                    "axon-agent",
                    Fn.select(2, Fn.split("/", self.stack_id)),
                ],
            ),
            encryption_key=backup_key,
            lock_configuration=backup.LockConfiguration(
                min_retention=Duration.days(30),
                max_retention=Duration.days(365),
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        backup_plan = backup.BackupPlan(
            self,
            "StateBackupPlan",
            backup_vault=backup_vault,
        )
        backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="DailyRetainedBackup",
                schedule_expression=events.Schedule.cron(
                    minute="30",
                    hour="5",
                ),
                start_window=Duration.hours(1),
                completion_window=Duration.hours(4),
                move_to_cold_storage_after=Duration.days(30),
                delete_after=Duration.days(365),
                recovery_point_tags={
                    "Application": "AxonLLM",
                    "Runtime": "AgentCore",
                },
            )
        )
        backup_plan.add_selection(
            "StateTableSelection",
            resources=[backup.BackupResource.from_dynamo_db_table(state_table)],
            allow_restores=True,
        )

        application_logs = logs.LogGroup(
            self,
            "ApplicationLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        usage_logs = logs.LogGroup(
            self,
            "UsageLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )

        runtime_execution_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name=Fn.join(
                "-",
                ["axonllm-agentcore-runtime", self.region],
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
                            f"{self.region}:{self.account}:runtime/axonllm*"
                        )
                    },
                },
            ),
            description="Execution role for Bedrock Agent Core Runtime",
            max_session_duration=Duration.hours(8),
        )
        runtime_artifact = agentcore.AgentRuntimeArtifact.from_image_uri(
            verified_image_uri.value_as_string
        )
        runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name="axonllm",
            description="Tenant-isolated AxonLLM production runtime",
            agent_runtime_artifact=runtime_artifact,
            execution_role=runtime_execution_role,
            authorizer_configuration=(
                agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                    oidc_discovery_url.value_as_string,
                    [oidc_client_id.value_as_string],
                    [oidc_audience.value_as_string],
                )
            ),
            environment_variables={
                "AWS_DEFAULT_REGION": self.region,
                "AXON_AWS_ACCOUNT_ID": self.account,
                "AXON_BEDROCK_REGION": self.region,
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                "AXON_DYNAMODB_TABLE": state_table.table_name,
                "AXON_EVENT_OUTBOX_QUEUE_URL": event_outbox_queue.queue_url,
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (
                    security_event_topic.topic_arn
                ),
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (
                    security_event_log_group.log_group_arn
                ),
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_OIDC_ISSUER": oidc_issuer.value_as_string,
                "AXON_OIDC_AUDIENCE": oidc_audience.value_as_string,
                "AXON_OIDC_TENANT_CLAIM": (
                    oidc_tenant_claim.value_as_string
                ),
                "AXON_OIDC_PROJECT_CLAIM": (
                    oidc_project_claim.value_as_string
                ),
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_ENABLED_PROVIDERS": "bedrock",
                **query_config.environment(),
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(10),
                max_lifetime=Duration.hours(4),
            ),
            logging_configs=[
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.APPLICATION_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(
                        application_logs
                    ),
                ),
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.USAGE_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(
                        usage_logs
                    ),
                ),
            ],
            network_configuration=(
                agentcore.RuntimeNetworkConfiguration.using_vpc(
                    self,
                    vpc=vpc,
                    security_groups=[runtime_security_group],
                    vpc_subnets=ec2.SubnetSelection(
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                    ),
                )
            ),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            request_header_configuration=(
                agentcore.RequestHeaderConfiguration(
                    allowlisted_headers=["Authorization"]
                )
            ),
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
        state_table.grant_read_write_data(runtime.role)
        event_outbox_queue.grant_send_messages(runtime.role)
        event_outbox_queue.grant_consume_messages(runtime.role)
        security_event_topic.grant_publish(runtime.role)
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
        if query_config.enabled:
            runtime.add_to_role_policy(
                iam.PolicyStatement(
                    actions=ATHENA_ASSUME_ROLE_ACTIONS,
                    resources=list(query_config.role_arns),
                )
            )
            if sts_endpoint is None or athena_endpoint is None:
                raise RuntimeError(
                    "query endpoints must exist when Athena is enabled"
                )
            sts_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[runtime.role],
                    actions=ATHENA_ASSUME_ROLE_ACTIONS,
                    resources=list(query_config.role_arns),
                )
            )
            athena_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[
                        iam.ArnPrincipal(role_arn)
                        for role_arn in query_config.role_arns
                    ],
                    actions=ATHENA_QUERY_ACTIONS,
                    resources=["*"],
                )
            )
        endpoint = runtime.add_endpoint(
            "production",
            description="AxonLLM production endpoint",
            version=runtime.agent_runtime_version,
        )

        alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="AxonLLM AgentCore production alarms",
            master_key=data_key,
        )
        alarm_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowAccountCloudWatchAlarms",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[alarm_topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:cloudwatch:{self.region}:"
                            f"{self.account}:alarm:*"
                        )
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
        dynamodb_throttles = (
            state_table.metric_throttled_requests_for_operations(
                operations=[
                    dynamodb.Operation.GET_ITEM,
                    dynamodb.Operation.QUERY,
                    dynamodb.Operation.SCAN,
                    dynamodb.Operation.PUT_ITEM,
                    dynamodb.Operation.UPDATE_ITEM,
                    dynamodb.Operation.DELETE_ITEM,
                    dynamodb.Operation.TRANSACT_GET_ITEMS,
                    dynamodb.Operation.TRANSACT_WRITE_ITEMS,
                ],
                period=Duration.minutes(5),
                statistic="Sum",
            )
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
                    event_dead_letter_queue
                    .metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(1),
                        statistic="Maximum",
                    )
                ),
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=(
                    cloudwatch.TreatMissingData.NOT_BREACHING
                ),
                alarm_description=(
                    "A security event exhausted delivery retries"
                ),
            ),
        ]
        alarm_action = cloudwatch_actions.SnsAction(alarm_topic)
        for alarm in alarms:
            alarm.add_alarm_action(alarm_action)
            alarm.add_ok_action(alarm_action)

        dashboard = cloudwatch.Dashboard(
            self,
            "OperationsDashboard",
            dashboard_name="AxonLLM-AgentCore-Production",
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
        )
        CfnOutput(
            self,
            "RuntimeExecutionRoleArn",
            value=runtime.role.role_arn,
            description=(
                "Exact principal that approved Athena datasource roles "
                "must trust"
            ),
        )
        CfnOutput(
            self,
            "RuntimeEndpointArn",
            value=endpoint.agent_runtime_endpoint_arn,
        )
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
            "DataKeyArn",
            value=data_key.key_arn,
            export_name=Fn.join(
                ":",
                [self.stack_name, "DataKeyArn"],
            ),
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
            "StateBackupVaultArn",
            value=backup_vault.backup_vault_arn,
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
