"""AxonLLM Fargate stack — CloudFront + ALB + ECS Fargate + DynamoDB + Secrets."""

import re

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CfnResource,
    CustomResource,
    CfnRule,
    CfnRuleAssertion,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_backup as backup,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    custom_resources as cr,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_elasticloadbalancingv2 as elbv2,
    aws_events as events,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_wafv2 as wafv2,
)
from constructs import Construct


_RECOVERY_CUTOVER_GUARD = """\
import boto3


ecs = boto3.client("ecs")
autoscaling = boto3.client("application-autoscaling")


def handler(event, _context):
    physical_id = "AxonLLMRecoveryCutoverGuard"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_id}

    properties = event["ResourceProperties"]
    previous = event.get("OldResourceProperties", {})
    cutover_mode = properties.get("CutoverMode") == "true"
    previous_cutover_mode = previous.get("CutoverMode") == "true"
    target_changed = (
        event["RequestType"] == "Update"
        and properties.get("TargetTable", "")
        != previous.get("TargetTable", "")
    )
    rollback_from_cutover = target_changed and previous_cutover_mode
    if target_changed and not (cutover_mode or rollback_from_cutover):
        raise RuntimeError(
            "recovery table changes require RecoveryCutoverMode=true"
        )
    if cutover_mode or rollback_from_cutover:
        resource_id = (
            f"service/{properties['ClusterName']}/"
            f"{properties['ServiceName']}"
        )
        targets = autoscaling.describe_scalable_targets(
            ServiceNamespace="ecs",
            ResourceIds=[resource_id],
            ScalableDimension="ecs:service:DesiredCount",
        ).get("ScalableTargets", [])
        if len(targets) != 1:
            raise RuntimeError(
                "recovery cutover requires exactly one ECS scalable target"
            )
        target = targets[0]
        suspended = target.get("SuspendedState", {})
        suspension_keys = (
            "DynamicScalingInSuspended",
            "DynamicScalingOutSuspended",
            "ScheduledScalingSuspended",
        )
        if target.get("MinCapacity") != 0 or not all(
            suspended.get(key) is True for key in suspension_keys
        ):
            raise RuntimeError(
                "recovery cutover requires autoscaling suspended with "
                "minimum capacity zero"
            )

        response = ecs.describe_services(
            cluster=properties["ClusterName"],
            services=[properties["ServiceName"]],
        )
        if response.get("failures") or len(response.get("services", [])) != 1:
            raise RuntimeError(
                "recovery cutover could not resolve the ECS service"
            )
        service = response["services"][0]
        counts = {
            name: service.get(name)
            for name in ("desiredCount", "pendingCount", "runningCount")
        }
        if any(value != 0 for value in counts.values()):
            raise RuntimeError(
                "recovery cutover requires a fully quiesced ECS service: "
                f"{counts}"
            )

    return {"PhysicalResourceId": physical_id}
"""


class AxonLLMStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if self.region != "us-east-1":
            raise ValueError(
                "AxonLLMStack must be deployed in us-east-1 because its "
                "CloudFront WebACL has global scope"
            )

        deployment_mode = CfnParameter(
            self,
            "DeploymentMode",
            type="String",
            default="staging",
            allowed_values=["staging", "production"],
            description=(
                "staging preserves the existing API-key/direct-JWT deployment; "
                "production also requires ALB OIDC and canonical identities"
            ),
        )
        oidc_issuer = CfnParameter(
            self,
            "OidcIssuer",
            type="String",
            default="",
            allowed_pattern=r"^$|^https://[^?#\s]+$",
            description="Exact HTTPS issuer used by the enterprise identity provider",
        )
        oidc_authorization_endpoint = CfnParameter(
            self,
            "OidcAuthorizationEndpoint",
            type="String",
            default="",
            allowed_pattern=r"^$|^https://[^?#\s]+$",
            description="OIDC authorization endpoint used by the ALB",
        )
        oidc_token_endpoint = CfnParameter(
            self,
            "OidcTokenEndpoint",
            type="String",
            default="",
            allowed_pattern=r"^$|^https://[^?#\s]+$",
            description="OIDC token endpoint used by the ALB",
        )
        oidc_user_info_endpoint = CfnParameter(
            self,
            "OidcUserInfoEndpoint",
            type="String",
            default="",
            allowed_pattern=r"^$|^https://[^?#\s]+$",
            description="OIDC user-info endpoint used by the ALB",
        )
        oidc_client_id = CfnParameter(
            self,
            "OidcClientId",
            type="String",
            default="",
            description="OIDC client identifier configured for the ALB",
        )
        oidc_client_secret = CfnParameter(
            self,
            "OidcClientSecret",
            type="String",
            default="",
            no_echo=True,
            description="OIDC client secret configured for the ALB",
        )
        oidc_audience = CfnParameter(
            self,
            "OidcAudience",
            type="String",
            default="",
            description="Exact audience accepted for direct OIDC bearer tokens",
        )
        public_hosted_zone_id = CfnParameter(
            self,
            "PublicHostedZoneId",
            type="String",
            default="",
            allowed_pattern=r"^$|^Z[A-Z0-9]+$",
            description=(
                "Optional Route 53 public hosted-zone ID for ViewerDomainName"
            ),
        )
        public_hosted_zone_name = CfnParameter(
            self,
            "PublicHostedZoneName",
            type="String",
            default="",
            description=(
                "Optional Route 53 public hosted-zone name; supply with "
                "PublicHostedZoneId to create A and AAAA aliases"
            ),
        )

        production_mode = CfnCondition(
            self,
            "ProductionMode",
            expression=Fn.condition_equals(
                deployment_mode.value_as_string,
                "production",
            ),
        )
        manage_public_dns = CfnCondition(
            self,
            "ManagePublicDns",
            expression=Fn.condition_and(
                Fn.condition_not(
                    Fn.condition_equals(
                        public_hosted_zone_id.value_as_string,
                        "",
                    )
                ),
                Fn.condition_not(
                    Fn.condition_equals(
                        public_hosted_zone_name.value_as_string,
                        "",
                    )
                ),
            ),
        )
        CfnRule(
            self,
            "ProductionIdentityInputs",
            rule_condition=Fn.condition_equals(
                deployment_mode.value_as_string,
                "production",
            ),
            assertions=[
                CfnRuleAssertion(
                    assert_=Fn.condition_not(
                        Fn.condition_equals(parameter.value_as_string, "")
                    ),
                    assert_description=f"{parameter.logical_id} is required in production mode",
                )
                for parameter in (
                    oidc_issuer,
                    oidc_authorization_endpoint,
                    oidc_token_endpoint,
                    oidc_user_info_endpoint,
                    oidc_client_id,
                    oidc_client_secret,
                    oidc_audience,
                )
            ],
        )
        CfnRule(
            self,
            "PublicDnsInputs",
            assertions=[
                CfnRuleAssertion(
                    assert_=Fn.condition_or(
                        Fn.condition_and(
                            Fn.condition_equals(
                                public_hosted_zone_id.value_as_string,
                                "",
                            ),
                            Fn.condition_equals(
                                public_hosted_zone_name.value_as_string,
                                "",
                            ),
                        ),
                        Fn.condition_and(
                            Fn.condition_not(
                                Fn.condition_equals(
                                    public_hosted_zone_id.value_as_string,
                                    "",
                                )
                            ),
                            Fn.condition_not(
                                Fn.condition_equals(
                                    public_hosted_zone_name.value_as_string,
                                    "",
                                )
                            ),
                        ),
                    ),
                    assert_description=(
                        "PublicHostedZoneId and PublicHostedZoneName must be "
                        "supplied together"
                    ),
                )
            ],
        )

        # These values are deliberately required CloudFormation parameters. A
        # deploy must bring account-owned TLS, DNS, and egress policy; the
        # reference stack must not silently fall back to plaintext or open
        # networking when those controls are absent.
        viewer_domain_name = CfnParameter(
            self,
            "ViewerDomainName",
            type="String",
            min_length=1,
            description=(
                "CloudFront alternate domain name covered by ViewerCertificateArn"
            ),
        )
        viewer_certificate_arn = CfnParameter(
            self,
            "ViewerCertificateArn",
            type="String",
            min_length=1,
            description=(
                "ACM certificate ARN in us-east-1 covering ViewerDomainName"
            ),
        )
        origin_domain_name = CfnParameter(
            self,
            "OriginDomainName",
            type="String",
            min_length=1,
            description=(
                "Private ALB origin name covered by OriginCertificateArn"
            ),
        )
        origin_certificate_arn = CfnParameter(
            self,
            "OriginCertificateArn",
            type="String",
            min_length=1,
            description=(
                "ACM certificate ARN in this stack's region covering OriginDomainName"
            ),
        )
        approved_https_prefix_list_id = CfnParameter(
            self,
            "ApprovedHttpsPrefixListId",
            type="String",
            allowed_pattern=r"^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=(
                "Managed prefix list containing every approved HTTPS destination "
                "required by ECS, AWS APIs, and configured LLM providers"
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
                r"^[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/"
                r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@"
                r"sha256:[0-9a-f]{64}$"
            ),
            constraint_description=(
                "must be an immutable private ECR URI in us-east-1 ending "
                "in @sha256:<64 lowercase hex characters>"
            ),
            description=(
                "Immutable ECR image emitted by the release deployment "
                "verification gate"
            ),
        )
        scim_tenants_secret_arn = self.node.try_get_context("scim_tenants_secret_arn")
        if scim_tenants_secret_arn is not None and (
            not isinstance(scim_tenants_secret_arn, str)
            or re.fullmatch(
                r"arn:aws:secretsmanager:us-east-1:[0-9]{12}:"
                r"secret:[A-Za-z0-9/_+=.@-]+",
                scim_tenants_secret_arn,
            )
            is None
        ):
            raise ValueError(
                "scim_tenants_secret_arn must be a complete Secrets Manager "
                "ARN in us-east-1"
            )
        primary_state_table_name = (
            self.node.try_get_context("table_name") or "axonllm-state"
        )
        restore_table_marker = "-restore-validation-"
        restore_table_suffix_limit = (
            255
            - len(primary_state_table_name)
            - len(restore_table_marker)
        )
        if restore_table_suffix_limit < 21:
            raise ValueError(
                "Fargate state table name must be at most 214 characters "
                "to preserve the PITR validation suffix"
            )
        restored_state_table_pattern = (
            rf"^$|^{re.escape(primary_state_table_name)}"
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
                "must be blank or a PITR validation table derived from the "
                f"{primary_state_table_name} primary table"
            ),
            description=(
                "Optional restored state table used for a controlled recovery "
                "cutover; blank selects the stack-managed primary table"
            ),
        )
        recovery_cutover_mode = CfnParameter(
            self,
            "RecoveryCutoverMode",
            type="String",
            default="false",
            allowed_values=["false", "true"],
            description=(
                "Pins ECS desired count to zero while a quiesced recovery "
                "table switch is deployed; return to false after validation"
            ),
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
        recovery_cutover_active = CfnCondition(
            self,
            "RecoveryCutoverActive",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "true",
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

        # --- Networking ---
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
                    name="Application",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        task_security_group = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM task ingress and explicitly approved egress",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.udp(53),
            "DNS to the VPC resolver",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(53),
            "DNS fallback to the VPC resolver",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(approved_https_prefix_list_id.value_as_string),
            ec2.Port.tcp(443),
            "HTTPS to explicitly approved destinations",
        )

        prefix_lookup_logs = logs.LogGroup(
            self,
            "PrefixLookupLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        cloudfront_origin_prefix_list = cr.AwsCustomResource(
            self,
            "CloudFrontOriginPrefixList",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="describeManagedPrefixLists",
                parameters={
                    "Filters": [
                        {
                            "Name": "prefix-list-name",
                            "Values": [
                                "com.amazonaws.global.cloudfront.origin-facing"
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
        cloudfront_origin_prefix_list_id = (
            cloudfront_origin_prefix_list.get_response_field(
                "PrefixLists.0.PrefixListId"
            )
        )

        alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM ALB ingress and explicitly approved egress",
        )
        alb_security_group.add_ingress_rule(
            ec2.Peer.prefix_list(cloudfront_origin_prefix_list_id),
            ec2.Port.tcp(443),
            "TLS from the CloudFront origin-facing managed prefix list",
        )
        alb_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.udp(53),
            "DNS to the VPC resolver",
        )
        alb_security_group.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(53),
            "DNS fallback to the VPC resolver",
        )
        alb_security_group.add_egress_rule(
            ec2.Peer.prefix_list(
                approved_https_prefix_list_id.value_as_string
            ),
            ec2.Port.tcp(443),
            "HTTPS to the approved OIDC identity provider",
        )
        alb_security_group.add_egress_rule(
            task_security_group,
            ec2.Port.tcp(8000),
            "Application traffic to AxonLLM tasks",
        )
        task_security_group.add_ingress_rule(
            alb_security_group,
            ec2.Port.tcp(8000),
            "Application traffic from the AxonLLM ALB",
        )

        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=False,
            desync_mitigation_mode=elbv2.DesyncMitigationMode.STRICTEST,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )

        # --- Secrets ---
        data_key = kms.Key(
            self,
            "DataKey",
            alias="alias/axonllm/data",
            description="Encrypts AxonLLM DynamoDB state and provider secrets",
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
        api_keys_secret = secretsmanager.Secret(
            self,
            "ApiKeys",
            description="LLM provider API keys for AxonLLM",
            encryption_key=data_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"ANTHROPIC_API_KEY":"","OPENAI_API_KEY":""}',
                generate_string_key="placeholder",
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- DynamoDB table ---
        # The application (src/gateway/persistence.py) uses a SINGLE-TABLE design:
        # one table with a composite PK/SK (uppercase) key and an `entity_type`
        # attribute discriminating usage records, projects, user configs, API keys,
        # policies, feedback, and audit records. The table name here MUST match the
        # AXON_DYNAMODB_TABLE env var passed to the container below, and the key
        # schema MUST be PK (HASH) / SK (RANGE), both String.
        # RETAIN plus a fixed physical name makes the stack un-redeployable: after
        # a `cdk destroy`, the table survives on purpose (it holds every project,
        # key and audit record) but nothing owns it, so the next deploy fails
        # early with "Resource of type 'AWS::DynamoDB::Table' with identifier
        # 'axonllm-state' already exists" and creates nothing at all.
        #
        # `-c table_name=…` is the way out that does not touch the retained data:
        # deploy against a new table and the old one stays exactly as it was, to
        # be inspected, migrated, or deleted deliberately. Deleting a table that
        # RETAIN went out of its way to preserve should be a decision, not a step
        # in a redeploy.
        state_table = dynamodb.Table(
            self,
            "StateTable",
            table_name=primary_state_table_name,
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=data_key,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            time_to_live_attribute="expires_at",
        )
        selected_state_table_name = Token.as_string(
            Fn.condition_if(
                use_recovered_state.logical_id,
                runtime_state_table_name.value_as_string,
                state_table.table_name,
            )
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
            display_name="AxonLLM durable security events",
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
        aws_endpoint_security_group = ec2.SecurityGroup(
            self,
            "AwsServiceEndpointSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description=(
                "Private AWS service endpoints for AxonLLM security events"
            ),
        )
        aws_endpoint_security_group.add_ingress_rule(
            task_security_group,
            ec2.Port.tcp(443),
            "HTTPS from AxonLLM tasks",
        )
        task_security_group.add_egress_rule(
            aws_endpoint_security_group,
            ec2.Port.tcp(443),
            "Security-event delivery through private AWS endpoints",
        )
        sqs_endpoint = vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            open=False,
            private_dns_enabled=True,
            security_groups=[aws_endpoint_security_group],
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
            security_groups=[aws_endpoint_security_group],
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
            security_groups=[aws_endpoint_security_group],
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

        backup_key = kms.Key(
            self,
            "BackupKey",
            alias="alias/axonllm/backups",
            description="Encrypts scheduled AxonLLM state backups",
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
                    "axon-state",
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
                    minute="0",
                    hour="5",
                ),
                start_window=Duration.hours(1),
                completion_window=Duration.hours(4),
                move_to_cold_storage_after=Duration.days(30),
                delete_after=Duration.days(365),
                recovery_point_tags={
                    "Application": "AxonLLM",
                    "DataClassification": "state",
                },
            )
        )
        backup_plan.add_selection(
            "StateTableSelection",
            resources=[backup.BackupResource.from_dynamo_db_table(state_table)],
            allow_restores=True,
        )
        recovered_backup_selection = backup_plan.add_selection(
            "RecoveredStateTableSelection",
            resources=[
                backup.BackupResource.from_arn(
                    self.format_arn(
                        service="dynamodb",
                        resource="table",
                        resource_name=(
                            runtime_state_table_name.value_as_string
                        ),
                    )
                )
            ],
            allow_restores=True,
        )
        for child in recovered_backup_selection.node.find_all():
            if isinstance(child, CfnResource):
                child.cfn_options.condition = use_recovered_state

        # --- ECS Cluster ---
        # cluster_name is explicit because every post-deploy instruction in the
        # README addresses the cluster by name (`--cluster axonllm`). Left unset,
        # CDK generates something like AxonLLMStack-ClusterEB0386A7-rSJKGJp9AqGt,
        # and the documented commands fail with a MISSING cluster — verified
        # against a real deployment. Naming resources you have to type is worth
        # the tradeoff below.
        #
        # The tradeoff: a physical name blocks CloudFormation from doing a
        # replacement update (two clusters cannot share a name), so a change that
        # requires replacing the cluster now fails instead of silently rebuilding
        # it. For a singleton deployment that is the safer failure.
        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            cluster_name="axonllm",
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        origin_certificate = acm.Certificate.from_certificate_arn(
            self,
            "OriginCertificate",
            origin_certificate_arn.value_as_string,
        )

        container_secrets = {
            "ANTHROPIC_API_KEY": ecs.Secret.from_secrets_manager(
                api_keys_secret, "ANTHROPIC_API_KEY"
            ),
            "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(
                api_keys_secret, "OPENAI_API_KEY"
            ),
        }
        scim_tenants_secret = None
        if scim_tenants_secret_arn is not None:
            scim_tenants_secret = (
                secretsmanager.Secret.from_secret_complete_arn(
                    self,
                    "ScimTenantsSecret",
                    scim_tenants_secret_arn,
                )
            )
            container_secrets["AXON_SCIM_TENANTS"] = (
                ecs.Secret.from_secrets_manager(scim_tenants_secret)
            )

        # --- Fargate service with ALB ---
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=2048,
            desired_count=2,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
            # Named for the same reason as the cluster: `--service axonllm` and
            # `--task-definition axonllm` appear in the README's post-deploy
            # steps, and both failed against a real deployment before this.
            service_name="axonllm",
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry(
                    verified_image_uri.value_as_string
                ),
                container_port=8000,
                family="axonllm",
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="axonllm",
                    log_retention=logs.RetentionDays.ONE_MONTH,
                ),
                secrets=container_secrets,
                environment={
                    "AWS_DEFAULT_REGION": self.region,
                    "AXON_AWS_ACCOUNT_ID": self.account,
                    "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                    "AXON_DYNAMODB_TABLE": selected_state_table_name,
                    "AXON_EVENT_OUTBOX_QUEUE_URL": (
                        event_outbox_queue.queue_url
                    ),
                    "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (
                        security_event_topic.topic_arn
                    ),
                    "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (
                        security_event_log_group.log_group_arn
                    ),
                    "AXON_AUTH_MODE": "ENFORCE",
                    "AXON_DEPLOYMENT_PROFILE": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            "production",
                            "development",
                        )
                    ),
                    "AXON_OIDC_ISSUER": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            oidc_issuer.value_as_string,
                            "",
                        )
                    ),
                    "AXON_OIDC_AUDIENCE": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            oidc_audience.value_as_string,
                            "",
                        )
                    ),
                    "AXON_ALB_SIGNER_ARN": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            load_balancer.load_balancer_arn,
                            "",
                        )
                    ),
                    "AXON_ALB_CLIENT_ID": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            oidc_client_id.value_as_string,
                            "",
                        )
                    ),
                    "AXON_ALB_ISSUER": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            (
                                "https://public-keys.auth.elb."
                                f"{self.region}.amazonaws.com"
                            ),
                            "",
                        )
                    ),
                    "AXON_REQUIRE_CANONICAL_IDENTITY": Token.as_string(
                        Fn.condition_if(
                            production_mode.logical_id,
                            "true",
                            "false",
                        )
                    ),
                    "AXON_SERVER_PORT": "8000",
                    "HOME": "/tmp",
                    # Explicit "false" because the container CMD is
                    # serve_dashboard.py, which defaults this to "true" when the
                    # variable is absent. Omitting it here deploys Acme Corp,
                    # three fictional users and 66 fabricated usage records to
                    # Fargate — indistinguishable from real usage in the UI, and
                    # merged into DynamoDB where they outlive the flag. For a
                    # seeded demo deployment, flip this to "true" deliberately.
                    "AXON_LOAD_DEMO_DATA": "false",
                },
            ),
            assign_public_ip=False,
            load_balancer=load_balancer,
            task_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[task_security_group],
            open_listener=False,
            certificate=origin_certificate,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            listener_port=443,
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
            health_check_grace_period=Duration.seconds(60),
        )
        cutover_guard_handler_logs = logs.LogGroup(
            self,
            "RecoveryCutoverGuardHandlerLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cutover_guard_handler = lambda_.Function(
            self,
            "RecoveryCutoverGuardHandler",
            description=(
                "Blocks DynamoDB state cutover until AxonLLM is quiesced"
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_RECOVERY_CUTOVER_GUARD),
            timeout=Duration.seconds(30),
            log_group=cutover_guard_handler_logs,
        )
        cutover_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=[
                    self.format_arn(
                        service="ecs",
                        resource="service",
                        resource_name="axonllm/axonllm",
                    )
                ],
            )
        )
        cutover_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "application-autoscaling:DescribeScalableTargets"
                ],
                resources=["*"],
            )
        )
        cutover_guard_provider_logs = logs.LogGroup(
            self,
            "RecoveryCutoverGuardProviderLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cutover_guard_provider = cr.Provider(
            self,
            "RecoveryCutoverGuardProvider",
            on_event_handler=cutover_guard_handler,
            log_group=cutover_guard_provider_logs,
        )
        cutover_guard = CustomResource(
            self,
            "RecoveryCutoverGuard",
            service_token=cutover_guard_provider.service_token,
            properties={
                "ClusterName": "axonllm",
                "CutoverMode": recovery_cutover_mode.value_as_string,
                "ServiceName": "axonllm",
                "TargetTable": (
                    runtime_state_table_name.value_as_string
                ),
            },
        )
        cfn_service = fargate_service.service.node.default_child
        if not isinstance(cfn_service, ecs.CfnService):
            raise TypeError("Fargate service did not create CfnService")
        cfn_service.desired_count = Token.as_number(
            Fn.condition_if(
                recovery_cutover_active.logical_id,
                0,
                2,
            )
        )
        cutover_guard_resource = cutover_guard.node.default_child
        if cutover_guard_resource is None:
            raise TypeError("recovery cutover guard has no CloudFormation child")
        cfn_service.add_dependency(cutover_guard_resource)

        task_definition = fargate_service.task_definition
        task_definition.add_volume(name="tmp")
        container = task_definition.default_container
        if container is None:
            raise RuntimeError("Fargate task must have an application container")
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/tmp",
                source_volume="tmp",
                read_only=False,
            )
        )
        cfn_task_definition = task_definition.node.default_child
        if not isinstance(cfn_task_definition, ecs.CfnTaskDefinition):
            raise RuntimeError("Fargate task definition escape hatch is unavailable")
        cfn_task_definition.add_property_override(
            "ContainerDefinitions.0.ReadonlyRootFilesystem",
            True,
        )
        cfn_task_definition.add_property_override(
            "ContainerDefinitions.0.LinuxParameters.Capabilities.Drop",
            ["ALL"],
        )
        cfn_task_definition.add_property_override(
            "ContainerDefinitions.0.LinuxParameters.InitProcessEnabled",
            True,
        )
        execution_role = (
            fargate_service.task_definition.obtain_execution_role()
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[verified_image_repository_arn],
            )
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        admin_oidc_rule = elbv2.CfnListenerRule(
            self,
            "AdminOidc",
            listener_arn=fargate_service.listener.listener_arn,
            priority=10,
            conditions=[
                elbv2.CfnListenerRule.RuleConditionProperty(
                    field="path-pattern",
                    path_pattern_config=(
                        elbv2.CfnListenerRule.PathPatternConfigProperty(
                            values=[
                                "/admin",
                                "/admin/*",
                                "/oauth2/idpresponse",
                            ]
                        )
                    ),
                )
            ],
            actions=[
                elbv2.CfnListenerRule.ActionProperty(
                    type="authenticate-oidc",
                    order=1,
                    authenticate_oidc_config=(
                        elbv2.CfnListenerRule.AuthenticateOidcConfigProperty(
                            authorization_endpoint=(
                                oidc_authorization_endpoint.value_as_string
                            ),
                            client_id=oidc_client_id.value_as_string,
                            client_secret=oidc_client_secret.value_as_string,
                            issuer=oidc_issuer.value_as_string,
                            token_endpoint=oidc_token_endpoint.value_as_string,
                            user_info_endpoint=(
                                oidc_user_info_endpoint.value_as_string
                            ),
                            on_unauthenticated_request="authenticate",
                            scope="openid email profile",
                            session_cookie_name="AxonLLMOIDCSession",
                            session_timeout=28800,
                        )
                    ),
                ),
                elbv2.CfnListenerRule.ActionProperty(
                    type="forward",
                    order=2,
                    target_group_arn=(
                        fargate_service.target_group.target_group_arn
                    ),
                ),
            ],
        )
        admin_oidc_rule.cfn_options.condition = production_mode

        # --- Health check ---
        fargate_service.target_group.configure_health_check(
            path="/ready",
            healthy_http_codes="200",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # --- Auto-scaling ---
        scaling = fargate_service.service.auto_scale_task_count(
            min_capacity=2,
            max_capacity=10,
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(30),
        )
        scaling.scale_on_request_count(
            "RequestScaling",
            requests_per_target=500,
            target_group=fargate_service.target_group,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(30),
        )

        # --- IAM permissions ---
        task_role = fargate_service.task_definition.task_role

        # Bedrock runtime and Mantle inference. Mantle uses a separate IAM
        # service prefix; grant only the two API operations this runtime calls.
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_invoke_resource_arns.value_as_list,
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-mantle:CreateInference",
                    "bedrock-mantle:ListModels",
                ],
                resources=["*"],
            )
        )

        # Resolve IAM through the same condition as AXON_DYNAMODB_TABLE so a
        # recovery cutover never leaves both state tables writable.
        selected_state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name,
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="UseSelectedStateTable",
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
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )

        # Durable security-event delivery and retry processing.
        event_outbox_queue.grant_send_messages(task_role)
        event_outbox_queue.grant_consume_messages(task_role)
        security_event_topic.grant_publish(task_role)
        security_event_log_group.grant_write(task_role)

        # Secrets Manager (read-only for API keys)
        api_keys_secret.grant_read(task_role)

        # --- Sticky sessions for SSE streaming ---
        fargate_service.target_group.set_attribute(
            "stickiness.enabled", "true"
        )
        fargate_service.target_group.set_attribute(
            "stickiness.type", "lb_cookie"
        )
        fargate_service.target_group.set_attribute(
            "stickiness.lb_cookie.duration_seconds", "3600"
        )

        # --- Idle timeout for long streaming responses ---
        fargate_service.load_balancer.set_attribute(
            "idle_timeout.timeout_seconds", "300"
        )
        fargate_service.load_balancer.set_attribute(
            "routing.http.drop_invalid_header_fields.enabled", "true"
        )
        fargate_service.load_balancer.set_attribute(
            "deletion_protection.enabled",
            Token.as_string(
                Fn.condition_if(
                    production_mode.logical_id,
                    "true",
                    "false",
                )
            ),
        )

        # CloudFront standard logging requires ACLs, so this bucket must use
        # ObjectWriter ownership rather than S3's ACL-disabled default. ALB log
        # delivery adds its own least-privilege bucket policy below.
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogs",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    expiration=Duration.days(365),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        fargate_service.load_balancer.log_access_logs(
            access_logs_bucket,
            "alb",
        )

        # --- CloudFront distribution ---
        vpc_origin = origins.VpcOrigin.with_application_load_balancer(
            fargate_service.load_balancer,
            domain_name=origin_domain_name.value_as_string,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            origin_ssl_protocols=[cloudfront.OriginSslPolicy.TLS_V1_2],
            https_port=443,
            read_timeout=Duration.seconds(60),
            keepalive_timeout=Duration.seconds(60),
        )
        viewer_certificate = acm.Certificate.from_certificate_arn(
            self,
            "ViewerCertificate",
            viewer_certificate_arn.value_as_string,
        )
        web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="AxonLLMWebAcl",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesAmazonIpReputationList",
                    priority=10,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(
                        none={}
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            name="AWSManagedRulesAmazonIpReputationList",
                            vendor_name="AWS",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AxonLLMIPReputation",
                        sampled_requests_enabled=True,
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="PerIpRateLimit",
                    priority=20,
                    action=wafv2.CfnWebACL.RuleActionProperty(
                        block=wafv2.CfnWebACL.BlockActionProperty()
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            aggregate_key_type="IP",
                            evaluation_window_sec=300,
                            limit=2000,
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AxonLLMRateLimit",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )
        distribution = cloudfront.Distribution(
            self,
            "CDN",
            default_behavior=cloudfront.BehaviorOptions(
                origin=vpc_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            ),
            additional_behaviors={
                "/admin/static/*": cloudfront.BehaviorOptions(
                    origin=vpc_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                ),
            },
            certificate=viewer_certificate,
            domain_names=[viewer_domain_name.value_as_string],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            ssl_support_method=cloudfront.SSLMethod.SNI,
            web_acl_id=web_acl.attr_arn,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            enable_logging=True,
            log_bucket=access_logs_bucket,
            log_file_prefix="cloudfront/",
            log_includes_cookies=False,
        )

        public_alias_target = route53.CfnRecordSet.AliasTargetProperty(
            dns_name=distribution.domain_name,
            hosted_zone_id="Z2FDTNDATAQYW2",
            evaluate_target_health=False,
        )
        public_a_record = route53.CfnRecordSet(
            self,
            "PublicAliasA",
            hosted_zone_id=public_hosted_zone_id.value_as_string,
            name=viewer_domain_name.value_as_string,
            type="A",
            alias_target=public_alias_target,
            comment="AxonLLM CloudFront IPv4 alias",
        )
        public_a_record.cfn_options.condition = manage_public_dns
        public_aaaa_record = route53.CfnRecordSet(
            self,
            "PublicAliasAaaa",
            hosted_zone_id=public_hosted_zone_id.value_as_string,
            name=viewer_domain_name.value_as_string,
            type="AAAA",
            alias_target=public_alias_target,
            comment="AxonLLM CloudFront IPv6 alias",
        )
        public_aaaa_record.cfn_options.condition = manage_public_dns

        # --- Monitoring and alerting ---
        alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="AxonLLM production alarms",
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
        running_tasks = cloudwatch.Metric(
            namespace="ECS/ContainerInsights",
            metric_name="RunningTaskCount",
            dimensions_map={
                "ClusterName": cluster.cluster_name,
                "ServiceName": fargate_service.service.service_name,
            },
            period=Duration.minutes(1),
            statistic="Minimum",
        )
        unhealthy_hosts = (
            fargate_service.target_group.metrics.unhealthy_host_count(
                period=Duration.minutes(1),
                statistic="Maximum",
            )
        )
        target_5xx = fargate_service.target_group.metrics.http_code_target(
            elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
            period=Duration.minutes(5),
            statistic="Sum",
        )
        alb_auth_errors = (
            fargate_service.load_balancer.metrics.elb_auth_error(
                period=Duration.minutes(5),
                statistic="Sum",
            )
        )
        cpu_utilization = (
            fargate_service.service.metric_cpu_utilization(
                period=Duration.minutes(5),
                statistic="Average",
            )
        )
        memory_utilization = (
            fargate_service.service.metric_memory_utilization(
                period=Duration.minutes(5),
                statistic="Average",
            )
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

        def state_operation_metrics(
            metric_name: str,
        ) -> dict[str, cloudwatch.Metric]:
            return {
                operation.lower(): cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name=metric_name,
                    dimensions_map={
                        "Operation": operation,
                        "TableName": selected_state_table_name,
                    },
                    period=Duration.minutes(5),
                    statistic="Sum",
                )
                for operation in state_operations
            }

        throttle_metrics = state_operation_metrics("ThrottledRequests")
        dynamodb_throttles = cloudwatch.MathExpression(
            expression=" + ".join(throttle_metrics),
            using_metrics=throttle_metrics,
            period=Duration.minutes(5),
            label="Sum of throttled requests across all operations",
        )
        system_error_metrics = state_operation_metrics("SystemErrors")
        dynamodb_system_errors = cloudwatch.MathExpression(
            expression=" + ".join(system_error_metrics),
            using_metrics=system_error_metrics,
            period=Duration.minutes(5),
            label="Sum of errors across all operations",
        )
        cloudfront_5xx_rate = distribution.metric5xx_error_rate(
            period=Duration.minutes(5),
            statistic="Average",
        )

        alarms = [
            cloudwatch.Alarm(
                self,
                "InsufficientTasksAlarm",
                metric=running_tasks,
                threshold=2,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD
                ),
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
                alarm_description=(
                    "AxonLLM is running fewer than its two required tasks"
                ),
            ),
            cloudwatch.Alarm(
                self,
                "UnhealthyTargetsAlarm",
                metric=unhealthy_hosts,
                threshold=1,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
                alarm_description=(
                    "The /ready target-group check is failing"
                ),
            ),
            cloudwatch.Alarm(
                self,
                "TargetErrorsAlarm",
                metric=target_5xx,
                threshold=10,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AxonLLM targets returned elevated 5xx errors",
            ),
            cloudwatch.Alarm(
                self,
                "AlbAuthenticationAlarm",
                metric=alb_auth_errors,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    "The ALB could not complete an OIDC authentication flow"
                ),
            ),
            cloudwatch.Alarm(
                self,
                "HighCpuAlarm",
                metric=cpu_utilization,
                threshold=85,
                evaluation_periods=3,
                datapoints_to_alarm=3,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
                alarm_description="AxonLLM ECS CPU is persistently high",
            ),
            cloudwatch.Alarm(
                self,
                "HighMemoryAlarm",
                metric=memory_utilization,
                threshold=85,
                evaluation_periods=3,
                datapoints_to_alarm=3,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
                alarm_description="AxonLLM ECS memory is persistently high",
            ),
            cloudwatch.Alarm(
                self,
                "DynamoDbThrottlesAlarm",
                metric=dynamodb_throttles,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AxonLLM state requests are being throttled",
            ),
            cloudwatch.Alarm(
                self,
                "DynamoDbSystemErrorsAlarm",
                metric=dynamodb_system_errors,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="DynamoDB reported AxonLLM system errors",
            ),
            cloudwatch.Alarm(
                self,
                "CloudFrontErrorsAlarm",
                metric=cloudfront_5xx_rate,
                threshold=5,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="CloudFront 5xx error rate exceeded 5 percent",
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
            dashboard_name="AxonLLM-Production",
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Service readiness and errors",
                left=[running_tasks, unhealthy_hosts],
                right=[target_5xx, cloudfront_5xx_rate],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Service utilization",
                left=[cpu_utilization, memory_utilization],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="OIDC authentication",
                left=[alb_auth_errors],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="DynamoDB throttles",
                left=[dynamodb_throttles],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="DynamoDB system errors",
                left=[dynamodb_system_errors],
                width=8,
            ),
        )

        # --- Outputs ---
        CfnOutput(
            self,
            "CloudFrontURL",
            value=f"https://{viewer_domain_name.value_as_string}",
        )
        CfnOutput(
            self,
            "CloudFrontDistributionDomain",
            value=distribution.domain_name,
        )
        CfnOutput(
            self,
            "ALBURL",
            value=f"https://{origin_domain_name.value_as_string}",
        )
        CfnOutput(
            self,
            "InternalALBDomain",
            value=fargate_service.load_balancer.load_balancer_dns_name,
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
            "ClusterName",
            value=cluster.cluster_name,
        )
        CfnOutput(
            self,
            "ServiceName",
            value=fargate_service.service.service_name,
        )
        CfnOutput(
            self,
            "TargetGroupArn",
            value=fargate_service.target_group.target_group_arn,
        )
        CfnOutput(
            self,
            "StateBackupVaultArn",
            value=backup_vault.backup_vault_arn,
        )
        CfnOutput(
            self,
            "DataKeyArn",
            value=data_key.key_arn,
        )
        CfnOutput(
            self,
            "StateTableName",
            value=state_table.table_name,
        )
        CfnOutput(
            self,
            "SelectedRuntimeStateTableName",
            value=selected_state_table_name,
        )
        recovery_cutover_mode_output = CfnOutput(
            self,
            "RecoveryCutoverModeOutput",
            value=recovery_cutover_mode.value_as_string,
        )
        recovery_cutover_mode_output.override_logical_id(
            "RecoveryCutoverMode"
        )
        CfnOutput(
            self,
            "SecurityEventOutboxQueueUrl",
            value=event_outbox_queue.queue_url,
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
        )
        CfnOutput(
            self,
            "SecurityEventLogGroupArn",
            value=security_event_log_group.log_group_arn,
        )
        CfnOutput(
            self,
            "ProviderSecretArn",
            value=api_keys_secret.secret_arn,
        )
        if scim_tenants_secret is not None:
            CfnOutput(
                self,
                "ScimTenantsSecretArn",
                value=scim_tenants_secret.secret_arn,
            )
        CfnOutput(
            self,
            "CloudFrontOriginPrefixListId",
            value=cloudfront_origin_prefix_list_id,
        )
        CfnOutput(
            self,
            "RuntimeImageUri",
            value=verified_image_uri.value_as_string,
        )
