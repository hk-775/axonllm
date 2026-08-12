"""Production ECS control plane sharing AgentCore's canonical authority."""

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
    custom_resources as cr,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elbv2_actions,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sqs as sqs,
)
from constructs import Construct

from agentcore_stack import load_athena_infrastructure_config


_DYNAMODB_STANDARD_ACTIONS = [
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
_DYNAMODB_TRANSACTION_ACTIONS = ["dynamodb:TransactWriteItems"]
_DYNAMODB_ACTIONS = [
    *_DYNAMODB_STANDARD_ACTIONS,
    *_DYNAMODB_TRANSACTION_ACTIONS,
]
_SQS_ACTIONS = [
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
]
_ECR_PULL_ACTIONS = [
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
]


_CONTROL_PLANE_RECOVERY_GUARD = """\
import boto3


cloudformation = boto3.client("cloudformation")
ecs = boto3.client("ecs")
autoscaling = boto3.client("application-autoscaling")

_PHYSICAL_ID = "AxonLLMControlPlaneRecoveryGuard"
_BLOCKED_MODES = {"quiesced", "selected"}
_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)


def _stack_outputs(stack_name):
    response = cloudformation.describe_stacks(StackName=stack_name)
    stacks = response.get("Stacks", [])
    if len(stacks) != 1:
        raise RuntimeError(
            f"recovery guard could not resolve stack {stack_name}"
        )
    stack = stacks[0]
    outputs = {
        item.get("OutputKey"): item.get("OutputValue")
        for item in stack.get("Outputs", [])
    }
    return stack, outputs


def _assert_table_namespace(primary, selected):
    if selected == primary:
        return
    if not selected.startswith(f"{primary}-restore-validation-"):
        raise RuntimeError(
            "control-plane recovery table is outside the AgentCore "
            "restore-validation namespace"
        )


def _agentcore_state(properties):
    stack, outputs = _stack_outputs(properties["AgentCoreStackName"])
    required = {
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "SelectedRuntimeStateTableName",
        "StateTableName",
    }
    missing = sorted(required.difference(outputs))
    if missing:
        raise RuntimeError(
            "AgentCore stack is missing recovery outputs: "
            + ", ".join(missing)
        )
    if outputs["StateTableName"] != properties["PrimaryTable"]:
        raise RuntimeError(
            "control-plane primary table is not owned by the selected "
            "AgentCore stack"
        )
    return stack, outputs


def _assert_agentcore(outputs, *, mode, selected, approval):
    actual = (
        outputs["RecoveryCutoverMode"],
        outputs["SelectedRuntimeStateTableName"],
        outputs["RecoveryApprovalId"],
    )
    expected = (mode, selected, approval)
    if actual != expected:
        raise RuntimeError(
            "AgentCore recovery state does not authorize this control-plane "
            f"transition: expected {expected}, found {actual}"
        )


def _assert_control_plane_quiesced(stack_name):
    _, outputs = _stack_outputs(stack_name)
    cluster_name = outputs.get("ClusterName")
    service_name = outputs.get("ServiceName")
    if not cluster_name or not service_name:
        raise RuntimeError(
            "control-plane stack is missing ClusterName or ServiceName"
        )
    resource_id = f"service/{cluster_name}/{service_name}"
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
        cluster=cluster_name,
        services=[service_name],
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


def _result():
    return {"PhysicalResourceId": _PHYSICAL_ID}


def handler(event, _context):
    if event["RequestType"] == "Delete":
        return _result()

    current = event["ResourceProperties"]
    mode = current.get("Mode")
    selected = current.get("SelectedTable", "")
    primary = current.get("PrimaryTable", "")
    approval = current.get("ApprovalId", "")
    if not primary or not selected:
        raise RuntimeError("control-plane recovery table ownership is missing")
    _assert_table_namespace(primary, selected)
    _, agentcore = _agentcore_state(current)

    if event["RequestType"] == "Create":
        if mode != "normal":
            raise RuntimeError(
                "a new control-plane stack must start in normal mode"
            )
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
        return _result()

    previous = event.get("OldResourceProperties", {})
    for immutable in (
        "AgentCoreStackName",
        "ControlPlaneStackName",
        "PrimaryTable",
    ):
        if current.get(immutable) != previous.get(immutable):
            raise RuntimeError(
                f"control-plane recovery ownership changed: {immutable}"
            )

    old_mode = previous.get("Mode")
    old_selected = previous.get("SelectedTable", "")
    old_approval = previous.get("ApprovalId", "")
    transition = (old_mode, mode)
    allowed = {
        ("normal", "normal"),
        ("normal", "quiesced"),
        ("quiesced", "quiesced"),
        ("quiesced", "normal"),
        ("quiesced", "selected"),
        ("selected", "selected"),
        ("selected", "quiesced"),
        ("selected", "normal"),
    }
    if transition not in allowed:
        raise RuntimeError(
            "unsupported control-plane recovery transition: "
            f"{old_mode} -> {mode}"
        )

    table_changed = selected != old_selected
    if table_changed and transition not in {
        ("quiesced", "selected"),
        ("selected", "quiesced"),
    }:
        raise RuntimeError(
            "control-plane table changes require a blocked "
            "quiesced <-> selected transition"
        )
    if transition in {
        ("quiesced", "selected"),
        ("selected", "quiesced"),
    } and not table_changed:
        raise RuntimeError(
            "control-plane selection transition requires a table change"
        )

    if transition == ("normal", "quiesced"):
        if not approval or approval == old_approval:
            raise RuntimeError(
                "entering control-plane recovery requires a new approval ID"
            )
    elif mode in _BLOCKED_MODES and approval != old_approval:
        raise RuntimeError(
            "control-plane recovery approval changed during a blocked phase"
        )
    elif transition == ("selected", "normal") and approval != old_approval:
        raise RuntimeError(
            "control-plane promotion changed its recovery approval"
        )

    if mode in _BLOCKED_MODES or old_mode in _BLOCKED_MODES:
        _assert_control_plane_quiesced(current["ControlPlaneStackName"])

    if transition == ("normal", "quiesced"):
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=old_selected,
            approval=old_approval,
        )
    elif transition == ("quiesced", "selected"):
        _assert_agentcore(
            agentcore,
            mode="quiesced",
            selected=old_selected,
            approval=approval,
        )
    elif transition == ("selected", "quiesced"):
        _assert_agentcore(
            agentcore,
            mode="quiesced",
            selected=selected,
            approval=approval,
        )
    elif transition == ("quiesced", "normal"):
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
    elif transition == ("selected", "normal"):
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
    elif mode == "normal":
        _assert_agentcore(
            agentcore,
            mode="normal",
            selected=selected,
            approval=approval,
        )
    elif mode == "quiesced":
        _assert_agentcore(
            agentcore,
            mode="quiesced",
            selected=selected,
            approval=approval,
        )
    else:
        _assert_agentcore(
            agentcore,
            mode="selected",
            selected=selected,
            approval=approval,
        )
    return _result()
"""


class AxonLLMControlPlaneStack(Stack):
    """Private Fargate control plane backed by AgentCore-owned state."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        query_config = load_athena_infrastructure_config(self)
        secret_arn_pattern = re.compile(
            rf"^arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:"
            rf"{re.escape(self.region)}:[0-9]{{12}}:"
            r"secret:[A-Za-z0-9/_+=.@-]{1,512}$"
        )

        def secret_context(name: str) -> str | None:
            value = self.node.try_get_context(name)
            if value in (None, ""):
                return None
            if (
                not isinstance(value, str)
                or secret_arn_pattern.fullmatch(value) is None
            ):
                raise ValueError(
                    f"{name} must be a complete Secrets Manager ARN in "
                    f"{self.region}"
                )
            return value

        scim_tenants_secret_arn = secret_context(
            "scim_tenants_secret_arn"
        )

        agentcore_stack_name = CfnParameter(
            self,
            "AgentCoreStackName",
            type="String",
            default="AxonLLMAgentCoreStack",
            min_length=1,
            max_length=128,
            allowed_pattern=r"^[A-Za-z][A-Za-z0-9-]*$",
            description=(
                "Deployed AgentCore stack exporting canonical state and audit "
                "resources"
            ),
        )
        identity_stack_name = CfnParameter(
            self,
            "IdentityStackName",
            type="String",
            default="AxonLLMIdentityStack",
            min_length=1,
            max_length=128,
            allowed_pattern=r"^[A-Za-z][A-Za-z0-9-]*$",
            description=(
                "Deployed AxonLLM identity stack exporting the ALB client"
            ),
        )
        certificate_arn = CfnParameter(
            self,
            "CertificateArn",
            type="String",
            allowed_pattern=(
                r"^arn:(?:aws|aws-us-gov|aws-cn):acm:[a-z0-9-]+:"
                r"[0-9]{12}:certificate/[0-9a-fA-F-]+$"
            ),
            description=(
                "Regional ACM certificate ARN for the control-plane HTTPS "
                "listener"
            ),
        )
        approved_ingress_prefix_list_id = CfnParameter(
            self,
            "ApprovedIngressPrefixListId",
            type="String",
            allowed_pattern=r"^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=(
                "Managed prefix list containing approved control-plane clients"
            ),
        )
        approved_https_prefix_list_id = CfnParameter(
            self,
            "ApprovedHttpsPrefixListId",
            type="String",
            allowed_pattern=r"^pl-[0-9a-fA-F]+$",
            constraint_description="must be an EC2 managed prefix list ID",
            description=(
                "Managed prefix list containing Cognito, ALB key, and other "
                "approved HTTPS destinations"
            ),
        )
        public_hosted_zone_id = CfnParameter(
            self,
            "PublicHostedZoneId",
            type="String",
            allowed_pattern=r"^Z[A-Z0-9]+$",
            constraint_description=(
                "must be a Route 53 public hosted-zone ID"
            ),
            description=(
                "Public hosted-zone ID containing the identity stack's "
                "control-plane hostname"
            ),
        )
        saml_login_path = CfnParameter(
            self,
            "SamlLoginPath",
            type="String",
            default="/admin/dashboard",
            min_length=2,
            max_length=2048,
            allowed_pattern=(
                r"^/(?!/)(?!$)(?!.*//)(?!.*[/]\.{1,2}(?:/|$))"
                r"(?!(?:[Ss][Aa][Mm][Ll]|[Ss][Cc][Ii][Mm]|"
                r"[Oo][Aa][Uu][Tt][Hh]2)(?:/|$))"
                r"(?!(?:[Hh][Ee][Aa][Ll][Tt][Hh]|"
                r"[Rr][Ee][Aa][Dd][Yy])$)"
                r"[A-Za-z0-9._~!$&'()*+,;=:@/-]+$"
            ),
            constraint_description=(
                "must be a protected application-local path without a "
                "scheme, authority, query, fragment, encoding, empty or dot "
                "segments, or SAML, SCIM, OAuth, health, or readiness targets"
            ),
            description=(
                "Protected local route used after the ALB and Cognito start "
                "managed enterprise login"
            ),
        )
        verified_image_uri = CfnParameter(
            self,
            "ControlPlaneVerifiedImageUri",
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
                "Immutable x86_64 AxonLLM server image emitted by control-plane "
                "release verification; this is distinct from the ARM64 "
                "AgentCore image"
            ),
        )
        runtime_state_table_name = CfnParameter(
            self,
            "RuntimeStateTableName",
            type="String",
            default="",
            min_length=0,
            max_length=255,
            allowed_pattern=r"^$|^[A-Za-z0-9_.-]{3,255}$",
            constraint_description=(
                "must be blank or a valid DynamoDB table name; the recovery "
                "guard enforces AgentCore ownership and restore namespace"
            ),
            description=(
                "Optional restored AgentCore table selected only through the "
                "coordinated recovery workflow"
            ),
        )
        primary_state_table_name_parameter = CfnParameter(
            self,
            "PrimaryStateTableName",
            type="String",
            min_length=3,
            max_length=255,
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,255}$",
            constraint_description="must be a valid DynamoDB table name",
            description=(
                "Primary table name read from the verified AgentCore stack "
                "outputs by the deployment wrapper"
            ),
        )
        recovery_cutover_mode = CfnParameter(
            self,
            "RecoveryCutoverMode",
            type="String",
            default="normal",
            allowed_values=["normal", "quiesced", "selected"],
            description=(
                "Control-plane recovery phase; table changes are accepted "
                "only while every task and scaling path is stopped"
            ),
        )
        recovery_approval_id = CfnParameter(
            self,
            "RecoveryApprovalId",
            type="String",
            default="",
            max_length=128,
            allowed_pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$",
            constraint_description=(
                "must be blank or a 3-128 character change/incident ID"
            ),
            description=(
                "Reviewed change or incident identifier shared with the "
                "AgentCore recovery selector"
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
        recovery_normal = CfnCondition(
            self,
            "RecoveryNormal",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "normal",
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
        recovery_access_blocked = CfnCondition(
            self,
            "RecoveryAccessBlocked",
            expression=Fn.condition_or(
                recovery_quiesced,
                recovery_selected,
            ),
        )

        def imported(stack_name: CfnParameter, output_name: str) -> str:
            return Fn.import_value(
                Fn.join(
                    ":",
                    [stack_name.value_as_string, output_name],
                )
            )

        primary_state_table_name = (
            primary_state_table_name_parameter.value_as_string
        )
        selected_state_table_name = Token.as_string(
            Fn.condition_if(
                use_recovered_state.logical_id,
                runtime_state_table_name.value_as_string,
                primary_state_table_name,
            )
        )
        selected_state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name,
        )
        data_key = kms.Key.from_key_arn(
            self,
            "AgentCoreDataKey",
            imported(agentcore_stack_name, "DataKeyArn"),
        )
        scim_tenants_secret = (
            secretsmanager.Secret.from_secret_complete_arn(
                self,
                "ScimTenantsSecret",
                scim_tenants_secret_arn,
            )
            if scim_tenants_secret_arn is not None
            else None
        )
        event_outbox_queue = sqs.Queue.from_queue_attributes(
            self,
            "AgentCoreEventOutbox",
            queue_arn=imported(
                agentcore_stack_name,
                "SecurityEventOutboxQueueArn",
            ),
            queue_url=imported(
                agentcore_stack_name,
                "SecurityEventOutboxQueueUrl",
            ),
            key_arn=data_key.key_arn,
            fifo=True,
        )
        security_event_topic = sns.Topic.from_topic_arn(
            self,
            "AgentCoreSecurityEventTopic",
            imported(
                agentcore_stack_name,
                "SecurityEventTopicArn",
            ),
        )
        security_event_log_group_arn = imported(
            agentcore_stack_name,
            "SecurityEventLogGroupArn",
        )
        security_event_log_group = logs.LogGroup.from_log_group_arn(
            self,
            "AgentCoreSecurityEventLogGroup",
            security_event_log_group_arn,
        )

        user_pool = cognito.UserPool.from_user_pool_arn(
            self,
            "IdentityUserPool",
            imported(identity_stack_name, "UserPoolArn"),
        )
        alb_client = cognito.UserPoolClient.from_user_pool_client_id(
            self,
            "IdentityAlbClient",
            imported(identity_stack_name, "AlbClientId"),
        )
        user_pool_domain = cognito.UserPoolDomain.from_domain_name(
            self,
            "IdentityHostedUiDomain",
            imported(identity_stack_name, "HostedUiDomainName"),
        )
        oidc_issuer = imported(identity_stack_name, "OidcIssuer")
        tenant_claim = imported(identity_stack_name, "TenantClaimName")
        project_claim = imported(identity_stack_name, "ProjectClaimName")
        control_plane_domain_name = imported(
            identity_stack_name,
            "ControlPlaneDomainName",
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
        image_repository_arn = self.format_arn(
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
                    name="Control",
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
            description="AxonLLM control-plane tasks",
        )
        alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="AxonLLM control-plane HTTPS ALB",
        )
        endpoint_security_group = ec2.SecurityGroup(
            self,
            "EndpointSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="Private AWS endpoints for the AxonLLM control plane",
        )

        for security_group in (task_security_group, alb_security_group):
            security_group.add_egress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.udp(53),
                "DNS to the VPC resolver",
            )
            security_group.add_egress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.tcp(53),
                "DNS fallback to the VPC resolver",
            )
        task_security_group.add_ingress_rule(
            alb_security_group,
            ec2.Port.tcp(8000),
            "Application traffic from the control-plane ALB",
        )
        alb_security_group.add_ingress_rule(
            ec2.Peer.prefix_list(
                approved_ingress_prefix_list_id.value_as_string
            ),
            ec2.Port.tcp(443),
            "HTTPS from approved control-plane clients",
        )
        alb_security_group.add_egress_rule(
            task_security_group,
            ec2.Port.tcp(8000),
            "Application traffic to control-plane tasks",
        )
        alb_security_group.add_egress_rule(
            ec2.Peer.prefix_list(
                approved_https_prefix_list_id.value_as_string
            ),
            ec2.Port.tcp(443),
            "HTTPS to Cognito and approved authentication destinations",
        )
        endpoint_security_group.add_ingress_rule(
            task_security_group,
            ec2.Port.tcp(443),
            "HTTPS from control-plane tasks",
        )
        task_security_group.add_egress_rule(
            endpoint_security_group,
            ec2.Port.tcp(443),
            "AWS services through private interface endpoints",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(
                approved_https_prefix_list_id.value_as_string
            ),
            ec2.Port.tcp(443),
            "HTTPS to approved authentication destinations",
        )

        def managed_prefix_list(
            construct_id: str,
            service_name: str,
        ) -> str:
            lookup_logs = logs.LogGroup(
                self,
                f"{construct_id}Logs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            lookup = cr.AwsCustomResource(
                self,
                construct_id,
                on_create=cr.AwsSdkCall(
                    service="EC2",
                    action="describeManagedPrefixLists",
                    parameters={
                        "Filters": [
                            {
                                "Name": "prefix-list-name",
                                "Values": [
                                    (
                                        f"com.amazonaws.{self.region}."
                                        f"{service_name}"
                                    )
                                ],
                            }
                        ]
                    },
                    output_paths=["PrefixLists.0.PrefixListId"],
                    physical_resource_id=(
                        cr.PhysicalResourceId.from_response(
                            "PrefixLists.0.PrefixListId"
                        )
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
                log_group=lookup_logs,
                timeout=Duration.seconds(30),
            )
            return lookup.get_response_field(
                "PrefixLists.0.PrefixListId"
            )

        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(
                managed_prefix_list(
                    "DynamoDbPrefixList",
                    "dynamodb",
                )
            ),
            ec2.Port.tcp(443),
            "DynamoDB through the VPC gateway endpoint",
        )
        task_security_group.add_egress_rule(
            ec2.Peer.prefix_list(
                managed_prefix_list("S3PrefixList", "s3")
            ),
            ec2.Port.tcp(443),
            "ECR image layers through the S3 gateway endpoint",
        )

        private_subnets = ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        )
        dynamodb_endpoint = vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[private_subnets],
        )
        s3_endpoint = vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[private_subnets],
        )
        sqs_endpoint = vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        sns_endpoint = vpc.add_interface_endpoint(
            "SnsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SNS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        logs_endpoint = vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        ecr_api_endpoint = vpc.add_interface_endpoint(
            "EcrApiEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        ecr_docker_endpoint = vpc.add_interface_endpoint(
            "EcrDockerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            open=False,
            private_dns_enabled=True,
            security_groups=[endpoint_security_group],
            subnets=private_subnets,
        )
        configured_secrets = (
            [scim_tenants_secret]
            if scim_tenants_secret is not None
            else []
        )
        secrets_endpoint = None
        if configured_secrets:
            secrets_endpoint = vpc.add_interface_endpoint(
                "SecretsManagerEndpoint",
                service=(
                    ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER
                ),
                open=False,
                private_dns_enabled=True,
                security_groups=[endpoint_security_group],
                subnets=private_subnets,
            )
        ecs_trust = iam.ServicePrincipal(
            "ecs-tasks.amazonaws.com",
            conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {
                    "aws:SourceArn": (
                        f"arn:{self.partition}:ecs:{self.region}:"
                        f"{self.account}:*"
                    )
                },
            },
        )
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=ecs_trust,
            description=(
                "Least-privilege AxonLLM control-plane application role"
            ),
        )
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=ecs_trust,
            description=(
                "Pulls the verified control-plane image and writes logs"
            ),
        )
        for secret in configured_secrets:
            secret.grant_read(execution_role)
        if secrets_endpoint is not None:
            secrets_endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[execution_role],
                    actions=[
                        "secretsmanager:DescribeSecret",
                        "secretsmanager:GetSecretValue",
                    ],
                    resources=[
                        secret.secret_arn
                        for secret in configured_secrets
                    ],
                )
            )
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=_DYNAMODB_STANDARD_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        transaction_policy = iam.Policy(
            self,
            "TaskDynamoTransactionPolicy",
            statements=[
                iam.PolicyStatement(
                    actions=_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[
                        selected_state_table_arn,
                        f"{selected_state_table_arn}/index/*",
                    ],
                )
            ],
        )
        task_role.attach_inline_policy(transaction_policy)
        cfn_transaction_policy = transaction_policy.node.default_child
        if not isinstance(cfn_transaction_policy, iam.CfnPolicy):
            raise RuntimeError(
                "DynamoDB transaction policy did not synthesize"
            )
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_transaction_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )
        recovery_deny_resource = Token.as_string(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                "*",
                self.format_arn(
                    service="dynamodb",
                    resource="table",
                    resource_name=(
                        "__axonllm_control_recovery_access_not_blocked__"
                    ),
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
                    actions=_DYNAMODB_STANDARD_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_deny_policy.attach_to_role(task_role)
        cfn_recovery_deny_policy = recovery_deny_policy.node.default_child
        if not isinstance(cfn_recovery_deny_policy, iam.CfnPolicy):
            raise RuntimeError(
                "control-plane recovery deny policy did not synthesize"
            )
        recovery_transaction_deny_policy = iam.Policy(
            self,
            "RecoveryStateTransactionAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateTransactionsDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_transaction_deny_policy.attach_to_role(task_role)
        cfn_recovery_transaction_deny_policy = (
            recovery_transaction_deny_policy.node.default_child
        )
        if not isinstance(
            cfn_recovery_transaction_deny_policy,
            iam.CfnPolicy,
        ):
            raise RuntimeError(
                "control-plane recovery transaction deny policy did not "
                "synthesize"
            )
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_recovery_transaction_deny_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )
        event_outbox_queue.grant_send_messages(task_role)
        event_outbox_queue.grant_consume_messages(task_role)
        security_event_topic.grant_publish(task_role)
        security_event_log_group.grant_write(task_role)
        data_key.grant_encrypt_decrypt(task_role)
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=_ECR_PULL_ACTIONS,
                resources=[image_repository_arn],
            )
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        application_logs = logs.LogGroup(
            self,
            "ApplicationLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        application_logs.grant_write(execution_role)

        task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            cpu=1024,
            memory_limit_mib=2048,
            execution_role=execution_role,
            task_role=task_role,
            family="axonllm-control-plane",
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_definition.add_volume(name="tmp")
        linux_parameters = ecs.LinuxParameters(
            self,
            "LinuxParameters",
            init_process_enabled=True,
        )
        linux_parameters.drop_capabilities(ecs.Capability.ALL)
        container_secrets: dict[str, ecs.Secret] = {}
        if scim_tenants_secret is not None:
            container_secrets["AXON_SCIM_TENANTS"] = (
                ecs.Secret.from_secrets_manager(scim_tenants_secret)
            )
        container = task_definition.add_container(
            "Application",
            image=ecs.ContainerImage.from_registry(
                verified_image_uri.value_as_string
            ),
            logging=ecs.LogDrivers.aws_logs(
                log_group=application_logs,
                stream_prefix="control-plane",
            ),
            environment={
                "AWS_DEFAULT_REGION": self.region,
                "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                "AXON_AWS_ACCOUNT_ID": self.account,
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                "AXON_DYNAMODB_TABLE": selected_state_table_name,
                "AXON_EVENT_OUTBOX_QUEUE_URL": event_outbox_queue.queue_url,
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (
                    security_event_topic.topic_arn
                ),
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (
                    security_event_log_group_arn
                ),
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_OIDC_ISSUER": oidc_issuer,
                "AXON_OIDC_AUDIENCE": alb_client.user_pool_client_id,
                "AXON_OIDC_TENANT_CLAIM": tenant_claim,
                "AXON_OIDC_PROJECT_CLAIM": project_claim,
                "AXON_ALB_CLIENT_ID": alb_client.user_pool_client_id,
                "AXON_ALB_ISSUER": (
                    "https://public-keys.auth.elb."
                    f"{self.region}.amazonaws.com"
                ),
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_CONTROL_PLANE_ONLY": "true",
                "AXON_SAML_FEDERATION_MODE": "managed-cognito",
                "AXON_SAML_LOGIN_PATH": (
                    saml_login_path.value_as_string
                ),
                "AXON_ENABLED_PROVIDERS": "bedrock",
                "AXON_SERVER_PORT": "8000",
                "HOME": "/tmp",
                **query_config.environment(),
            },
            secrets=container_secrets,
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    (
                        "python -c \"import urllib.request;"
                        "urllib.request.urlopen("
                        "'http://127.0.0.1:8000/ready',timeout=3)\""
                    ),
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(60),
            ),
            linux_parameters=linux_parameters,
            readonly_root_filesystem=True,
            stop_timeout=Duration.seconds(30),
        )
        container.add_port_mappings(
            ecs.PortMapping(
                container_port=8000,
                protocol=ecs.Protocol.TCP,
            )
        )
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/tmp",
                source_volume="tmp",
                read_only=False,
            )
        )

        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=2,
            assign_public_ip=False,
            vpc_subnets=private_subnets,
            security_groups=[task_security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
            health_check_grace_period=Duration.seconds(90),
            enable_execute_command=False,
        )

        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            ),
            security_group=alb_security_group,
            deletion_protection=True,
            desync_mitigation_mode=elbv2.DesyncMitigationMode.STRICTEST,
            drop_invalid_header_fields=True,
        )
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    enabled=True,
                    expiration=Duration.days(365),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        load_balancer.log_access_logs(
            access_logs_bucket,
            prefix="alb",
        )
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "TargetGroup",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path="/ready",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        service.attach_to_application_target_group(target_group)
        certificate = acm.Certificate.from_certificate_arn(
            self,
            "Certificate",
            certificate_arn.value_as_string,
        )
        https_listener = load_balancer.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
            open=False,
            default_action=elbv2_actions.AuthenticateCognitoAction(
                user_pool=user_pool,
                user_pool_client=alb_client,
                user_pool_domain=user_pool_domain,
                allow_https_outbound=False,
                on_unauthenticated_request=(
                    elbv2.UnauthenticatedAction.AUTHENTICATE
                ),
                scope="openid email profile",
                session_cookie_name="AxonLLMControlPlaneSession",
                session_timeout=Duration.hours(1),
                next=elbv2.ListenerAction.forward([target_group]),
            ),
        )
        https_listener.add_action(
            "SelfAuthenticatedProtocols",
            priority=10,
            conditions=[
                elbv2.ListenerCondition.path_patterns(
                    ["/scim/*"]
                )
            ],
            action=elbv2.ListenerAction.forward([target_group]),
        )
        container.add_environment(
            "AXON_ALB_SIGNER_ARN",
            load_balancer.load_balancer_arn,
        )
        route53.CfnRecordSet(
            self,
            "ControlPlaneAlias",
            hosted_zone_id=public_hosted_zone_id.value_as_string,
            name=control_plane_domain_name,
            type="A",
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=Fn.join(
                    "",
                    [
                        "dualstack.",
                        load_balancer.load_balancer_dns_name,
                    ],
                ),
                hosted_zone_id=(
                    load_balancer
                    .load_balancer_canonical_hosted_zone_id
                ),
                evaluate_target_health=True,
            ),
        )

        scaling = service.auto_scale_task_count(
            min_capacity=2,
            max_capacity=6,
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=60,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1),
        )
        scaling.scale_on_memory_utilization(
            "MemoryScaling",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1),
        )

        recovery_guard_handler_logs = logs.LogGroup(
            self,
            "RecoveryGuardHandlerLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
        )
        recovery_guard_handler = lambda_.Function(
            self,
            "RecoveryGuardHandler",
            description=(
                "Blocks unsafe control-plane DynamoDB recovery transitions"
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                _CONTROL_PLANE_RECOVERY_GUARD
            ),
            timeout=Duration.seconds(60),
            log_group=recovery_guard_handler_logs,
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=Fn.join(
                            "",
                            [
                                agentcore_stack_name.value_as_string,
                                "/*",
                            ],
                        ),
                    ),
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name="AxonLLMControlPlaneStack/*",
                    ),
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
                actions=[
                    "application-autoscaling:DescribeScalableTargets"
                ],
                resources=["*"],
            )
        )
        recovery_guard_provider_logs = logs.LogGroup(
            self,
            "RecoveryGuardProviderLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.RETAIN,
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
                "AgentCoreStackName": (
                    agentcore_stack_name.value_as_string
                ),
                "ApprovalId": recovery_approval_id.value_as_string,
                "ControlPlaneStackName": "AxonLLMControlPlaneStack",
                "Mode": recovery_cutover_mode.value_as_string,
                "PrimaryTable": primary_state_table_name,
                "SelectedTable": selected_state_table_name,
            },
        )
        recovery_guard_resource = recovery_guard.node.default_child
        if not isinstance(recovery_guard_resource, CfnResource):
            raise RuntimeError(
                "control-plane recovery guard did not synthesize"
            )

        cfn_service = service.node.default_child
        if not isinstance(cfn_service, ecs.CfnService):
            raise RuntimeError("control-plane service did not synthesize")
        cfn_service.add_override(
            "Properties.DesiredCount",
            Fn.condition_if(
                recovery_normal.logical_id,
                2,
                0,
            ),
        )
        cfn_service.add_dependency(recovery_guard_resource)

        scaling_targets = [
            child
            for child in scaling.node.find_all()
            if isinstance(child, CfnResource)
            and child.cfn_resource_type
            == "AWS::ApplicationAutoScaling::ScalableTarget"
        ]
        if len(scaling_targets) != 1:
            raise RuntimeError(
                "control-plane scalable target did not synthesize"
            )
        cfn_scaling_target = scaling_targets[0]
        cfn_scaling_target.add_override(
            "Properties.MinCapacity",
            Fn.condition_if(
                recovery_normal.logical_id,
                2,
                0,
            ),
        )
        cfn_scaling_target.add_override(
            "Properties.SuspendedState",
            Fn.condition_if(
                recovery_normal.logical_id,
                {
                    key: False
                    for key in (
                        "DynamicScalingInSuspended",
                        "DynamicScalingOutSuspended",
                        "ScheduledScalingSuspended",
                    )
                },
                {
                    key: True
                    for key in (
                        "DynamicScalingInSuspended",
                        "DynamicScalingOutSuspended",
                        "ScheduledScalingSuspended",
                    )
                },
            ),
        )
        cfn_scaling_target.add_dependency(recovery_guard_resource)
        cfn_recovery_deny_policy.add_dependency(
            recovery_guard_resource
        )
        cfn_recovery_transaction_deny_policy.add_dependency(
            recovery_guard_resource
        )

        dynamodb_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=_DYNAMODB_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        s3_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[execution_role],
                actions=["s3:GetObject"],
                resources=[
                    (
                        f"arn:{self.partition}:s3:::"
                        f"prod-{self.region}-starport-layer-bucket/*"
                    )
                ],
            )
        )
        sqs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=_SQS_ACTIONS,
                resources=[event_outbox_queue.queue_arn],
            )
        )
        sns_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        logs_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role, execution_role],
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    application_logs.log_group_arn,
                    f"{application_logs.log_group_arn}:*",
                    security_event_log_group_arn,
                    f"{security_event_log_group_arn}:*",
                ],
            )
        )
        for endpoint in (ecr_api_endpoint, ecr_docker_endpoint):
            endpoint.add_to_policy(
                iam.PolicyStatement(
                    principals=[execution_role],
                    actions=_ECR_PULL_ACTIONS,
                    resources=[image_repository_arn],
                )
            )
        ecr_api_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[execution_role],
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        CfnOutput(
            self,
            "AgentCoreStackNameOutput",
            value=agentcore_stack_name.value_as_string,
        ).override_logical_id("AgentCoreStackName")
        CfnOutput(
            self,
            "PrimaryStateTableNameOutput",
            value=primary_state_table_name,
        ).override_logical_id("PrimaryStateTableName")
        CfnOutput(
            self,
            "SelectedRuntimeStateTableName",
            value=selected_state_table_name,
        )
        CfnOutput(
            self,
            "RecoveryCutoverModeOutput",
            value=recovery_cutover_mode.value_as_string,
        ).override_logical_id("RecoveryCutoverMode")
        CfnOutput(
            self,
            "RecoveryApprovalIdOutput",
            value=recovery_approval_id.value_as_string,
        ).override_logical_id("RecoveryApprovalId")
        CfnOutput(
            self,
            "LoadBalancerDnsName",
            value=load_balancer.load_balancer_dns_name,
        )
        CfnOutput(
            self,
            "ClusterName",
            value=cluster.cluster_name,
        )
        CfnOutput(
            self,
            "ServiceName",
            value=service.service_name,
        )
        CfnOutput(
            self,
            "QueryPlaneEnabled",
            value="true" if query_config.enabled else "false",
        )
        if scim_tenants_secret is not None:
            CfnOutput(
                self,
                "ScimTenantsSecretArn",
                value=scim_tenants_secret.secret_arn,
            )
