"""Bounded ECS/Fargate compute for the AgentCore launch Activity workers."""

from __future__ import annotations

import re

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    CfnTag,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
)
from constructs import Construct


_ACCOUNT_ID = r"[0-9]{12}"
_NAMESPACE_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?$")

_ACTION_LOG_GROUP = "/aws/ecs/axonllm/launch-workers/action"
_CLEANUP_LOG_GROUP = "/aws/ecs/axonllm/launch-workers/cleanup"
_EXECUTION_ROLE_NAME = "AxonLLMLaunchWorkerExecutionRole"
_WORKER_SCRIPT = "scripts/operations/launch_activity_worker.py"
_HANDLER_MODULE = "launch_activity_domains"
_OWNER_EXPIRY_INDEX_NAME = "owner-expiry"
_TAGS = [
    CfnTag(key="Application", value="AxonLLM"),
    CfnTag(key="Environment", value="production"),
    CfnTag(key="Purpose", value="agentcore-launch-worker"),
]


class AxonLLMLaunchWorkersStack(Stack):
    """Run one action poller and one cleanup poller on imported infrastructure."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        if not isinstance(deployment_namespace, str) or (
            deployment_namespace and _NAMESPACE_PATTERN.fullmatch(deployment_namespace) is None
        ):
            raise ValueError(
                "deployment_namespace must be 1-16 lowercase letters, digits, "
                "or internal hyphens, start with a letter, and end with a letter "
                "or digit"
            )
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "us-east-1":
            raise ValueError("AxonLLM launch workers must run with the release foundation in us-east-1")

        account_pattern = _ACCOUNT_ID if Token.is_unresolved(self.account) else re.escape(self.account)
        suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        action_log_group_name = f"{_ACTION_LOG_GROUP}{suffix}"
        cleanup_log_group_name = f"{_CLEANUP_LOG_GROUP}{suffix}"

        cluster_arn = self._arn_parameter(
            "ClusterArn",
            pattern=(
                rf"^arn:aws:ecs:us-east-1:{account_pattern}:"
                r"cluster/[A-Za-z0-9_-]{1,255}$"
            ),
            description="Exact ARN of the existing ECS cluster",
        )
        private_subnet_ids = CfnParameter(
            self,
            "PrivateSubnetIds",
            type="List<AWS::EC2::Subnet::Id>",
            description=("Private subnet IDs with controlled egress for both workers"),
        )
        security_group_ids = CfnParameter(
            self,
            "SecurityGroupIds",
            type="List<AWS::EC2::SecurityGroup::Id>",
            description=("Security group IDs with only the worker egress that is required"),
        )
        action_activity_arn = self._arn_parameter(
            "ActionActivityArn",
            pattern=(
                rf"^arn:aws:states:us-east-1:{account_pattern}:"
                r"activity:axonllm-agentcore-launch-actions$"
            ),
            description="Exact Step Functions action Activity ARN",
        )
        cleanup_activity_arn = self._arn_parameter(
            "CleanupActivityArn",
            pattern=(
                rf"^arn:aws:states:us-east-1:{account_pattern}:"
                r"activity:axonllm-agentcore-launch-cleanup$"
            ),
            description="Exact Step Functions cleanup Activity ARN",
        )
        lease_table_arn = self._arn_parameter(
            "LeaseTableArn",
            pattern=(
                rf"^arn:aws:dynamodb:us-east-1:{account_pattern}:"
                r"table/axonllm-launch-rehearsal-leases$"
            ),
            description="Exact fenced launch lease-table ARN",
        )
        rehearsal_control_table_arn = self._arn_parameter(
            "RehearsalControlTableArn",
            pattern=(
                rf"^arn:aws:dynamodb:us-east-1:{account_pattern}:"
                r"table/axonllm-rehearsal-control-ledger$"
            ),
            description="Exact rehearsal-control ledger table ARN",
        )
        runtime_identity_secret_arn = self._arn_parameter(
            "RuntimeIdentitySecretArn",
            pattern=(
                rf"^arn:aws:secretsmanager:us-east-1:{account_pattern}:"
                r"secret:axonllm/launch/runtime-identity-[A-Za-z0-9]{6}$"
            ),
            description="Exact launch runtime-identity secret ARN",
        )
        qualification_mutation_broker_version_arn = self._arn_parameter(
            "QualificationMutationBrokerVersionArn",
            pattern=(
                rf"^arn:aws:lambda:us-east-1:{account_pattern}:function:"
                r"axonllm-qualification-selector-mutation-broker:[1-9][0-9]*$"
            ),
            description=(
                "Exact immutable qualification selector-mutation broker version ARN"
            ),
        )
        action_task_role_arn = self._arn_parameter(
            "ActionTaskRoleArn",
            pattern=(
                rf"^arn:aws:iam::{account_pattern}:"
                r"role/AxonLLMLaunchActionWorkerRole$"
            ),
            description="Exact pre-provisioned action worker task-role ARN",
        )
        cleanup_task_role_arn = self._arn_parameter(
            "CleanupTaskRoleArn",
            pattern=(
                rf"^arn:aws:iam::{account_pattern}:"
                r"role/AxonLLMLaunchCleanupWorkerRole$"
            ),
            description="Exact pre-provisioned cleanup worker task-role ARN",
        )
        image_repository_arn = self._arn_parameter(
            "WorkerImageRepositoryArn",
            pattern=(
                rf"^arn:aws:ecr:us-east-1:{account_pattern}:"
                r"repository/axonllm/fargate$"
            ),
            description=("Exact private ECR repository ARN containing the worker image"),
        )
        image_uri = CfnParameter(
            self,
            "WorkerImageUri",
            type="String",
            allowed_pattern=(
                rf"^{account_pattern}\.dkr\.ecr\.us-east-1\.amazonaws\.com/"
                r"axonllm/fargate@sha256:[0-9a-f]{64}$"
            ),
            constraint_description=("must be a private ECR image pinned by an exact sha256 digest"),
            description=("Immutable private ECR Fargate image URI; it must belong to WorkerImageRepositoryArn"),
        )

        log_key = self._log_key(
            log_group_names=[
                action_log_group_name,
                cleanup_log_group_name,
            ],
            pending_window_in_days=7 if deployment_namespace else 30,
        )
        action_log_group = self._log_group(
            "ActionWorkerLogGroup",
            name=action_log_group_name,
            key=log_key,
            deletion_protection_enabled=not deployment_namespace,
        )
        cleanup_log_group = self._log_group(
            "CleanupWorkerLogGroup",
            name=cleanup_log_group_name,
            key=log_key,
            deletion_protection_enabled=not deployment_namespace,
        )
        execution_role = (
            None
            if deployment_namespace
            else self._execution_role(
                role_name=_EXECUTION_ROLE_NAME,
                image_repository_arn=(image_repository_arn.value_as_string),
                log_group_names=[
                    action_log_group_name,
                    cleanup_log_group_name,
                ],
            )
        )
        execution_role_arn = (
            Fn.sub(f"arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/{_EXECUTION_ROLE_NAME}{suffix}")
            if execution_role is None
            else execution_role.attr_arn
        )

        action_task = self._task_definition(
            "ActionWorkerTaskDefinition",
            family=f"axonllm-launch-action-worker{suffix}",
            container_name=f"launch-action-worker{suffix}",
            mode="action",
            activity_arn=action_activity_arn.value_as_string,
            lease_table_arn=lease_table_arn.value_as_string,
            rehearsal_control_table_arn=rehearsal_control_table_arn.value_as_string,
            runtime_identity_secret_arn=runtime_identity_secret_arn.value_as_string,
            qualification_mutation_broker_version_arn=(
                qualification_mutation_broker_version_arn.value_as_string
            ),
            task_role_arn=action_task_role_arn.value_as_string,
            execution_role_arn=execution_role_arn,
            image_uri=image_uri.value_as_string,
            log_group_name=action_log_group_name,
            log_group=action_log_group,
        )
        cleanup_task = self._task_definition(
            "CleanupWorkerTaskDefinition",
            family=f"axonllm-launch-cleanup-worker{suffix}",
            container_name=f"launch-cleanup-worker{suffix}",
            mode="cleanup",
            activity_arn=cleanup_activity_arn.value_as_string,
            lease_table_arn=lease_table_arn.value_as_string,
            rehearsal_control_table_arn=rehearsal_control_table_arn.value_as_string,
            runtime_identity_secret_arn=runtime_identity_secret_arn.value_as_string,
            qualification_mutation_broker_version_arn=(
                qualification_mutation_broker_version_arn.value_as_string
            ),
            task_role_arn=cleanup_task_role_arn.value_as_string,
            execution_role_arn=execution_role_arn,
            image_uri=image_uri.value_as_string,
            log_group_name=cleanup_log_group_name,
            log_group=cleanup_log_group,
        )

        action_service = self._service(
            "ActionWorkerService",
            service_name=f"axonllm-launch-action-worker{suffix}",
            cluster_arn=cluster_arn.value_as_string,
            task_definition_arn=action_task.ref,
            private_subnet_ids=private_subnet_ids.value_as_list,
            security_group_ids=security_group_ids.value_as_list,
        )
        cleanup_service = self._service(
            "CleanupWorkerService",
            service_name=f"axonllm-launch-cleanup-worker{suffix}",
            cluster_arn=cluster_arn.value_as_string,
            task_definition_arn=cleanup_task.ref,
            private_subnet_ids=private_subnet_ids.value_as_list,
            security_group_ids=security_group_ids.value_as_list,
        )

        durable_resources = [
            log_key,
            action_log_group,
            cleanup_log_group,
        ]
        if deployment_namespace:
            for resource in (
                *durable_resources,
                action_task,
                cleanup_task,
                action_service,
                cleanup_service,
            ):
                resource.apply_removal_policy(RemovalPolicy.DESTROY)
        else:
            for resource in durable_resources:
                resource.apply_removal_policy(RemovalPolicy.RETAIN)

        self._outputs(
            image_uri=image_uri.value_as_string,
            image_repository_arn=(image_repository_arn.value_as_string),
            log_key=log_key,
            execution_role_arn=execution_role_arn,
            action_log_group=action_log_group,
            cleanup_log_group=cleanup_log_group,
            action_task=action_task,
            cleanup_task=cleanup_task,
            action_service=action_service,
            cleanup_service=cleanup_service,
        )

    def _arn_parameter(
        self,
        construct_id: str,
        *,
        pattern: str,
        description: str,
    ) -> CfnParameter:
        return CfnParameter(
            self,
            construct_id,
            type="String",
            allowed_pattern=pattern,
            constraint_description="must be an exact supported AWS ARN",
            description=description,
        )

    def _log_key(
        self,
        *,
        log_group_names: list[str],
        pending_window_in_days: int,
    ) -> kms.CfnKey:
        exact_log_group_arns = [
            Fn.sub(f"arn:${{AWS::Partition}}:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:{name}")
            for name in log_group_names
        ]
        key = kms.CfnKey(
            self,
            "WorkerLogKey",
            description="Encrypts AxonLLM launch worker logs",
            enable_key_rotation=True,
            pending_window_in_days=pending_window_in_days,
            key_policy={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "EnableAccountAdministration",
                        "Effect": "Allow",
                        "Principal": {"AWS": Fn.sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:root")},
                        "Action": "kms:*",
                        "Resource": "*",
                    },
                    {
                        "Sid": "EncryptOnlyExactWorkerLogGroups",
                        "Effect": "Allow",
                        "Principal": {"Service": Fn.sub("logs.${AWS::Region}.${AWS::URLSuffix}")},
                        "Action": [
                            "kms:Decrypt",
                            "kms:DescribeKey",
                            "kms:Encrypt",
                            "kms:GenerateDataKey",
                            "kms:GenerateDataKeyWithoutPlaintext",
                            "kms:ReEncryptFrom",
                            "kms:ReEncryptTo",
                        ],
                        "Resource": "*",
                        "Condition": {
                            "ArnEquals": {"kms:EncryptionContext:aws:logs:arn": (exact_log_group_arns)},
                            "StringEquals": {"kms:ViaService": Fn.sub("logs.${AWS::Region}.${AWS::URLSuffix}")},
                        },
                    },
                ],
            },
            tags=_TAGS,
        )
        return key

    def _log_group(
        self,
        construct_id: str,
        *,
        name: str,
        key: kms.CfnKey,
        deletion_protection_enabled: bool,
    ) -> logs.CfnLogGroup:
        log_group = logs.CfnLogGroup(
            self,
            construct_id,
            deletion_protection_enabled=deletion_protection_enabled,
            kms_key_id=key.attr_arn,
            log_group_class="STANDARD",
            log_group_name=name,
            retention_in_days=3653,
            tags=_TAGS,
        )
        return log_group

    def _execution_role(
        self,
        *,
        role_name: str,
        image_repository_arn: str,
        log_group_names: list[str],
    ) -> iam.CfnRole:
        log_stream_arns = [
            Fn.sub(f"arn:${{AWS::Partition}}:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:{name}:log-stream:*")
            for name in log_group_names
        ]
        return iam.CfnRole(
            self,
            "WorkerExecutionRole",
            description=("Pulls the exact launch worker repository and delivers container logs"),
            role_name=role_name,
            max_session_duration=3600,
            assume_role_policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "ArnLike": {
                                "aws:SourceArn": Fn.sub("arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:*")
                            },
                            "StringEquals": {"aws:SourceAccount": Fn.ref("AWS::AccountId")},
                        },
                    }
                ],
            },
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="LaunchWorkerImageAndLogs",
                    policy_document={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "AuthorizePrivateEcr",
                                "Effect": "Allow",
                                "Action": "ecr:GetAuthorizationToken",
                                "Resource": "*",
                            },
                            {
                                "Sid": "PullExactWorkerRepository",
                                "Effect": "Allow",
                                "Action": [
                                    "ecr:BatchCheckLayerAvailability",
                                    "ecr:BatchGetImage",
                                    "ecr:GetDownloadUrlForLayer",
                                ],
                                "Resource": image_repository_arn,
                            },
                            {
                                "Sid": "DeliverWorkerLogs",
                                "Effect": "Allow",
                                "Action": [
                                    "logs:CreateLogStream",
                                    "logs:PutLogEvents",
                                ],
                                "Resource": log_stream_arns,
                            },
                        ],
                    },
                )
            ],
            tags=_TAGS,
        )

    def _task_definition(
        self,
        construct_id: str,
        *,
        family: str,
        container_name: str,
        mode: str,
        activity_arn: str,
        lease_table_arn: str,
        rehearsal_control_table_arn: str,
        runtime_identity_secret_arn: str,
        qualification_mutation_broker_version_arn: str,
        task_role_arn: str,
        execution_role_arn: str,
        image_uri: str,
        log_group_name: str,
        log_group: logs.CfnLogGroup,
    ) -> ecs.CfnTaskDefinition:
        task = ecs.CfnTaskDefinition(
            self,
            construct_id,
            container_definitions=[
                ecs.CfnTaskDefinition.ContainerDefinitionProperty(
                    command=[
                        "python",
                        _WORKER_SCRIPT,
                        "--mode",
                        mode,
                        "--activity-arn",
                        activity_arn,
                        "--region",
                        Fn.ref("AWS::Region"),
                        "--lease-table-arn",
                        lease_table_arn,
                        "--owner-expiry-index-name",
                        _OWNER_EXPIRY_INDEX_NAME,
                        "--handler-module",
                        _HANDLER_MODULE,
                        "--poll-timeout-seconds",
                        "70",
                        "--api-timeout-seconds",
                        "8",
                        "--heartbeat-interval-seconds",
                        "20",
                        "--claim-ttl-seconds",
                        "90",
                    ],
                    environment=[
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="AWS_DEFAULT_REGION",
                            value=Fn.ref("AWS::Region"),
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="AWS_REGION",
                            value=Fn.ref("AWS::Region"),
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="AXON_LAUNCH_REHEARSAL_IDENTITY_SECRET_ARN",
                            value=runtime_identity_secret_arn,
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="AXON_LAUNCH_REHEARSAL_TABLE",
                            value=rehearsal_control_table_arn,
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name=(
                                "AXON_QUALIFICATION_MUTATION_BROKER_VERSION_ARN"
                            ),
                            value=qualification_mutation_broker_version_arn,
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="HOME",
                            value="/tmp",
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="PYTHONDONTWRITEBYTECODE",
                            value="1",
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="PYTHONUNBUFFERED",
                            value="1",
                        ),
                        ecs.CfnTaskDefinition.KeyValuePairProperty(
                            name="TMPDIR",
                            value="/tmp",
                        ),
                    ],
                    essential=True,
                    image=image_uri,
                    interactive=False,
                    linux_parameters=(
                        ecs.CfnTaskDefinition.LinuxParametersProperty(
                            capabilities=(ecs.CfnTaskDefinition.KernelCapabilitiesProperty(drop=["ALL"])),
                            init_process_enabled=True,
                        )
                    ),
                    log_configuration=(
                        ecs.CfnTaskDefinition.LogConfigurationProperty(
                            log_driver="awslogs",
                            options={
                                "awslogs-create-group": "false",
                                "awslogs-group": log_group_name,
                                "awslogs-region": Fn.ref("AWS::Region"),
                                "awslogs-stream-prefix": mode,
                            },
                        )
                    ),
                    mount_points=[
                        ecs.CfnTaskDefinition.MountPointProperty(
                            container_path="/tmp",
                            read_only=False,
                            source_volume="tmp",
                        )
                    ],
                    name=container_name,
                    privileged=False,
                    pseudo_terminal=False,
                    readonly_root_filesystem=True,
                    stop_timeout=120,
                    user="10001:10001",
                    version_consistency="enabled",
                    working_directory="/app",
                )
            ],
            cpu="256",
            enable_fault_injection=False,
            execution_role_arn=execution_role_arn,
            family=family,
            memory="512",
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            runtime_platform=ecs.CfnTaskDefinition.RuntimePlatformProperty(
                cpu_architecture="X86_64",
                operating_system_family="LINUX",
            ),
            task_role_arn=task_role_arn,
            volumes=[ecs.CfnTaskDefinition.VolumeProperty(name="tmp")],
            tags=_TAGS,
        )
        task.add_dependency(log_group)
        return task

    def _service(
        self,
        construct_id: str,
        *,
        service_name: str,
        cluster_arn: str,
        task_definition_arn: str,
        private_subnet_ids: list[str],
        security_group_ids: list[str],
    ) -> ecs.CfnService:
        return ecs.CfnService(
            self,
            construct_id,
            availability_zone_rebalancing="ENABLED",
            cluster=cluster_arn,
            deployment_configuration=(
                ecs.CfnService.DeploymentConfigurationProperty(
                    deployment_circuit_breaker=(
                        ecs.CfnService.DeploymentCircuitBreakerProperty(
                            enable=True,
                            rollback=True,
                        )
                    ),
                    maximum_percent=200,
                    minimum_healthy_percent=100,
                )
            ),
            deployment_controller=(ecs.CfnService.DeploymentControllerProperty(type="ECS")),
            desired_count=2,
            enable_ecs_managed_tags=True,
            enable_execute_command=False,
            launch_type="FARGATE",
            network_configuration=(
                ecs.CfnService.NetworkConfigurationProperty(
                    awsvpc_configuration=(
                        ecs.CfnService.AwsVpcConfigurationProperty(
                            assign_public_ip="DISABLED",
                            security_groups=security_group_ids,
                            subnets=private_subnet_ids,
                        )
                    )
                )
            ),
            platform_version="1.4.0",
            propagate_tags="TASK_DEFINITION",
            scheduling_strategy="REPLICA",
            service_name=service_name,
            task_definition=task_definition_arn,
            tags=_TAGS,
        )

    def _outputs(
        self,
        *,
        image_uri: str,
        image_repository_arn: str,
        log_key: kms.CfnKey,
        execution_role_arn: str,
        action_log_group: logs.CfnLogGroup,
        cleanup_log_group: logs.CfnLogGroup,
        action_task: ecs.CfnTaskDefinition,
        cleanup_task: ecs.CfnTaskDefinition,
        action_service: ecs.CfnService,
        cleanup_service: ecs.CfnService,
    ) -> None:
        outputs = {
            "LaunchWorkerImageUri": image_uri,
            "LaunchWorkerImageRepositoryArn": image_repository_arn,
            "LaunchWorkerLogKeyArn": log_key.attr_arn,
            "LaunchWorkerExecutionRoleArn": execution_role_arn,
            "LaunchActionWorkerLogGroupName": action_log_group.ref,
            "LaunchCleanupWorkerLogGroupName": cleanup_log_group.ref,
            "LaunchActionWorkerTaskDefinitionArn": (action_task.attr_task_definition_arn),
            "LaunchCleanupWorkerTaskDefinitionArn": (cleanup_task.attr_task_definition_arn),
            "LaunchActionWorkerServiceName": action_service.attr_name,
            "LaunchCleanupWorkerServiceName": cleanup_service.attr_name,
            "LaunchActionWorkerServiceArn": action_service.attr_service_arn,
            "LaunchCleanupWorkerServiceArn": cleanup_service.attr_service_arn,
        }
        for construct_id, value in outputs.items():
            CfnOutput(self, construct_id, value=value)
