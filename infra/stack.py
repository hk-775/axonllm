"""AxonLLM Fargate stack — CloudFront + ALB + ECS Fargate + DynamoDB + Secrets."""

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_wafv2 as wafv2,
)
from constructs import Construct


class AxonLLMStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if self.region != "us-east-1":
            raise ValueError(
                "AxonLLMStack must be deployed in us-east-1 because its "
                "CloudFront WebACL has global scope"
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

        # --- Networking ---
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
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

        # --- Secrets ---
        api_keys_secret = secretsmanager.Secret(
            self,
            "ApiKeys",
            secret_name="axonllm/api-keys",
            description="LLM provider API keys for AxonLLM",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"ANTHROPIC_API_KEY":"","OPENAI_API_KEY":""}',
                generate_string_key="placeholder",
            ),
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
            table_name=self.node.try_get_context("table_name") or "axonllm-state",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )

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

        # --- Docker image from project root ---
        image = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory="..",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        origin_certificate = acm.Certificate.from_certificate_arn(
            self,
            "OriginCertificate",
            origin_certificate_arn.value_as_string,
        )

        # --- Fargate service with ALB ---
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=2048,
            desired_count=2,
            # Named for the same reason as the cluster: `--service axonllm` and
            # `--task-definition axonllm` appear in the README's post-deploy
            # steps, and both failed against a real deployment before this.
            service_name="axonllm",
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_docker_image_asset(image),
                container_port=8000,
                family="axonllm",
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="axonllm",
                    log_retention=logs.RetentionDays.ONE_MONTH,
                ),
                secrets={
                    "ANTHROPIC_API_KEY": ecs.Secret.from_secrets_manager(
                        api_keys_secret, "ANTHROPIC_API_KEY"
                    ),
                    "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(
                        api_keys_secret, "OPENAI_API_KEY"
                    ),
                },
                environment={
                    "AWS_DEFAULT_REGION": self.region,
                    "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                    "AXON_DYNAMODB_TABLE": state_table.table_name,
                    "AXON_AUTH_MODE": "ENFORCE",
                    "AXON_SERVER_PORT": "8000",
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
            task_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[task_security_group],
            public_load_balancer=False,
            open_listener=False,
            certificate=origin_certificate,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            listener_port=443,
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
            health_check_grace_period=Duration.seconds(60),
        )

        # CloudFront VPC origins use private addresses in this VPC. The ALB is
        # internal and its listener is not opened to the internet.
        fargate_service.load_balancer.connections.allow_from(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "CloudFront VPC origin traffic",
        )

        # --- Health check ---
        fargate_service.target_group.configure_health_check(
            path="/health",
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

        # Bedrock (runtime + mantle)
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:InvokeEndpoint",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                    "bedrock-mantle:*",
                ],
                resources=["*"],
            )
        )

        # DynamoDB — single state table (matches AXON_DYNAMODB_TABLE)
        state_table.grant_read_write_data(task_role)

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
