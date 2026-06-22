"""AxonLLM Fargate stack — CloudFront + ALB + ECS Fargate + DynamoDB + Secrets."""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_iam as iam,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class AxonLLMStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Networking ---
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
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

        # --- DynamoDB tables ---
        audit_table = dynamodb.Table(
            self,
            "AuditTrail",
            table_name="axonllm-audit-trail",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        keys_table = dynamodb.Table(
            self,
            "ApiKeysTable",
            table_name="axonllm-api-keys",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        policies_table = dynamodb.Table(
            self,
            "PoliciesTable",
            table_name="axonllm-policies",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- ECS Cluster ---
        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # --- Docker image from project root ---
        image = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory="..",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # --- Fargate service with ALB ---
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=2048,
            desired_count=2,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_docker_image_asset(image),
                container_port=8000,
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
                    "AXON_AUTH_MODE": "ENFORCE",
                    "AXON_SERVER_PORT": "8000",
                },
            ),
            public_load_balancer=True,
            health_check_grace_period=Duration.seconds(60),
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

        # DynamoDB
        audit_table.grant_read_write_data(task_role)
        keys_table.grant_read_write_data(task_role)
        policies_table.grant_read_write_data(task_role)

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

        # --- CloudFront distribution ---
        distribution = cloudfront.Distribution(
            self,
            "CDN",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.LoadBalancerV2Origin(
                    fargate_service.load_balancer,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    read_timeout=Duration.seconds(60),
                    keepalive_timeout=Duration.seconds(60),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            ),
            additional_behaviors={
                "/admin/static/*": cloudfront.BehaviorOptions(
                    origin=origins.LoadBalancerV2Origin(
                        fargate_service.load_balancer,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                ),
            },
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

        # --- Outputs ---
        CfnOutput(self, "CloudFrontURL", value=f"https://{distribution.domain_name}")
        CfnOutput(self, "ALBURL", value=f"http://{fargate_service.load_balancer.load_balancer_dns_name}")
