"""Production ECS control plane sharing AgentCore's canonical authority."""

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    custom_resources as cr,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elbv2_actions,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
    aws_route53 as route53,
    aws_s3 as s3,
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

        def imported(stack_name: CfnParameter, output_name: str) -> str:
            return Fn.import_value(
                Fn.join(
                    ":",
                    [stack_name.value_as_string, output_name],
                )
            )

        data_key = kms.Key.from_key_arn(
            self,
            "AgentCoreDataKey",
            imported(agentcore_stack_name, "DataKeyArn"),
        )
        state_table = dynamodb.Table.from_table_attributes(
            self,
            "AgentCoreStateTable",
            table_name=imported(
                agentcore_stack_name,
                "StateTableName",
            ),
            encryption_key=data_key,
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
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=_DYNAMODB_STANDARD_ACTIONS,
                resources=[
                    state_table.table_arn,
                    f"{state_table.table_arn}/index/*",
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
                        state_table.table_arn,
                        f"{state_table.table_arn}/index/*",
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
                "AXON_DYNAMODB_TABLE": state_table.table_name,
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
                "AXON_ENABLED_PROVIDERS": "bedrock",
                "AXON_SERVER_PORT": "8000",
                "HOME": "/tmp",
                **query_config.environment(),
            },
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
        load_balancer.add_listener(
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

        dynamodb_endpoint.add_to_policy(
            iam.PolicyStatement(
                principals=[task_role],
                actions=_DYNAMODB_ACTIONS,
                resources=[
                    state_table.table_arn,
                    f"{state_table.table_arn}/index/*",
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
