"""Private release registries and GitHub OIDC roles for AxonLLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    CfnParameter,
    CfnTag,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_dynamodb as dynamodb,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_sqs as sqs,
    aws_stepfunctions as stepfunctions,
)
from constructs import Construct


_GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"
_DEFAULT_GITHUB_SUBJECT_PREFIX = "repo:AxonLLM@313590914/axonllm@1276398779"
_GITHUB_SUBJECT_PREFIX_PATTERN = r"^repo:[A-Za-z0-9_.-]+@[0-9]+/[A-Za-z0-9_.-]+@[0-9]+$"
_SECRET_ARN_PATTERN = (
    r"^arn:aws:secretsmanager:us-east-1:[0-9]{12}:secret:"
    r"[A-Za-z0-9/_+=.@-]{1,512}$"
)
_KMS_KEY_ARN_PATTERN = (
    r"^arn:aws:kms:us-east-1:[0-9]{12}:key/"
    r"[0-9a-fA-F-]{36}$"
)
_QUALIFICATION_NAMESPACE = "managed"
_PRODUCTION_CDK_QUALIFIER = "axprod"
_QUALIFICATION_CDK_QUALIFIER = "axqual"
_CDK_EXECUTION_POLICY_PART_COUNT = 3
_EXTERNAL_CDK_QUALIFIER = "axext"
_OWNER_EXPIRY_INDEX_NAME = "owner-expiry"
_REHEARSAL_EVIDENCE_PREFIX = "agentcore-production/rehearsal"
_QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX = "agentcore-production/qualification-teardown"
_TRANSITION_EVIDENCE_PREFIX = "agentcore-production/transitions"
_EXTERNAL_OIDC_EVIDENCE_PREFIX = "agentcore-external-oidc"
_EVIDENCE_RETENTION_DAYS = 2555
_LAUNCH_RUNTIME_IDENTITY_SECRET_NAME = "axonllm/launch/runtime-identity"
_LAUNCH_ACTION_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-action/v1"
_LAUNCH_MAINTENANCE_SCHEMA = "axonllm.agentcore-launch-rehearsal-maintenance/v1"
_LAUNCH_STATE_MACHINE_NAME = "AxonLLMLaunchCoordinator"
_LAUNCH_SCHEDULE_GROUP_NAME = "axonllm-launch-coordinator"
_LAUNCH_CLEANUP_SCHEDULE_NAME = "axonllm-launch-coordinator-cleanup"
_LAUNCH_WATCHDOG_SCHEDULE_NAME = "axonllm-launch-coordinator-watchdog"
_QUALIFICATION_MUTATION_AUTHORIZATION_TABLE_NAME = (
    "axonllm-qualification-mutation-authorizations"
)
_QUALIFICATION_MUTATION_BROKER_NAME = (
    "axonllm-qualification-selector-mutation-broker"
)
_PRODUCTION_MUTATION_BROKER_NAME = "axonllm-production-transition-mutation-broker"
_QUALIFICATION_MUTATION_BROKER_LOG_GROUP = (
    f"/aws/lambda/{_QUALIFICATION_MUTATION_BROKER_NAME}"
)
_PRODUCTION_MUTATION_BROKER_LOG_GROUP = (
    f"/aws/lambda/{_PRODUCTION_MUTATION_BROKER_NAME}"
)
_LAUNCH_WORKER_ACTIONS = (
    "induce-initialization-timeout",
    "observe-exit-124",
    "observe-runtime-replacement",
    "verify-replacement-ready",
    "reject-query-boundaries",
    "interrupt-query",
    "verify-terminal-reconciliation",
    "verify-deferred-accounting",
    "restore-state",
    "cutover-restored-state",
    "verify-restored-state",
    "rollback-primary-state",
    "verify-primary-state",
    "deliver-security-events",
    "verify-outbox-drained",
    "force-dead-letter",
    "verify-dead-letter-alarm",
    "redrive-dead-letter",
    "verify-redelivery",
    "exercise-routing-strategies",
    "verify-routing-decisions",
    "inject-primary-provider-fault",
    "verify-provider-fallback",
    "clear-primary-provider-fault",
    "verify-primary-provider-recovery",
    "inject-control-plane-fault",
    "verify-control-plane-fail-closed",
    "clear-control-plane-fault",
    "verify-control-plane-recovery",
)
_COORDINATOR_TAGS = [
    {"key": "Application", "value": "AxonLLM"},
    {"key": "Environment", "value": "production"},
    {"key": "Purpose", "value": "agentcore-launch-rehearsal"},
]


@dataclass(frozen=True)
class _LaunchCoordinator:
    key: kms.Key
    lease_table: dynamodb.Table
    rehearsal_control_table: dynamodb.Table
    qualification_mutation_authorization_table: dynamodb.Table
    qualification_mutation_broker_version: lambda_.Version
    runtime_identity_secret: secretsmanager.CfnSecret
    state_machine: stepfunctions.CfnStateMachine
    version: stepfunctions.CfnStateMachineVersion
    execution_role: iam.Role
    launch_role: iam.Role
    action_activity: stepfunctions.CfnActivity
    cleanup_activity: stepfunctions.CfnActivity
    action_worker_role: iam.Role
    cleanup_worker_role: iam.Role
    scheduler_role: iam.Role
    schedule_group: scheduler.CfnScheduleGroup
    scheduler_dead_letter_queue: sqs.Queue
    watchdog_alarm: cloudwatch.Alarm
    alarm_topic: sns.Topic
    alarm_receipt_queue: sqs.Queue


@dataclass(frozen=True)
class _LaunchAuthorities:
    deploy_role: iam.Role
    rehearsal_evidence_role: iam.Role
    external_oidc_role: iam.Role
    qualification_role: iam.Role
    transition_watchdog_role: iam.Role


class AxonLLMReleaseFoundationStack(Stack):
    """Retained release stores with distinct publish and verification roles."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "us-east-1":
            raise ValueError("AxonLLM release foundation must be deployed in us-east-1")

        fargate_state_table_name = CfnParameter(
            self,
            "FargateStateTableName",
            type="String",
            default="axonllm-state",
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,214}$",
            description="Physical state table name used by AxonLLMStack",
        )
        agentcore_state_table_name = CfnParameter(
            self,
            "AgentCoreStateTableName",
            type="String",
            default="axonllm-agentcore-state",
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,214}$",
            description=("Physical state table name used by AxonLLMAgentCoreStack"),
        )
        state_table_names = (
            fargate_state_table_name.value_as_string,
            agentcore_state_table_name.value_as_string,
        )
        github_subject_prefix = CfnParameter(
            self,
            "GitHubOidcSubjectPrefix",
            type="String",
            default=_DEFAULT_GITHUB_SUBJECT_PREFIX,
            allowed_pattern=_GITHUB_SUBJECT_PREFIX_PATTERN,
            description=(
                "GitHub OIDC repository identity in repo:<owner>@<owner-id>/<repository>@<repository-id> form"
            ),
        ).value_as_string
        github_subjects = {
            "signing": f"{github_subject_prefix}:ref:refs/tags/v*",
            "release": f"{github_subject_prefix}:environment:release",
            "production": (f"{github_subject_prefix}:environment:production"),
            "deploy": (f"{github_subject_prefix}:environment:agentcore-production-deploy"),
            "evidence": (f"{github_subject_prefix}:environment:agentcore-production-evidence"),
            "watchdog": (f"{github_subject_prefix}:environment:agentcore-production-watchdog"),
            "launch_gates": (f"{github_subject_prefix}:environment:agentcore-production-launch-gates"),
            "external": (f"{github_subject_prefix}:environment:agentcore-external-oidc-production-like"),
            "qualification": (f"{github_subject_prefix}:environment:agentcore-qualification"),
        }
        provider_source_parameters = {
            namespace: (
                CfnParameter(
                    self,
                    f"{construct_prefix}ProviderSourceSecretArn",
                    type="String",
                    allowed_pattern=_SECRET_ARN_PATTERN,
                    description=(f"Exact {description} provider source secret ARN"),
                ).value_as_string,
                CfnParameter(
                    self,
                    f"{construct_prefix}ProviderSourceKmsKeyArn",
                    type="String",
                    allowed_pattern=_KMS_KEY_ARN_PATTERN,
                    description=(f"Exact customer-managed KMS key ARN for the {description} provider source secret"),
                ).value_as_string,
            )
            for namespace, construct_prefix, description in (
                ("production", "Production", "production"),
                ("qualification", "Qualification", "qualification"),
                (
                    "external",
                    "ExternalOidc",
                    "external-OIDC certification",
                ),
            )
        }
        launch_alarm_email = CfnParameter(
            self,
            "LaunchAlarmEmail",
            type="String",
            allowed_pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            description=("Operator incident email that must confirm the launch alarm subscription before production"),
        ).value_as_string

        release_key = kms.Key(
            self,
            "ReleaseRegistryKey",
            alias="alias/axonllm/release-ecr",
            description="AxonLLM private release registry encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        release_signing_key = kms.Key(
            self,
            "ReleaseSigningKey",
            alias="alias/axonllm/release-signing",
            description="AxonLLM private release evidence signing",
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        release_signing_version_alias = kms.Alias(
            self,
            "ReleaseSigningKeyVersionAlias",
            alias_name="alias/axonllm/release-signing-v1",
            target_key=release_signing_key,
        )
        release_signing_version_alias.apply_removal_policy(RemovalPolicy.RETAIN)
        prerequisite_signing_key = kms.Key(
            self,
            "LaunchPrerequisiteSigningKey",
            alias="alias/axonllm/agentcore-launch-prerequisite-signing",
            description=("Signs AgentCore launch prerequisite and qualification evidence"),
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        transition_signing_key = kms.Key(
            self,
            "ProductionTransitionSigningKey",
            alias="alias/axonllm/agentcore-production-transition-signing",
            description=("Signs AgentCore production transition intent and deployment evidence"),
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        transition_terminal_signing_key = kms.Key(
            self,
            "ProductionTransitionTerminalSigningKey",
            alias="alias/axonllm/agentcore-production-transition-terminal-signing",
            description=("Signs terminal records produced by the production transition watchdog"),
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            removal_policy=RemovalPolicy.RETAIN,
        )

        evidence_key = kms.Key(
            self,
            "DeploymentEvidenceKey",
            alias="alias/axonllm/deployment-evidence",
            description="Encrypts immutable AxonLLM deployment evidence",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        evidence_bucket = s3.Bucket(
            self,
            "DeploymentEvidenceBucket",
            bucket_name=(f"axonllm-deployment-evidence-{self.account}-{self.region}"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            bucket_key_enabled=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=evidence_key,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.compliance(Duration.days(_EVIDENCE_RETENTION_DAYS)),
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,
        )
        evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyEvidenceDeletion",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=[
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                resources=[evidence_bucket.arn_for_objects("*")],
            )
        )

        repositories = {
            target: self._release_repository(
                target=target,
                encryption_key=release_key,
            )
            for target in ("fargate", "agentcore")
        }

        github_provider = iam.OidcProviderNative(
            self,
            "GitHubActionsProvider",
            url=f"https://{_GITHUB_OIDC_ISSUER}",
            client_ids=["sts.amazonaws.com"],
            removal_policy=RemovalPolicy.RETAIN,
        )

        signer = iam.Role(
            self,
            "ReleaseSignerRole",
            role_name="AxonLLMReleaseSigner",
            description=("Signs AxonLLM release evidence from v-prefixed Git tags"),
            assumed_by=self._github_signing_principal(
                github_provider,
                subject=github_subjects["signing"],
            ),
            max_session_duration=Duration.hours(1),
        )
        publisher = iam.Role(
            self,
            "ReleasePublisherRole",
            role_name="AxonLLMReleasePublisher",
            description=("Publishes verified AxonLLM release artifacts from the protected GitHub release environment"),
            assumed_by=self._github_principal(
                github_provider,
                subject=github_subjects["release"],
            ),
            max_session_duration=Duration.hours(1),
        )
        verifier = iam.Role(
            self,
            "ReleaseVerifierRole",
            role_name="AxonLLMReleaseVerifier",
            description=("Reads immutable AxonLLM images during deployment verification"),
            assumed_by=self._github_principal(
                github_provider,
                subject=github_subjects["production"],
            ),
            max_session_duration=Duration.hours(1),
        )
        operations_audit = iam.Role(
            self,
            "OperationsAuditRole",
            role_name="AxonLLMOperationsAudit",
            description=(
                "Audits AxonLLM recovery and secret-rotation metadata from the protected GitHub production environment"
            ),
            assumed_by=self._github_principal(
                github_provider,
                subject=github_subjects["production"],
            ),
            max_session_duration=Duration.hours(1),
        )
        operations_recovery = iam.Role(
            self,
            "OperationsRecoveryRole",
            role_name="AxonLLMOperationsRecovery",
            description=("Exercises AxonLLM point-in-time recovery from the protected GitHub production environment"),
            assumed_by=self._github_principal(
                github_provider,
                subject=github_subjects["production"],
            ),
            max_session_duration=Duration.hours(2),
        )
        launch_coordinator = self._create_launch_coordinator(
            github_provider=github_provider,
            launch_gates_subject=github_subjects["launch_gates"],
            launch_alarm_email=launch_alarm_email,
            state_table_names=state_table_names,
        )
        production_mutation_broker_version = (
            self._create_production_transition_mutation_broker(
                evidence_bucket=evidence_bucket,
                evidence_key=evidence_key,
                transition_signing_key=transition_signing_key,
                log_encryption_key=launch_coordinator.key,
            )
        )
        self._grant_evidence_access(
            launch_coordinator.launch_role,
            bucket=evidence_bucket,
            evidence_key=evidence_key,
            read_prefixes=(_REHEARSAL_EVIDENCE_PREFIX,),
            write_prefixes=(_REHEARSAL_EVIDENCE_PREFIX,),
            verification_keys=(prerequisite_signing_key,),
            signing_key=prerequisite_signing_key,
        )
        launch_authorities = self._create_launch_authorities(
            github_provider=github_provider,
            evidence_bucket=evidence_bucket,
            evidence_key=evidence_key,
            prerequisite_signing_key=prerequisite_signing_key,
            transition_signing_key=transition_signing_key,
            transition_terminal_signing_key=(
                transition_terminal_signing_key
            ),
            production_mutation_broker_version=(
                production_mutation_broker_version
            ),
            repositories=repositories,
            runtime_identity_secret=launch_coordinator.runtime_identity_secret,
            coordinator_key=launch_coordinator.key,
            github_subjects=github_subjects,
            provider_source_parameters=provider_source_parameters,
        )

        repository_arns = [repository.repository_arn for repository in repositories.values()]
        for role in (publisher, verifier):
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="AuthorizePrivateRegistry",
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"],
                )
            )
        publisher.add_to_policy(
            iam.PolicyStatement(
                sid="PublishReleaseImages",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:CompleteLayerUpload",
                    "ecr:DescribeImages",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:InitiateLayerUpload",
                    "ecr:PutImage",
                    "ecr:UploadLayerPart",
                ],
                resources=repository_arns,
            )
        )
        verifier.add_to_policy(
            iam.PolicyStatement(
                sid="VerifyReleaseImages",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=repository_arns,
            )
        )
        release_signing_key.grant_sign_verify(signer)
        for role in (publisher, verifier):
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="VerifyApprovedReleaseEvidence",
                    actions=["kms:Verify"],
                    resources=[self._account_key_arn()],
                    conditions=self._release_signing_alias_condition(),
                )
            )
        self._grant_operations_audit(
            operations_audit,
            state_table_names=state_table_names,
        )
        self._grant_operations_recovery(
            operations_recovery,
            state_table_names=state_table_names,
        )

        CfnOutput(
            self,
            "ReleaseRegistryKeyArn",
            value=release_key.key_arn,
        )
        CfnOutput(
            self,
            "ReleaseSigningKeyArn",
            value=release_signing_key.key_arn,
        )
        CfnOutput(
            self,
            "LaunchPrerequisiteSigningKeyArn",
            value=prerequisite_signing_key.key_arn,
        )
        CfnOutput(
            self,
            "ProductionTransitionSigningKeyArn",
            value=transition_signing_key.key_arn,
        )
        CfnOutput(
            self,
            "ProductionTransitionTerminalSigningKeyArn",
            value=transition_terminal_signing_key.key_arn,
        )
        CfnOutput(
            self,
            "ProductionTransitionMutationBrokerVersionArn",
            value=production_mutation_broker_version.function_arn,
        )
        CfnOutput(
            self,
            "QualificationMutationBrokerVersionArn",
            value=(
                launch_coordinator.qualification_mutation_broker_version.function_arn
            ),
        )
        CfnOutput(
            self,
            "QualificationMutationAuthorizationTableArn",
            value=(
                launch_coordinator.qualification_mutation_authorization_table.table_arn
            ),
        )
        CfnOutput(
            self,
            "DeploymentEvidenceBucketArn",
            value=evidence_bucket.bucket_arn,
        )
        CfnOutput(
            self,
            "DeploymentEvidenceBucketName",
            value=evidence_bucket.bucket_name,
        )
        CfnOutput(
            self,
            "DeploymentEvidenceKeyArn",
            value=evidence_key.key_arn,
        )
        CfnOutput(
            self,
            "DeploymentEvidencePrefix",
            value=_TRANSITION_EVIDENCE_PREFIX,
        )
        CfnOutput(
            self,
            "LaunchRehearsalEvidencePrefix",
            value=_REHEARSAL_EVIDENCE_PREFIX,
        )
        CfnOutput(
            self,
            "QualificationTeardownEvidencePrefix",
            value=_QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX,
        )
        CfnOutput(
            self,
            "ExternalOidcEvidencePrefix",
            value=_EXTERNAL_OIDC_EVIDENCE_PREFIX,
        )
        CfnOutput(
            self,
            "GitHubOidcProviderArn",
            value=github_provider.oidc_provider_arn,
        )
        CfnOutput(
            self,
            "ReleasePublisherRoleArn",
            value=publisher.role_arn,
        )
        CfnOutput(
            self,
            "ReleaseSignerRoleArn",
            value=signer.role_arn,
        )
        CfnOutput(
            self,
            "ReleaseVerifierRoleArn",
            value=verifier.role_arn,
        )
        CfnOutput(
            self,
            "OperationsAuditRoleArn",
            value=operations_audit.role_arn,
        )
        CfnOutput(
            self,
            "OperationsRecoveryRoleArn",
            value=operations_recovery.role_arn,
        )
        CfnOutput(
            self,
            "AgentCoreDeployRoleArn",
            value=launch_authorities.deploy_role.role_arn,
        )
        CfnOutput(
            self,
            "AgentCoreRehearsalEvidenceRoleArn",
            value=launch_authorities.rehearsal_evidence_role.role_arn,
        )
        CfnOutput(
            self,
            "ExternalOidcCertificationRoleArn",
            value=launch_authorities.external_oidc_role.role_arn,
        )
        CfnOutput(
            self,
            "AgentCoreQualificationRoleArn",
            value=launch_authorities.qualification_role.role_arn,
        )
        CfnOutput(
            self,
            "AgentCoreTransitionWatchdogRoleArn",
            value=launch_authorities.transition_watchdog_role.role_arn,
        )
        CfnOutput(
            self,
            "AgentCoreLaunchGatesRoleArn",
            value=launch_coordinator.launch_role.role_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorExecutionRoleArn",
            value=launch_coordinator.execution_role.role_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorStateMachineArn",
            value=launch_coordinator.state_machine.attr_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorStateMachineVersionArn",
            value=launch_coordinator.version.attr_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorLeaseTableArn",
            value=launch_coordinator.lease_table.table_arn,
        )
        CfnOutput(
            self,
            "RehearsalControlLedgerTableArn",
            value=launch_coordinator.rehearsal_control_table.table_arn,
        )
        CfnOutput(
            self,
            "RehearsalControlLedgerTableName",
            value=launch_coordinator.rehearsal_control_table.table_name,
        )
        CfnOutput(
            self,
            "LaunchRuntimeIdentitySecretArn",
            value=launch_coordinator.runtime_identity_secret.attr_id,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorKeyArn",
            value=launch_coordinator.key.key_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorWatchdogAlarmArn",
            value=launch_coordinator.watchdog_alarm.alarm_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorActionActivityArn",
            value=launch_coordinator.action_activity.attr_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorCleanupActivityArn",
            value=launch_coordinator.cleanup_activity.attr_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorActionWorkerRoleArn",
            value=launch_coordinator.action_worker_role.role_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorCleanupWorkerRoleArn",
            value=launch_coordinator.cleanup_worker_role.role_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorSchedulerRoleArn",
            value=launch_coordinator.scheduler_role.role_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorScheduleGroupArn",
            value=launch_coordinator.schedule_group.attr_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorSchedulerDeadLetterQueueArn",
            value=launch_coordinator.scheduler_dead_letter_queue.queue_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorAlarmTopicArn",
            value=launch_coordinator.alarm_topic.topic_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorAlarmReceiptQueueArn",
            value=launch_coordinator.alarm_receipt_queue.queue_arn,
        )
        CfnOutput(
            self,
            "LaunchCoordinatorAlarmReceiptQueueUrl",
            value=launch_coordinator.alarm_receipt_queue.queue_url,
        )
        CfnOutput(
            self,
            "FargateRepositoryUri",
            value=repositories["fargate"].repository_uri,
        )
        CfnOutput(
            self,
            "AgentCoreRepositoryUri",
            value=repositories["agentcore"].repository_uri,
        )

    def _release_repository(
        self,
        *,
        target: str,
        encryption_key: kms.IKey,
    ) -> ecr.Repository:
        repository = ecr.Repository(
            self,
            f"{target.title()}Repository",
            repository_name=f"axonllm/{target}",
            encryption=ecr.RepositoryEncryption.KMS,
            encryption_key=encryption_key,
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            empty_on_delete=False,
            removal_policy=RemovalPolicy.RETAIN,
        )
        repository.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["ecr:*"],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )
        return repository

    @staticmethod
    def _mutation_broker_source(filename: str) -> str:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "gateway"
            / "deployment"
            / filename
        )
        source = path.read_text(encoding="ascii")
        if not source.endswith("\n"):
            raise ValueError(f"mutation broker source is not newline terminated: {path}")
        return source

    def _create_production_transition_mutation_broker(
        self,
        *,
        evidence_bucket: s3.IBucket,
        evidence_key: kms.IKey,
        transition_signing_key: kms.IKey,
        log_encryption_key: kms.IKey,
    ) -> lambda_.Version:
        execution_role_arn = self.format_arn(
            service="iam",
            region="",
            resource="role",
            resource_name=(
                f"cdk-{_PRODUCTION_CDK_QUALIFIER}-cfn-exec-role-"
                f"{self.account}-{self.region}"
            ),
        )
        role = iam.Role(
            self,
            "ProductionTransitionMutationBrokerRole",
            role_name="AxonLLMProductionTransitionMutationBrokerRole",
            description=(
                "Executes only evidence-bound production transition mutations"
            ),
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            max_session_duration=Duration.hours(1),
        )
        log_group = logs.LogGroup(
            self,
            "ProductionTransitionMutationBrokerLogGroup",
            log_group_name=_PRODUCTION_MUTATION_BROKER_LOG_GROUP,
            encryption_key=log_encryption_key,
            retention=logs.RetentionDays.TEN_YEARS,
            deletion_protection_enabled=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteBrokerLogs",
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[f"{log_group.log_group_arn}:log-stream:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListTransitionEvidenceVersions",
                actions=["s3:ListBucketVersions"],
                resources=[evidence_bucket.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [
                            f"{_TRANSITION_EVIDENCE_PREFIX}/*",
                        ]
                    }
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadExactTransitionEvidence",
                actions=[
                    "s3:GetObjectRetention",
                    "s3:GetObjectVersion",
                ],
                resources=[
                    evidence_bucket.arn_for_objects(
                        f"{_TRANSITION_EVIDENCE_PREFIX}/*"
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptTransitionEvidence",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                ],
                resources=[evidence_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (
                            f"s3.{self.region}.{self.url_suffix}"
                        ),
                    }
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="VerifyTransitionEvidence",
                actions=["kms:DescribeKey", "kms:Verify"],
                resources=[transition_signing_key.key_arn],
            )
        )
        production_stack_arns = [
            self.format_arn(
                service="cloudformation",
                resource="stack",
                resource_name=f"{name}/*",
            )
            for name in (
                "AxonLLMAgentCoreStack",
                "AxonLLMControlPlaneStack",
            )
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="MutateBoundProductionTransition",
                actions=[
                    "cloudformation:DeleteStack",
                    "cloudformation:DescribeStacks",
                    "cloudformation:ListStackResources",
                    "cloudformation:UpdateStack",
                ],
                resources=production_stack_arns,
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassProductionCloudFormationExecutionRole",
                actions=["iam:PassRole"],
                resources=[execution_role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "cloudformation.amazonaws.com"
                    }
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ManageOwnedControlPlaneDeletionProtection",
                actions=[
                    "elasticloadbalancing:DescribeLoadBalancerAttributes",
                    "elasticloadbalancing:ModifyLoadBalancerAttributes",
                ],
                resources=[
                    self.format_arn(
                        service="elasticloadbalancing",
                        resource="loadbalancer/app",
                        resource_name="*/*",
                    )
                ],
                conditions={
                    "StringEquals": {
                        "aws:ResourceTag/aws:cloudformation:stack-name": (
                            "AxonLLMControlPlaneStack"
                        )
                    }
                },
            )
        )
        function = lambda_.Function(
            self,
            "ProductionTransitionMutationBroker",
            function_name=_PRODUCTION_MUTATION_BROKER_NAME,
            description=(
                "Evidence-verifying broker for one bounded production "
                "transition mutation per invocation"
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="index.lambda_handler",
            code=lambda_.Code.from_inline(
                self._mutation_broker_source(
                    "production_transition_mutation_broker.py"
                )
            ),
            role=role,
            log_group=log_group,
            memory_size=512,
            timeout=Duration.minutes(2),
            reserved_concurrent_executions=1,
            environment={
                "AXON_DEPLOYMENT_EVIDENCE_BUCKET": evidence_bucket.bucket_name,
                "AXON_DEPLOYMENT_EVIDENCE_PREFIX": (
                    _TRANSITION_EVIDENCE_PREFIX
                ),
                "AXON_AGENTCORE_TRANSITION_SIGNING_KEY_ARN": (
                    transition_signing_key.key_arn
                ),
                "AXON_AGENTCORE_STACK_NAME": "AxonLLMAgentCoreStack",
                "AXON_CONTROL_PLANE_STACK_NAME": "AxonLLMControlPlaneStack",
                "AXON_CLOUDFORMATION_EXECUTION_ROLE_ARN": execution_role_arn,
            },
        )
        function.node.add_dependency(log_group)
        version = function.current_version
        version.apply_removal_policy(RemovalPolicy.RETAIN)
        return version

    def _create_launch_authorities(
        self,
        *,
        github_provider: iam.IOidcProvider,
        evidence_bucket: s3.IBucket,
        evidence_key: kms.IKey,
        prerequisite_signing_key: kms.IKey,
        transition_signing_key: kms.IKey,
        transition_terminal_signing_key: kms.IKey,
        production_mutation_broker_version: lambda_.IVersion,
        repositories: dict[str, ecr.Repository],
        runtime_identity_secret: secretsmanager.CfnSecret,
        coordinator_key: kms.IKey,
        github_subjects: dict[str, str],
        provider_source_parameters: dict[str, tuple[str, str]],
    ) -> _LaunchAuthorities:
        deploy_role = self._github_role(
            "AgentCoreDeployRole",
            role_name="AxonLLMAgentCoreDeployRole",
            description="Deploys the reviewed AgentCore production release",
            provider=github_provider,
            subject=github_subjects["deploy"],
            duration=Duration.hours(3),
        )
        rehearsal_evidence_role = self._github_role(
            "AgentCoreRehearsalEvidenceRole",
            role_name="AxonLLMAgentCoreRehearsalEvidenceRole",
            description="Publishes immutable AgentCore rehearsal evidence",
            provider=github_provider,
            subject=github_subjects["evidence"],
            duration=Duration.hours(2),
        )
        external_oidc_role = self._github_role(
            "ExternalOidcCertificationRole",
            role_name="AxonLLMExternalOidcCertificationRole",
            description="Certifies an isolated external-OIDC AgentCore candidate",
            provider=github_provider,
            subject=github_subjects["external"],
            duration=Duration.hours(2),
        )
        qualification_role = self._github_role(
            "AgentCoreQualificationRole",
            role_name="AxonLLMAgentCoreQualificationRole",
            description="Stages and certifies isolated AgentCore qualification resources",
            provider=github_provider,
            subject=github_subjects["qualification"],
            duration=Duration.hours(3),
        )
        transition_watchdog_role = self._github_role(
            "AgentCoreTransitionWatchdogRole",
            role_name="AxonLLMAgentCoreTransitionWatchdogRole",
            description="Reconciles only signed nonterminal AgentCore transitions",
            provider=github_provider,
            subject=github_subjects["watchdog"],
            duration=Duration.hours(1),
        )

        self._grant_evidence_access(
            deploy_role,
            bucket=evidence_bucket,
            evidence_key=evidence_key,
            read_prefixes=(
                _REHEARSAL_EVIDENCE_PREFIX,
                _QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX,
                _TRANSITION_EVIDENCE_PREFIX,
                _EXTERNAL_OIDC_EVIDENCE_PREFIX,
            ),
            write_prefixes=(_TRANSITION_EVIDENCE_PREFIX,),
            verification_keys=(
                prerequisite_signing_key,
                transition_signing_key,
            ),
            signing_key=transition_signing_key,
        )
        self._grant_evidence_access(
            rehearsal_evidence_role,
            bucket=evidence_bucket,
            evidence_key=evidence_key,
            read_prefixes=(
                _REHEARSAL_EVIDENCE_PREFIX,
                _QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX,
            ),
            write_prefixes=(
                _REHEARSAL_EVIDENCE_PREFIX,
                _QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX,
            ),
            verification_keys=(prerequisite_signing_key,),
            signing_key=prerequisite_signing_key,
        )
        self._grant_evidence_access(
            external_oidc_role,
            bucket=evidence_bucket,
            evidence_key=evidence_key,
            read_prefixes=(_EXTERNAL_OIDC_EVIDENCE_PREFIX,),
            write_prefixes=(_EXTERNAL_OIDC_EVIDENCE_PREFIX,),
            verification_keys=(prerequisite_signing_key,),
            signing_key=prerequisite_signing_key,
        )
        self._grant_evidence_access(
            qualification_role,
            bucket=evidence_bucket,
            evidence_key=evidence_key,
            read_prefixes=(
                _REHEARSAL_EVIDENCE_PREFIX,
                _EXTERNAL_OIDC_EVIDENCE_PREFIX,
            ),
            write_prefixes=(),
            verification_keys=(prerequisite_signing_key,),
            signing_key=None,
        )
        self._grant_transition_watchdog_evidence_access(
            transition_watchdog_role,
            bucket=evidence_bucket,
            evidence_key=evidence_key,
            transition_signing_key=transition_signing_key,
            terminal_signing_key=transition_terminal_signing_key,
        )
        self._grant_deployment_access(
            deploy_role,
            repositories=repositories,
            deployment_namespace="",
            stack_bases=(
                "AxonLLMIdentityStack",
                "AxonLLMAgentCoreStack",
                "AxonLLMControlPlaneStack",
            ),
            managed_identity=True,
            provider_source_secret_arn=provider_source_parameters["production"][0],
            provider_source_kms_key_arn=provider_source_parameters["production"][1],
        )
        self._grant_deployment_access(
            qualification_role,
            repositories=repositories,
            deployment_namespace=_QUALIFICATION_NAMESPACE,
            stack_bases=(
                "AxonLLMIdentityStack",
                "AxonLLMAgentCoreStack",
                "AxonLLMControlPlaneStack",
                "AxonLLMLaunchWorkersStack",
            ),
            managed_identity=True,
            provider_source_secret_arn=provider_source_parameters["qualification"][0],
            provider_source_kms_key_arn=provider_source_parameters["qualification"][1],
        )
        self._grant_deployment_access(
            external_oidc_role,
            repositories=repositories,
            deployment_namespace="external",
            stack_bases=("AxonLLMAgentCoreStack",),
            managed_identity=False,
            provider_source_secret_arn=provider_source_parameters["external"][0],
            provider_source_kms_key_arn=provider_source_parameters["external"][1],
        )
        self._grant_runtime_identity_rotation(
            qualification_role,
            runtime_identity_secret=runtime_identity_secret,
            coordinator_key=coordinator_key,
        )
        production_mutation_broker_version.grant_invoke(
            transition_watchdog_role
        )
        return _LaunchAuthorities(
            deploy_role=deploy_role,
            rehearsal_evidence_role=rehearsal_evidence_role,
            external_oidc_role=external_oidc_role,
            qualification_role=qualification_role,
            transition_watchdog_role=transition_watchdog_role,
        )

    def _github_role(
        self,
        construct_id: str,
        *,
        role_name: str,
        description: str,
        provider: iam.IOidcProvider,
        subject: str,
        duration: Duration,
    ) -> iam.Role:
        return iam.Role(
            self,
            construct_id,
            role_name=role_name,
            description=description,
            assumed_by=self._github_principal(
                provider,
                subject=subject,
            ),
            max_session_duration=duration,
        )

    def _grant_evidence_access(
        self,
        role: iam.Role,
        *,
        bucket: s3.IBucket,
        evidence_key: kms.IKey,
        read_prefixes: tuple[str, ...],
        write_prefixes: tuple[str, ...],
        verification_keys: tuple[kms.IKey, ...],
        signing_key: kms.IKey | None,
    ) -> None:
        if not read_prefixes or not set(write_prefixes).issubset(read_prefixes):
            raise ValueError("evidence write prefixes must be a subset of read prefixes")
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectImmutableEvidenceStore",
                actions=[
                    "s3:GetBucketObjectLockConfiguration",
                    "s3:GetBucketPolicy",
                    "s3:GetBucketVersioning",
                    "s3:GetEncryptionConfiguration",
                ],
                resources=[bucket.bucket_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListBoundEvidenceVersions",
                actions=["s3:ListBucketVersions"],
                resources=[bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{prefix}/*" for prefix in read_prefixes]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadBoundImmutableEvidence",
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectRetention",
                    "s3:GetObjectVersion",
                ],
                resources=[bucket.arn_for_objects(f"{prefix}/*") for prefix in read_prefixes],
            )
        )
        if write_prefixes:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="AppendBoundImmutableEvidence",
                    actions=[
                        "s3:PutObject",
                        "s3:PutObjectRetention",
                    ],
                    resources=[bucket.arn_for_objects(f"{prefix}/*") for prefix in write_prefixes],
                )
            )
        key_actions = [
            "kms:Decrypt",
            "kms:DescribeKey",
        ]
        if write_prefixes:
            key_actions.extend(
                [
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ]
            )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseEvidenceEncryption",
                actions=key_actions,
                resources=[evidence_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"s3.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="VerifyBoundLaunchEvidence",
                actions=[
                    "kms:DescribeKey",
                    "kms:Verify",
                ],
                resources=[key.key_arn for key in verification_keys],
            )
        )
        if signing_key is not None:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="SignOwnedLaunchEvidence",
                    actions=[
                        "kms:DescribeKey",
                        "kms:Sign",
                        "kms:Verify",
                    ],
                    resources=[signing_key.key_arn],
                )
            )

    def _grant_transition_watchdog_evidence_access(
        self,
        role: iam.Role,
        *,
        bucket: s3.IBucket,
        evidence_key: kms.IKey,
        transition_signing_key: kms.IKey,
        terminal_signing_key: kms.IKey,
    ) -> None:
        prefix = _TRANSITION_EVIDENCE_PREFIX
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectImmutableEvidenceStore",
                actions=[
                    "s3:GetBucketObjectLockConfiguration",
                    "s3:GetBucketPolicy",
                    "s3:GetBucketVersioning",
                    "s3:GetEncryptionConfiguration",
                ],
                resources=[bucket.bucket_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListBoundEvidenceVersions",
                actions=["s3:ListBucketVersions"],
                resources=[bucket.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [f"{prefix}/*"],
                    }
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadBoundImmutableEvidence",
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectRetention",
                    "s3:GetObjectVersion",
                ],
                resources=[bucket.arn_for_objects(f"{prefix}/*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AppendTerminalTransitionEvidence",
                actions=[
                    "s3:PutObject",
                    "s3:PutObjectRetention",
                ],
                resources=[
                    bucket.arn_for_objects(
                        f"{prefix}/*/transition-terminal.json"
                    ),
                    bucket.arn_for_objects(
                        f"{prefix}/*/"
                        "transition-terminal-kms-signature.json"
                    ),
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseEvidenceEncryption",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[evidence_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (
                            f"s3.{self.region}.{self.url_suffix}"
                        ),
                    }
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="VerifyProductionTransitionIntent",
                actions=["kms:DescribeKey", "kms:Verify"],
                resources=[transition_signing_key.key_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SignTerminalTransitionEvidence",
                actions=[
                    "kms:DescribeKey",
                    "kms:Sign",
                    "kms:Verify",
                ],
                resources=[terminal_signing_key.key_arn],
            )
        )

    def _grant_deployment_access(
        self,
        role: iam.Role,
        *,
        repositories: dict[str, ecr.Repository],
        deployment_namespace: str,
        stack_bases: tuple[str, ...],
        managed_identity: bool,
        provider_source_secret_arn: str,
        provider_source_kms_key_arn: str,
    ) -> None:
        cdk_qualifier = self._cdk_qualifier(deployment_namespace)
        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        stack_names = tuple(f"{base}{physical_suffix}" for base in stack_bases)
        stack_arns = [
            self.format_arn(
                service="cloudformation",
                resource="stack",
                resource_name=f"{name}/*",
            )
            for name in stack_names
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DeployReviewedAxonStacks",
                actions=[
                    "cloudformation:CreateChangeSet",
                    "cloudformation:CreateStack",
                    "cloudformation:DeleteChangeSet",
                    "cloudformation:DeleteStack",
                    "cloudformation:DescribeChangeSet",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStacks",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:GetTemplate",
                    "cloudformation:GetTemplateSummary",
                    "cloudformation:ListStackResources",
                    "cloudformation:UpdateStack",
                ],
                resources=stack_arns,
            )
        )
        if deployment_namespace == _QUALIFICATION_NAMESPACE:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="InspectReleaseFoundation",
                    actions=["cloudformation:DescribeStacks"],
                    resources=[
                        self.format_arn(
                            service="cloudformation",
                            resource="stack",
                            resource_name="AxonLLMReleaseFoundationStack/*",
                        )
                    ],
                )
            )
            launch_worker_service_arns = [
                self.format_arn(
                    service="ecs",
                    resource="service",
                    resource_name=(f"*/axonllm-launch-{mode}-worker-{_QUALIFICATION_NAMESPACE}"),
                )
                for mode in ("action", "cleanup")
            ]
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="InspectQualificationLaunchWorkers",
                    actions=["ecs:DescribeServices"],
                    resources=launch_worker_service_arns,
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="ResizeQualificationLaunchWorkers",
                    actions=["ecs:UpdateService"],
                    resources=launch_worker_service_arns,
                )
            )
        cdk_role_arns = [
            self.format_arn(
                service="iam",
                region="",
                resource="role",
                resource_name=(f"cdk-{cdk_qualifier}-{purpose}-role-{self.account}-{self.region}"),
            )
            for purpose in (
                "deploy",
                "file-publishing",
                "image-publishing",
                "lookup",
            )
        ]
        cloudformation_role_arn = self.format_arn(
            service="iam",
            region="",
            resource="role",
            resource_name=(f"cdk-{cdk_qualifier}-cfn-exec-role-{self.account}-{self.region}"),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=cdk_role_arns,
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassCloudFormationExecutionRole",
                actions=["iam:PassRole"],
                resources=[cloudformation_role_arn],
                conditions={"StringEquals": {"iam:PassedToService": ("cloudformation.amazonaws.com")}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectCdkBootstrapPolicies",
                actions=[
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                ],
                resources=[
                    self.format_arn(
                        service="iam",
                        region="",
                        resource="policy",
                        resource_name=name,
                    )
                    for name in (
                        *(
                            "AxonLLMAgentCoreCloudFormationExecution-"
                            f"{cdk_qualifier}-{self.region}-part{part}"
                            for part in range(
                                1,
                                _CDK_EXECUTION_POLICY_PART_COUNT + 1,
                            )
                        ),
                        f"AxonLLMAgentCoreServiceBoundary-{cdk_qualifier}-{self.region}",
                        f"AxonLLMAgentCoreBootstrapBoundary-{cdk_qualifier}-{self.region}",
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectCdkBootstrapExecutionRole",
                actions=[
                    "iam:GetRole",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                ],
                resources=[cloudformation_role_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCdkBootstrapVersion",
                actions=[
                    "ssm:GetParameter",
                ],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=(f"cdk-bootstrap/{cdk_qualifier}/version"),
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ConfirmDeploymentAccount",
                actions=[
                    "sts:GetCallerIdentity",
                ],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadReleaseImages",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:DescribeImages",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[repository.repository_arn for repository in repositories.values()],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AuthorizePrivateRegistry",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        self._grant_certification_access(
            role,
            managed_identity=managed_identity,
            deployment_namespace=deployment_namespace,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadProviderSourceSecret",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[provider_source_secret_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptProviderSourceSecret",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                ],
                resources=[provider_source_kms_key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:EncryptionContext:SecretARN": (provider_source_secret_arn),
                        "kms:ViaService": (f"secretsmanager.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )
        deployed_secret_name = f"AxonLLMAgentCoreStack{physical_suffix}-ProviderCredentials*"
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RotateDeployedProviderSecret",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:UpdateSecretVersionStage",
                ],
                resources=[
                    self.format_arn(
                        service="secretsmanager",
                        resource="secret",
                        resource_name=deployed_secret_name,
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        data_key_alias = f"alias/axonllm/agentcore-data{physical_suffix}"
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseDeployedProviderSecretKey",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[self._account_key_arn()],
                conditions={
                    "ForAnyValue:StringEquals": {
                        "kms:ResourceAliases": [data_key_alias],
                    },
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"secretsmanager.{self.region}.{self.url_suffix}"),
                    },
                },
            )
        )

    def _grant_runtime_identity_rotation(
        self,
        role: iam.Role,
        *,
        runtime_identity_secret: secretsmanager.CfnSecret,
        coordinator_key: kms.IKey,
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InstallLaunchRuntimeIdentity",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                ],
                resources=[runtime_identity_secret.attr_id],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EncryptLaunchRuntimeIdentity",
                actions=[
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"secretsmanager.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )

    def _grant_certification_access(
        self,
        role: iam.Role,
        *,
        managed_identity: bool,
        deployment_namespace: str,
    ) -> None:
        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectCertificationRuntime",
                actions=[
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=self._agentcore_runtime_arns(
                    deployment_namespace=deployment_namespace,
                ),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectCertificationStack",
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=(f"AxonLLMAgentCoreStack{physical_suffix}/*"),
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ManageOwnedCertificationFixtures",
                actions=[
                    "dynamodb:DeleteItem",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                ],
                resources=self._agentcore_state_table_arns(
                    deployment_namespace=deployment_namespace,
                ),
            )
        )
        if managed_identity:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="ManageOwnedCertificationUsers",
                    actions=[
                        "cognito-idp:AdminGetUser",
                        "cognito-idp:AdminCreateUser",
                        "cognito-idp:AdminDeleteUser",
                        "cognito-idp:AdminInitiateAuth",
                        "cognito-idp:AdminRespondToAuthChallenge",
                        "cognito-idp:AdminUserGlobalSignOut",
                        "cognito-idp:AssociateSoftwareToken",
                        "cognito-idp:DescribeUserPool",
                        "cognito-idp:DescribeUserPoolClient",
                        "cognito-idp:VerifySoftwareToken",
                    ],
                    resources=[
                        self.format_arn(
                            service="cognito-idp",
                            resource="userpool",
                            resource_name="*",
                        )
                    ],
                    conditions={
                        "StringEquals": {
                            "aws:ResourceTag/aws:cloudformation:stack-name": (f"AxonLLMIdentityStack{physical_suffix}")
                        }
                    },
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="InspectCertificationTargetHealth",
                    actions=[
                        "elasticloadbalancing:DescribeTargetHealth",
                    ],
                    resources=[
                        self.format_arn(
                            service="elasticloadbalancing",
                            resource="targetgroup",
                            resource_name="*/*",
                        )
                    ],
                    conditions={
                        "StringEquals": {
                            "aws:ResourceTag/aws:cloudformation:stack-name": (
                                f"AxonLLMControlPlaneStack{physical_suffix}"
                            )
                        }
                    },
                )
            )

    def _create_qualification_mutation_broker(
        self,
        *,
        authorization_table: dynamodb.ITable,
        log_encryption_key: kms.IKey,
    ) -> lambda_.Version:
        execution_role_arn = self.format_arn(
            service="iam",
            region="",
            resource="role",
            resource_name=(
                f"cdk-{_QUALIFICATION_CDK_QUALIFIER}-cfn-exec-role-"
                f"{self.account}-{self.region}"
            ),
        )
        role = iam.Role(
            self,
            "QualificationMutationBrokerRole",
            role_name="AxonLLMQualificationMutationBrokerRole",
            description=(
                "Executes only coordinator-authorized qualification selector "
                "mutations"
            ),
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            max_session_duration=Duration.hours(1),
        )
        log_group = logs.LogGroup(
            self,
            "QualificationMutationBrokerLogGroup",
            log_group_name=_QUALIFICATION_MUTATION_BROKER_LOG_GROUP,
            encryption_key=log_encryption_key,
            retention=logs.RetentionDays.TEN_YEARS,
            deletion_protection_enabled=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteBrokerLogs",
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[f"{log_group.log_group_arn}:log-stream:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCoordinatorMutationAuthorization",
                actions=["dynamodb:GetItem"],
                resources=[authorization_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="MutateAuthorizedQualificationSelector",
                actions=[
                    "cloudformation:DescribeStacks",
                    "cloudformation:UpdateStack",
                ],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=f"{name}/*",
                    )
                    for name in (
                        "AxonLLMAgentCoreStack-managed",
                        "AxonLLMControlPlaneStack-managed",
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassQualificationCloudFormationExecutionRole",
                actions=["iam:PassRole"],
                resources=[execution_role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "cloudformation.amazonaws.com"
                    }
                },
            )
        )
        function = lambda_.Function(
            self,
            "QualificationMutationBroker",
            function_name=_QUALIFICATION_MUTATION_BROKER_NAME,
            description=(
                "Coordinator-authorized broker for one bounded qualification "
                "selector mutation per invocation"
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="index.lambda_handler",
            code=lambda_.Code.from_inline(
                self._mutation_broker_source(
                    "qualification_mutation_broker.py"
                )
            ),
            role=role,
            log_group=log_group,
            memory_size=256,
            timeout=Duration.seconds(30),
            reserved_concurrent_executions=1,
            environment={
                "AXON_QUALIFICATION_MUTATION_AUTHORIZATION_TABLE": (
                    authorization_table.table_arn
                ),
                "AXON_QUALIFICATION_PRIMARY_TABLE_NAME": (
                    "axonllm-agentcore-state-managed"
                ),
                "AXON_QUALIFICATION_CLOUDFORMATION_EXECUTION_ROLE_ARN": (
                    execution_role_arn
                ),
            },
        )
        function.node.add_dependency(log_group)
        version = function.current_version
        version.apply_removal_policy(RemovalPolicy.RETAIN)
        return version

    def _create_launch_coordinator(
        self,
        *,
        github_provider: iam.IOidcProvider,
        launch_gates_subject: str,
        launch_alarm_email: str,
        state_table_names: tuple[str, str],
    ) -> _LaunchCoordinator:
        coordinator_key = kms.Key(
            self,
            "LaunchCoordinatorKey",
            alias="alias/axonllm/agentcore-launch-coordinator",
            description=("Encrypts AxonLLM AgentCore launch coordinator state"),
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        coordinator_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCoordinatorLogEncryption",
                principals=[iam.ServicePrincipal(f"logs.{self.region}.{self.url_suffix}")],
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
                            [
                                (
                                    f"arn:{self.partition}:logs:{self.region}:"
                                    f"{self.account}:log-group:/aws/vendedlogs/"
                                    "states/AxonLLMLaunchCoordinator"
                                ),
                                (
                                    f"arn:{self.partition}:logs:{self.region}:"
                                    f"{self.account}:log-group:"
                                    f"{_QUALIFICATION_MUTATION_BROKER_LOG_GROUP}"
                                ),
                                (
                                    f"arn:{self.partition}:logs:{self.region}:"
                                    f"{self.account}:log-group:"
                                    f"{_PRODUCTION_MUTATION_BROKER_LOG_GROUP}"
                                ),
                            ]
                        )
                    }
                },
            )
        )
        coordinator_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCoordinatorAlarmEncryption",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": self.format_arn(
                            service="cloudwatch",
                            resource="alarm",
                            resource_name="axonllm-launch-*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME,
                        )
                    },
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            )
        )
        lease_table = dynamodb.Table(
            self,
            "LaunchCoordinatorLeaseTable",
            table_name="axonllm-launch-rehearsal-leases",
            partition_key=dynamodb.Attribute(
                name="leaseKey",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            deletion_protection=True,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=coordinator_key,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True,
                    recovery_period_in_days=35,
                )
            ),
            time_to_live_attribute="expiresAtEpoch",
            contributor_insights_specification=(dynamodb.ContributorInsightsSpecification(enabled=True)),
            removal_policy=RemovalPolicy.RETAIN,
            resource_policy=iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="DenyInsecureTransport",
                        effect=iam.Effect.DENY,
                        principals=[iam.AnyPrincipal()],
                        actions=["dynamodb:*"],
                        resources=["*"],
                        conditions={"Bool": {"aws:SecureTransport": "false"}},
                    )
                ]
            ),
        )
        lease_table.add_global_secondary_index(
            index_name=_OWNER_EXPIRY_INDEX_NAME,
            partition_key=dynamodb.Attribute(
                name="recordType",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="ownerExpiresAtEpoch",
                type=dynamodb.AttributeType.NUMBER,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        rehearsal_control_table = dynamodb.Table(
            self,
            "RehearsalControlLedgerTable",
            table_name="axonllm-rehearsal-control-ledger",
            partition_key=dynamodb.Attribute(
                name="ledger_key",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            deletion_protection=True,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=coordinator_key,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True,
                    recovery_period_in_days=35,
                )
            ),
            time_to_live_attribute="expires_at_epoch",
            contributor_insights_specification=(dynamodb.ContributorInsightsSpecification(enabled=True)),
            removal_policy=RemovalPolicy.RETAIN,
            resource_policy=iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="DenyInsecureTransport",
                        effect=iam.Effect.DENY,
                        principals=[iam.AnyPrincipal()],
                        actions=["dynamodb:*"],
                        resources=["*"],
                        conditions={"Bool": {"aws:SecureTransport": "false"}},
                    )
                ]
            ),
        )
        qualification_mutation_authorization_table = dynamodb.Table(
            self,
            "QualificationMutationAuthorizationTable",
            table_name=_QUALIFICATION_MUTATION_AUTHORIZATION_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="authorizationId",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            deletion_protection=True,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=coordinator_key,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True,
                    recovery_period_in_days=35,
                )
            ),
            time_to_live_attribute="expiresAtEpoch",
            contributor_insights_specification=(
                dynamodb.ContributorInsightsSpecification(enabled=True)
            ),
            removal_policy=RemovalPolicy.RETAIN,
            resource_policy=iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="DenyInsecureTransport",
                        effect=iam.Effect.DENY,
                        principals=[iam.AnyPrincipal()],
                        actions=["dynamodb:*"],
                        resources=["*"],
                        conditions={
                            "Bool": {"aws:SecureTransport": "false"}
                        },
                    )
                ]
            ),
        )
        qualification_mutation_broker_version = (
            self._create_qualification_mutation_broker(
                authorization_table=(
                    qualification_mutation_authorization_table
                ),
                log_encryption_key=coordinator_key,
            )
        )
        runtime_identity_secret = secretsmanager.CfnSecret(
            self,
            "LaunchRuntimeIdentitySecret",
            description=("Service-generated identity material for isolated AgentCore launch rehearsals"),
            generate_secret_string=(
                secretsmanager.CfnSecret.GenerateSecretStringProperty(
                    exclude_punctuation=True,
                    include_space=False,
                    password_length=64,
                    require_each_included_type=True,
                )
            ),
            kms_key_id=coordinator_key.key_arn,
            name=_LAUNCH_RUNTIME_IDENTITY_SECRET_NAME,
            tags=[
                CfnTag(
                    key=tag["key"],
                    value=tag["value"],
                )
                for tag in _COORDINATOR_TAGS
            ],
        )
        runtime_identity_secret.apply_removal_policy(RemovalPolicy.RETAIN)
        runtime_identity_policy = secretsmanager.CfnResourcePolicy(
            self,
            "LaunchRuntimeIdentitySecretPolicy",
            block_public_policy=True,
            secret_id=runtime_identity_secret.attr_id,
            resource_policy={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyInsecureTransport",
                        "Effect": "Deny",
                        "Principal": {"AWS": "*"},
                        "Action": "secretsmanager:*",
                        "Resource": "*",
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            },
        )
        runtime_identity_policy.apply_removal_policy(RemovalPolicy.RETAIN)

        activity_encryption = stepfunctions.CfnActivity.EncryptionConfigurationProperty(
            type="CUSTOMER_MANAGED_KMS_KEY",
            kms_key_id=coordinator_key.key_arn,
            kms_data_key_reuse_period_seconds=300,
        )
        action_activity = stepfunctions.CfnActivity(
            self,
            "LaunchActionActivity",
            name="axonllm-agentcore-launch-actions",
            encryption_configuration=activity_encryption,
            tags=_COORDINATOR_TAGS,
        )
        action_activity.apply_removal_policy(RemovalPolicy.RETAIN)
        cleanup_activity = stepfunctions.CfnActivity(
            self,
            "LaunchCleanupActivity",
            name="axonllm-agentcore-launch-cleanup",
            encryption_configuration=activity_encryption,
            tags=_COORDINATOR_TAGS,
        )
        cleanup_activity.apply_removal_policy(RemovalPolicy.RETAIN)

        state_machine_arn = self._launch_state_machine_arn()
        execution_role = iam.Role(
            self,
            "LaunchCoordinatorExecutionRole",
            role_name="AxonLLMLaunchCoordinatorExecutionRole",
            description=("Orchestrates fenced AgentCore launch rehearsal work"),
            assumed_by=iam.ServicePrincipal(
                "states.amazonaws.com",
                conditions={
                    "ArnLike": {"aws:SourceArn": state_machine_arn},
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            ),
            max_session_duration=Duration.hours(1),
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AdvanceFencedLaunchLease",
                actions=["dynamodb:UpdateItem"],
                resources=[lease_table.table_arn],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="IssueQualificationMutationAuthorizations",
                actions=["dynamodb:TransactWriteItems"],
                resources=[
                    qualification_mutation_authorization_table.table_arn
                ],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="DeliverCoordinatorLogs",
                actions=[
                    "logs:CreateLogDelivery",
                    "logs:DeleteLogDelivery",
                    "logs:DescribeLogGroups",
                    "logs:DescribeResourcePolicies",
                    "logs:GetLogDelivery",
                    "logs:ListLogDeliveries",
                    "logs:PutResourcePolicy",
                    "logs:UpdateLogDelivery",
                ],
                resources=["*"],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="PublishCoordinatorTraces",
                actions=[
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                    "xray:PutTelemetryRecords",
                    "xray:PutTraceSegments",
                ],
                resources=["*"],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EncryptLaunchCoordinatorExecutions",
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:EncryptionContext:aws:states:stateMachineArn": (
                            state_machine_arn
                        ),
                    }
                },
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EncryptLaunchCoordinatorActivities",
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:EncryptionContext:aws:states:activityArn": [
                            action_activity.attr_arn,
                            cleanup_activity.attr_arn,
                        ],
                    }
                },
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EncryptLaunchCoordinatorLogDelivery",
                actions=["kms:GenerateDataKey"],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:EncryptionContext:SourceArn": self.format_arn(
                            service="logs",
                            resource="*",
                        ),
                    }
                },
            )
        )
        self._ignore_cfn_lint_transact_write_items(execution_role)

        action_worker_role = self._launch_worker_role(
            "LaunchActionWorkerRole",
            role_name="AxonLLMLaunchActionWorkerRole",
            description=("Polls only the AgentCore launch action activity"),
            activity_arn=action_activity.attr_arn,
            lease_table=lease_table,
            rehearsal_control_table=rehearsal_control_table,
            runtime_identity_secret=runtime_identity_secret,
            coordinator_key=coordinator_key,
            state_table_names=state_table_names,
            qualification_mutation_broker_version=(
                qualification_mutation_broker_version
            ),
            cleanup=False,
        )
        cleanup_worker_role = self._launch_worker_role(
            "LaunchCleanupWorkerRole",
            role_name="AxonLLMLaunchCleanupWorkerRole",
            description=("Polls only durable AgentCore launch cleanup work"),
            activity_arn=cleanup_activity.attr_arn,
            lease_table=lease_table,
            rehearsal_control_table=rehearsal_control_table,
            runtime_identity_secret=runtime_identity_secret,
            coordinator_key=coordinator_key,
            state_table_names=state_table_names,
            qualification_mutation_broker_version=(
                qualification_mutation_broker_version
            ),
            cleanup=True,
        )

        log_group = logs.LogGroup(
            self,
            "LaunchCoordinatorLogGroup",
            log_group_name="/aws/vendedlogs/states/AxonLLMLaunchCoordinator",
            encryption_key=coordinator_key,
            retention=logs.RetentionDays.TEN_YEARS,
            deletion_protection_enabled=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        state_machine = stepfunctions.CfnStateMachine(
            self,
            "LaunchCoordinatorStateMachine",
            state_machine_name=_LAUNCH_STATE_MACHINE_NAME,
            state_machine_type="STANDARD",
            role_arn=execution_role.role_arn,
            definition=self._launch_coordinator_definition(
                lease_table_name=lease_table.table_name,
                action_activity_arn=action_activity.attr_arn,
                cleanup_activity_arn=cleanup_activity.attr_arn,
                qualification_mutation_authorization_table_name=(
                    qualification_mutation_authorization_table.table_name
                ),
                qualification_execution_role_arn=self.format_arn(
                    service="iam",
                    region="",
                    resource="role",
                    resource_name=(
                        f"cdk-{_QUALIFICATION_CDK_QUALIFIER}-"
                        f"cfn-exec-role-{self.account}-{self.region}"
                    ),
                ),
            ),
            encryption_configuration=(
                stepfunctions.CfnStateMachine.EncryptionConfigurationProperty(
                    type="CUSTOMER_MANAGED_KMS_KEY",
                    kms_key_id=coordinator_key.key_arn,
                    kms_data_key_reuse_period_seconds=300,
                )
            ),
            logging_configuration=(
                stepfunctions.CfnStateMachine.LoggingConfigurationProperty(
                    destinations=[
                        stepfunctions.CfnStateMachine.LogDestinationProperty(
                            cloud_watch_logs_log_group=(
                                stepfunctions.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                                    log_group_arn=log_group.log_group_arn
                                )
                            )
                        )
                    ],
                    include_execution_data=False,
                    level="ALL",
                )
            ),
            tracing_configuration=(stepfunctions.CfnStateMachine.TracingConfigurationProperty(enabled=True)),
            tags=_COORDINATOR_TAGS,
        )
        state_machine.apply_removal_policy(RemovalPolicy.RETAIN)
        version = stepfunctions.CfnStateMachineVersion(
            self,
            "LaunchCoordinatorStateMachineVersion",
            state_machine_arn=state_machine.attr_arn,
            state_machine_revision_id=(state_machine.attr_state_machine_revision_id),
            description=("Immutable production AgentCore launch coordinator"),
        )
        version.apply_removal_policy(RemovalPolicy.RETAIN)

        launch_role = iam.Role(
            self,
            "AgentCoreLaunchGatesRole",
            role_name="AxonLLMLaunchGatesRole",
            description=(
                "Starts and observes only the reviewed launch coordinator "
                "version from the protected GitHub production environment"
            ),
            assumed_by=self._github_principal(
                github_provider,
                subject=launch_gates_subject,
            ),
            max_session_duration=Duration.hours(3),
        )
        launch_role.add_to_policy(
            iam.PolicyStatement(
                sid="StartExactLaunchCoordinatorVersion",
                actions=["states:StartExecution"],
                resources=[version.attr_arn],
            )
        )
        launch_role.add_to_policy(
            iam.PolicyStatement(
                sid="ObserveLaunchCoordinatorExecutions",
                actions=[
                    "states:DescribeExecution",
                    "states:StopExecution",
                ],
                resources=[self._launch_execution_arn()],
            )
        )
        self._grant_coordinator_key_use(
            launch_role,
            coordinator_key=coordinator_key,
            service="states",
        )

        scheduler_dead_letter_queue = sqs.Queue(
            self,
            "LaunchCoordinatorSchedulerDeadLetterQueue",
            queue_name="axonllm-launch-coordinator-scheduler-dlq",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=coordinator_key,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )
        schedule_group = scheduler.CfnScheduleGroup(
            self,
            "LaunchCoordinatorScheduleGroup",
            name=_LAUNCH_SCHEDULE_GROUP_NAME,
            tags=[
                {
                    "key": tag["key"],
                    "value": tag["value"],
                }
                for tag in _COORDINATOR_TAGS
            ],
        )
        schedule_group.apply_removal_policy(RemovalPolicy.RETAIN)
        scheduler_role = iam.Role(
            self,
            "LaunchCoordinatorSchedulerRole",
            role_name="AxonLLMLaunchCoordinatorSchedulerRole",
            description=("Starts only scheduled cleanup and watchdog executions"),
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "ArnLike": {"aws:SourceArn": (self._launch_schedule_group_arn())},
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            ),
            max_session_duration=Duration.hours(1),
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                # Scheduler authorizes its templated StartExecution call
                # against the base ARN even when the target is versioned.
                sid="StartScheduledLaunchCoordinator",
                actions=["states:StartExecution"],
                resources=[self._launch_state_machine_arn()],
            )
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="SendFailedScheduleToDedicatedQueue",
                actions=["sqs:SendMessage"],
                resources=[scheduler_dead_letter_queue.queue_arn],
            )
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptBoundSchedulePayloads",
                actions=["kms:Decrypt"],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                    },
                    "ArnEquals": {
                        "kms:EncryptionContext:aws:scheduler:schedule:arn": [
                            self._launch_schedule_arn(
                                _LAUNCH_CLEANUP_SCHEDULE_NAME
                            ),
                            self._launch_schedule_arn(
                                _LAUNCH_WATCHDOG_SCHEDULE_NAME
                            ),
                        ]
                    },
                },
            )
        )
        self._grant_coordinator_key_use(
            scheduler_role,
            coordinator_key=coordinator_key,
            service="sqs",
        )
        self._launch_schedule(
            "LaunchCoordinatorCleanupSchedule",
            name=_LAUNCH_CLEANUP_SCHEDULE_NAME,
            expression="rate(15 minutes)",
            operation="cleanup-expired",
            group=schedule_group,
            state_machine_version_arn=version.attr_arn,
            role=scheduler_role,
            dead_letter_queue=scheduler_dead_letter_queue,
            coordinator_key=coordinator_key,
        )
        self._launch_schedule(
            "LaunchCoordinatorWatchdogSchedule",
            name=_LAUNCH_WATCHDOG_SCHEDULE_NAME,
            expression="rate(5 minutes)",
            operation="watchdog",
            group=schedule_group,
            state_machine_version_arn=version.attr_arn,
            role=scheduler_role,
            dead_letter_queue=scheduler_dead_letter_queue,
            coordinator_key=coordinator_key,
        )

        alarm_topic_name = "axonllm-launch-coordinator-alarms"
        alarm_topic_arn = self.format_arn(
            service="sns",
            resource=alarm_topic_name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        alarm_topic = sns.Topic(
            self,
            "LaunchCoordinatorAlarmTopic",
            topic_name=alarm_topic_name,
            display_name="AxonLLM launch coordinator alarms",
            enforce_ssl=True,
        )
        alarm_topic_resource = alarm_topic.node.default_child
        if not isinstance(alarm_topic_resource, sns.CfnTopic):
            raise TypeError("launch coordinator alarm topic must synthesize an AWS::SNS::Topic")
        alarm_topic_resource.kms_master_key_id = coordinator_key.key_arn
        coordinator_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowExactCoordinatorTopicEncryption",
                principals=[iam.ServicePrincipal("sns.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=["*"],
                conditions={
                    "ArnEquals": {
                        "aws:SourceArn": alarm_topic_arn,
                    },
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            )
        )
        alarm_topic.apply_removal_policy(RemovalPolicy.RETAIN)
        alarm_receipt_queue = sqs.Queue(
            self,
            "LaunchCoordinatorAlarmReceiptQueue",
            queue_name="axonllm-launch-coordinator-alarm-receipts",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=coordinator_key,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )
        alarm_topic.add_subscription(sns_subscriptions.EmailSubscription(launch_alarm_email))
        sns.CfnSubscription(
            self,
            "LaunchCoordinatorAlarmReceiptSubscription",
            endpoint=alarm_receipt_queue.queue_arn,
            protocol="sqs",
            raw_message_delivery=True,
            topic_arn=alarm_topic.topic_arn,
        )
        alarm_receipt_queue.add_to_resource_policy(
            iam.PolicyStatement(
                sid="ReceiveExactCoordinatorAlarm",
                principals=[iam.ServicePrincipal("sns.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[alarm_receipt_queue.queue_arn],
                conditions={
                    "ArnEquals": {
                        "aws:SourceArn": alarm_topic.topic_arn,
                    },
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
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
                        "aws:SourceArn": self.format_arn(
                            service="cloudwatch",
                            resource="alarm",
                            resource_name="axonllm-launch-*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME,
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )
        launch_role.add_to_policy(
            iam.PolicyStatement(
                sid="VerifyLaunchAlarmDelivery",
                actions=[
                    "sns:GetTopicAttributes",
                    "sns:ListSubscriptionsByTopic",
                    "sns:Publish",
                ],
                resources=[alarm_topic.topic_arn],
            )
        )
        launch_role.add_to_policy(
            iam.PolicyStatement(
                sid="ConsumeLaunchAlarmReceipt",
                actions=[
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:ReceiveMessage",
                ],
                resources=[alarm_receipt_queue.queue_arn],
            )
        )
        launch_role.add_to_policy(
            iam.PolicyStatement(
                sid="EncryptLaunchAlarmViaSns",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sns.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )
        launch_role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptLaunchAlarmReceiptViaSqs",
                actions=["kms:Decrypt"],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sqs.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )
        watchdog_alarm = cloudwatch.Alarm(
            self,
            "LaunchCoordinatorWatchdogAlarm",
            alarm_name="axonllm-launch-rehearsal-watchdog",
            alarm_description=("The independent launch cleanup worker stopped reporting"),
            metric=cloudwatch.Metric(
                namespace="AxonLLM/LaunchCoordinator",
                metric_name="WatchdogHeartbeat",
                dimensions_map={"Coordinator": _LAUNCH_STATE_MACHINE_NAME},
                period=Duration.minutes(5),
                statistic="Minimum",
            ),
            threshold=1,
            comparison_operator=(cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD),
            evaluation_periods=2,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        execution_failure_alarm = cloudwatch.Alarm(
            self,
            "LaunchCoordinatorExecutionFailureAlarm",
            alarm_name="axonllm-launch-coordinator-execution-failures",
            alarm_description=("The launch coordinator failed or timed out"),
            metric=cloudwatch.MathExpression(
                expression="failed + timed_out",
                using_metrics={
                    "failed": cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsFailed",
                        dimensions_map={"StateMachineArn": state_machine.attr_arn},
                        period=Duration.minutes(5),
                        statistic="Sum",
                    ),
                    "timed_out": cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsTimedOut",
                        dimensions_map={"StateMachineArn": state_machine.attr_arn},
                        period=Duration.minutes(5),
                        statistic="Sum",
                    ),
                },
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=(cloudwatch.TreatMissingData.NOT_BREACHING),
        )
        scheduler_dead_letter_alarm = cloudwatch.Alarm(
            self,
            "LaunchCoordinatorSchedulerDeadLetterAlarm",
            alarm_name="axonllm-launch-coordinator-scheduler-dead-letters",
            alarm_description=("A scheduled launch cleanup or watchdog invocation failed"),
            metric=(
                scheduler_dead_letter_queue.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(5),
                    statistic="Maximum",
                )
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=(cloudwatch.TreatMissingData.NOT_BREACHING),
        )
        alarm_action = cloudwatch_actions.SnsAction(alarm_topic)
        for alarm in (
            watchdog_alarm,
            execution_failure_alarm,
            scheduler_dead_letter_alarm,
        ):
            alarm.add_alarm_action(alarm_action)
            alarm.add_ok_action(alarm_action)

        return _LaunchCoordinator(
            key=coordinator_key,
            lease_table=lease_table,
            rehearsal_control_table=rehearsal_control_table,
            qualification_mutation_authorization_table=(
                qualification_mutation_authorization_table
            ),
            qualification_mutation_broker_version=(
                qualification_mutation_broker_version
            ),
            runtime_identity_secret=runtime_identity_secret,
            state_machine=state_machine,
            version=version,
            execution_role=execution_role,
            launch_role=launch_role,
            action_activity=action_activity,
            cleanup_activity=cleanup_activity,
            action_worker_role=action_worker_role,
            cleanup_worker_role=cleanup_worker_role,
            scheduler_role=scheduler_role,
            schedule_group=schedule_group,
            scheduler_dead_letter_queue=scheduler_dead_letter_queue,
            watchdog_alarm=watchdog_alarm,
            alarm_topic=alarm_topic,
            alarm_receipt_queue=alarm_receipt_queue,
        )

    def _launch_worker_role(
        self,
        construct_id: str,
        *,
        role_name: str,
        description: str,
        activity_arn: str,
        lease_table: dynamodb.ITable,
        rehearsal_control_table: dynamodb.ITable,
        runtime_identity_secret: secretsmanager.CfnSecret,
        coordinator_key: kms.IKey,
        state_table_names: tuple[str, str],
        qualification_mutation_broker_version: lambda_.IVersion,
        cleanup: bool,
    ) -> iam.Role:
        role = iam.Role(
            self,
            construct_id,
            role_name=role_name,
            description=description,
            assumed_by=iam.ServicePrincipal(
                "ecs-tasks.amazonaws.com",
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": self.format_arn(
                            service="ecs",
                            resource="*",
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            ),
            max_session_duration=Duration.hours(1),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PollDedicatedLaunchActivity",
                actions=["states:GetActivityTask"],
                resources=[activity_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseRehearsalControlLedger",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                ],
                resources=[rehearsal_control_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadLaunchRuntimeIdentity",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[runtime_identity_secret.attr_id],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CompleteAssignedLaunchTask",
                actions=[
                    "states:SendTaskFailure",
                    "states:SendTaskHeartbeat",
                    "states:SendTaskSuccess",
                ],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
            )
        )
        lease_resources = [lease_table.table_arn]
        if cleanup:
            lease_resources.append(f"{lease_table.table_arn}/index/{_OWNER_EXPIRY_INDEX_NAME}")
        role.add_to_policy(
            iam.PolicyStatement(
                sid=("CleanOwnedFencedLaunchLeases" if cleanup else "UseAssignedFencedLaunchLease"),
                actions=(
                    [
                        "dynamodb:BatchWriteItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:GetItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                        "dynamodb:TransactWriteItems",
                        "dynamodb:UpdateItem",
                    ]
                    if cleanup
                    else [
                        "dynamodb:GetItem",
                        "dynamodb:TransactWriteItems",
                        "dynamodb:UpdateItem",
                    ]
                ),
                resources=lease_resources,
            )
        )
        if cleanup:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="PublishLaunchWatchdogHeartbeat",
                    actions=["cloudwatch:PutMetricData"],
                    resources=["*"],
                    conditions={"StringEquals": {"cloudwatch:namespace": ("AxonLLM/LaunchCoordinator")}},
                )
            )
        self._grant_coordinator_key_use(
            role,
            coordinator_key=coordinator_key,
            service="states",
        )
        self._grant_coordinator_key_use(
            role,
            coordinator_key=coordinator_key,
            service="secretsmanager",
        )
        self._grant_launch_worker_domain_access(
            role,
            state_table_names=state_table_names,
            cleanup=cleanup,
        )
        qualification_mutation_broker_version.grant_invoke(role)
        self._ignore_cfn_lint_transact_write_items(role)
        return role

    @staticmethod
    def _ignore_cfn_lint_transact_write_items(role: iam.Role) -> None:
        default_policy = role.node.try_find_child("DefaultPolicy")
        if not isinstance(default_policy, iam.Policy):
            raise TypeError("role with DynamoDB transactions has no default policy")
        cfn_policy = default_policy.node.default_child
        if not isinstance(cfn_policy, iam.CfnPolicy):
            raise TypeError("role default policy has no CfnPolicy child")
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )

    def _grant_launch_worker_domain_access(
        self,
        role: iam.Role,
        *,
        state_table_names: tuple[str, str],
        cleanup: bool,
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                sid=("VerifyCleanedAgentCoreRuntime" if cleanup else "ExerciseBoundAgentCoreRuntime"),
                actions=[
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=self._agentcore_qualification_runtime_arns(),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=("RemoveOwnedDomainState" if cleanup else "ExerciseOwnedDomainState"),
                actions=(
                    [
                        "dynamodb:BatchWriteItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:DeleteTable",
                        "dynamodb:DescribeContinuousBackups",
                        "dynamodb:DescribeTable",
                        "dynamodb:DescribeTimeToLive",
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                        "dynamodb:UpdateContinuousBackups",
                        "dynamodb:UpdateItem",
                        "dynamodb:UpdateTable",
                        "dynamodb:UpdateTimeToLive",
                    ]
                    if cleanup
                    else [
                        "dynamodb:DeleteItem",
                        "dynamodb:DeleteTable",
                        "dynamodb:DescribeContinuousBackups",
                        "dynamodb:DescribeTable",
                        "dynamodb:DescribeTimeToLive",
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:Query",
                        "dynamodb:RestoreTableToPointInTime",
                        "dynamodb:Scan",
                        "dynamodb:UpdateContinuousBackups",
                        "dynamodb:UpdateItem",
                        "dynamodb:UpdateTable",
                        "dynamodb:UpdateTimeToLive",
                    ]
                ),
                resources=self._agentcore_qualification_state_table_arns(),
            )
        )
        queue_arns = [
            self.format_arn(
                service="sqs",
                resource=pattern,
                arn_format=ArnFormat.NO_RESOURCE_NAME,
            )
            for pattern in (
                "AxonLLMAgentCoreStack-managed-SecurityEventOutboxQueue*",
                "AxonLLMAgentCoreStack-managed-SecurityEventDeadLetterQueue*",
            )
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                sid=("DrainOwnedSecurityEventFixtures" if cleanup else "ExerciseSecurityEventDelivery"),
                actions=[
                    "sqs:ChangeMessageVisibility",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:ReceiveMessage",
                    *([] if cleanup else ["sqs:SendMessage"]),
                ],
                resources=queue_arns,
            )
        )
        if not cleanup:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="InspectRehearsalAlarms",
                    actions=["cloudwatch:DescribeAlarms"],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
                )
            )
        qualification_stack_arns = [
            self.format_arn(
                service="cloudformation",
                resource="stack",
                resource_name=(f"{name}-{_QUALIFICATION_NAMESPACE}/*"),
            )
            for name in (
                "AxonLLMAgentCoreStack",
                "AxonLLMControlPlaneStack",
            )
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectReviewedQualificationSelectors",
                actions=["cloudformation:DescribeStacks"],
                resources=qualification_stack_arns,
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectQualificationControlService",
                actions=["ecs:DescribeServices"],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ResizeQualificationControlService",
                actions=["ecs:UpdateService"],
                resources=[
                    self.format_arn(
                        service="ecs",
                        resource="service",
                        resource_name=("*/AxonLLMControlPlaneStack-managed-Service*"),
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ManageQualificationControlScaling",
                actions=[
                    "application-autoscaling:DescribeScalableTargets",
                    "application-autoscaling:RegisterScalableTarget",
                ],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
            )
        )
        log_group = self.format_arn(
            service="logs",
            resource="log-group",
            resource_name=("AxonLLMAgentCoreStack-managed-SecurityEventLogGroup*"),
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=("RemoveOwnedSecurityEventStreams" if cleanup else "ExerciseSecurityEventLogDelivery"),
                actions=(
                    ["logs:DeleteLogStream"]
                    if cleanup
                    else [
                        "logs:CreateLogStream",
                        "logs:FilterLogEvents",
                    ]
                ),
                resources=[
                    log_group,
                    f"{log_group}:log-stream:axonllm-launch-*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseRuntimeDataKeyThroughDomainServices",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[self._account_key_arn()],
                conditions={
                    **self._qualification_data_key_alias_condition(),
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": [
                            (f"dynamodb.{self.region}.{self.url_suffix}"),
                            f"sqs.{self.region}.{self.url_suffix}",
                        ],
                    },
                },
            )
        )
        if not cleanup:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="GrantAgentCoreStateKeysForRestore",
                    actions=["kms:CreateGrant"],
                    resources=[self._account_key_arn()],
                    conditions={
                        **self._qualification_data_key_alias_condition(),
                        "Bool": {"kms:GrantIsForAWSResource": "true"},
                        "StringEquals": {
                            "kms:CallerAccount": self.account,
                            "kms:ViaService": (f"dynamodb.{self.region}.{self.url_suffix}"),
                        },
                    },
                )
            )

    def _launch_schedule(
        self,
        construct_id: str,
        *,
        name: str,
        expression: str,
        operation: str,
        group: scheduler.CfnScheduleGroup,
        state_machine_version_arn: str,
        role: iam.IRole,
        dead_letter_queue: sqs.IQueue,
        coordinator_key: kms.IKey,
    ) -> None:
        schedule = scheduler.CfnSchedule(
            self,
            construct_id,
            name=name,
            group_name=group.name,
            description=(f"Independent durable AgentCore launch coordinator {operation} invocation"),
            schedule_expression=expression,
            state="ENABLED",
            flexible_time_window=(scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF")),
            kms_key_arn=coordinator_key.key_arn,
            target=scheduler.CfnSchedule.TargetProperty(
                arn=state_machine_version_arn,
                role_arn=role.role_arn,
                input=json.dumps(
                    {
                        "operation": operation,
                        "schema": _LAUNCH_MAINTENANCE_SCHEMA,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                dead_letter_config=(scheduler.CfnSchedule.DeadLetterConfigProperty(arn=dead_letter_queue.queue_arn)),
                retry_policy=(
                    scheduler.CfnSchedule.RetryPolicyProperty(
                        maximum_event_age_in_seconds=86400,
                        maximum_retry_attempts=185,
                    )
                ),
            ),
        )
        schedule.apply_removal_policy(RemovalPolicy.RETAIN)

    def _grant_coordinator_key_use(
        self,
        role: iam.Role,
        *,
        coordinator_key: kms.IKey,
        service: str,
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                sid=f"UseCoordinatorKeyVia{service.title()}",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                ],
                resources=[coordinator_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"{service}.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )

    def _launch_state_machine_arn(self) -> str:
        return self.format_arn(
            service="states",
            resource="stateMachine",
            resource_name=_LAUNCH_STATE_MACHINE_NAME,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

    def _launch_execution_arn(self) -> str:
        return self.format_arn(
            service="states",
            resource="execution",
            resource_name=f"{_LAUNCH_STATE_MACHINE_NAME}:*",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

    def _launch_schedule_group_arn(self) -> str:
        return self.format_arn(
            service="scheduler",
            resource="schedule-group",
            resource_name=_LAUNCH_SCHEDULE_GROUP_NAME,
        )

    def _launch_schedule_arn(self, name: str) -> str:
        return self.format_arn(
            service="scheduler",
            resource="schedule",
            resource_name=f"{_LAUNCH_SCHEDULE_GROUP_NAME}/{name}",
        )

    @staticmethod
    def _launch_coordinator_definition(
        *,
        lease_table_name: str,
        action_activity_arn: str,
        cleanup_activity_arn: str,
        qualification_mutation_authorization_table_name: str,
        qualification_execution_role_arn: str,
    ) -> dict[str, object]:
        action_choices = [
            {
                "Variable": "$.operation",
                "StringEquals": action,
            }
            for action in _LAUNCH_WORKER_ACTIONS
        ]
        lease_key = {"leaseKey": {"S": "production"}}
        mark_lease_values = {
            ":fence": {
                "N.$": "$.lease.Attributes.fenceToken.N",
            },
            ":owner": {"S.$": "$.owner.id"},
        }

        def authorization_item(
            stack_kind: str,
            legal_edge: str,
        ) -> dict[str, object]:
            stack_path = (
                "$.binding.agentcoreStackArn"
                if stack_kind == "agentcore"
                else "$.binding.controlPlaneStackArn"
            )
            return {
                "Put": {
                    "TableName": (
                        qualification_mutation_authorization_table_name
                    ),
                    "Item": {
                        "schema": {
                            "S": (
                                "axonllm.qualification-selector-"
                                "authorization"
                            )
                        },
                        "version": {"N": "1"},
                        "authorizationId": {
                            "S.$": (
                                "States.Format('{}:{}:"
                                f"{stack_kind}:{legal_edge}', "
                                "$.owner.id, "
                                "$.lease.Attributes.fenceToken.N)"
                            )
                        },
                        "ownerId": {"S.$": "$.owner.id"},
                        "fenceToken": {
                            "N.$": "$.lease.Attributes.fenceToken.N"
                        },
                        "stackKind": {"S": stack_kind},
                        "legalEdge": {"S": legal_edge},
                        "status": {"S": "ACTIVE"},
                        "expiresAtEpoch": {
                            "N.$": (
                                "$.owner.authorizationExpiresAtEpoch"
                            )
                        },
                        "stackArn": {"S.$": stack_path},
                        "primaryTableName": {
                            "S.$": "$.parameters.primaryTableName"
                        },
                        "restoredTableName": {
                            "S.$": "$.parameters.restoredTableName"
                        },
                        "approvalId": {
                            "S.$": (
                                "States.Format('launch/{}', $.owner.id)"
                            )
                        },
                        "executionRoleArn": {
                            "S": qualification_execution_role_arn
                        },
                    },
                }
            }

        def authorization_state(
            edges: tuple[tuple[str, str], ...],
        ) -> dict[str, object]:
            return {
                "Type": "Task",
                "Resource": (
                    "arn:aws:states:::aws-sdk:"
                    "dynamodb:transactWriteItems"
                ),
                "Parameters": {
                    "TransactItems": [
                        authorization_item(stack_kind, legal_edge)
                        for stack_kind, legal_edge in edges
                    ],
                },
                "ResultPath": "$.mutationAuthorization",
                "Catch": [
                    {
                        "ErrorEquals": ["States.ALL"],
                        "ResultPath": "$.workerError",
                        "Next": "MarkActionFailed",
                    }
                ],
                "Next": "RunActionWorker",
            }

        primary_recovery_edges = tuple(
            (stack_kind, legal_edge)
            for stack_kind, legal_edge in (
                ("control-plane", "quiesce-restored"),
                ("agentcore", "quiesce-restored"),
                ("control-plane", "cutover-to-primary"),
                ("agentcore", "cutover-to-primary"),
                ("agentcore", "resume-primary"),
                ("control-plane", "resume-primary"),
            )
        )
        restored_cutover_edges = tuple(
            (stack_kind, legal_edge)
            for stack_kind, legal_edge in (
                ("control-plane", "quiesce-primary"),
                ("agentcore", "quiesce-primary"),
                ("control-plane", "cutover-to-restored"),
                ("agentcore", "cutover-to-restored"),
                ("agentcore", "resume-restored"),
                ("control-plane", "resume-restored"),
            )
        )
        return {
            "Comment": ("Fenced dispatcher for production AgentCore launch rehearsals"),
            "StartAt": "ValidateOperation",
            "TimeoutSeconds": 1800,
            "States": {
                "ValidateOperation": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "And": [
                                {
                                    "Variable": "$.schema",
                                    "StringEquals": _LAUNCH_ACTION_SCHEMA,
                                },
                                {
                                    "Variable": "$.operation",
                                    "StringEquals": "cleanup",
                                },
                            ],
                            "Next": "RunCleanupWorker",
                        },
                        {
                            "And": [
                                {
                                    "Variable": "$.schema",
                                    "StringEquals": _LAUNCH_ACTION_SCHEMA,
                                },
                                {"Or": action_choices},
                            ],
                            "Next": "AcquireActionLease",
                        },
                        {
                            "And": [
                                {
                                    "Variable": "$.schema",
                                    "StringEquals": (_LAUNCH_MAINTENANCE_SCHEMA),
                                },
                                {
                                    "Variable": "$.operation",
                                    "StringEquals": "cleanup-expired",
                                },
                            ],
                            "Next": "RunCleanupMaintenanceWorker",
                        },
                        {
                            "And": [
                                {
                                    "Variable": "$.schema",
                                    "StringEquals": (_LAUNCH_MAINTENANCE_SCHEMA),
                                },
                                {
                                    "Variable": "$.operation",
                                    "StringEquals": "watchdog",
                                },
                            ],
                            "Next": "RunCleanupWorker",
                        },
                    ],
                    "Default": "RejectOperation",
                },
                "RejectOperation": {
                    "Type": "Fail",
                    "Error": "UnsupportedLaunchOperation",
                    "Cause": ("The coordinator accepts only reviewed launch operations"),
                },
                "AcquireActionLease": {
                    "Type": "Task",
                    "Resource": ("arn:aws:states:::aws-sdk:dynamodb:updateItem"),
                    "Parameters": {
                        "TableName": lease_table_name,
                        "Key": lease_key,
                        "ConditionExpression": (
                            "attribute_not_exists(leaseKey) OR "
                            "#status = :available OR (ownerId = :owner "
                            "AND idempotencyKey = :idempotency)"
                        ),
                        "UpdateExpression": (
                            "SET ownerId = :owner, correlationId = "
                            ":correlation, idempotencyKey = :idempotency, "
                            "#status = :active, updatedAt = :updated "
                            "ADD fenceToken :one"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":active": {"S": "ACTIVE"},
                            ":available": {"S": "AVAILABLE"},
                            ":correlation": {"S.$": "$.correlationId"},
                            ":idempotency": {"S.$": "$.idempotencyKey"},
                            ":one": {"N": "1"},
                            ":owner": {"S.$": "$.owner.id"},
                            ":updated": {"S.$": "$$.State.EnteredTime"},
                        },
                        "ReturnValues": "ALL_NEW",
                    },
                    "ResultPath": "$.lease",
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.leaseError",
                            "Next": "LeaseRejected",
                        }
                    ],
                    "Next": "SelectMutationAuthorizations",
                },
                "LeaseRejected": {
                    "Type": "Fail",
                    "Error": "LaunchLeaseUnavailable",
                    "Cause": ("A different owner holds the production launch lease"),
                },
                "SelectMutationAuthorizations": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Or": [
                                {
                                    "Variable": "$.operation",
                                    "StringEquals": "restore-state",
                                },
                                {
                                    "Variable": "$.operation",
                                    "StringEquals": "rollback-primary-state",
                                },
                            ],
                            "Next": "AuthorizePrimarySelectorRecovery",
                        },
                        {
                            "Variable": "$.operation",
                            "StringEquals": "cutover-restored-state",
                            "Next": "AuthorizeRestoredSelectorCutover",
                        },
                    ],
                    "Default": "RunActionWorker",
                },
                "AuthorizePrimarySelectorRecovery": authorization_state(
                    primary_recovery_edges
                ),
                "AuthorizeRestoredSelectorCutover": authorization_state(
                    restored_cutover_edges
                ),
                "RunActionWorker": {
                    "Type": "Task",
                    "Resource": action_activity_arn,
                    "HeartbeatSeconds": 60,
                    "TimeoutSeconds": 1500,
                    "ResultPath": "$.workerResult",
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.workerError",
                            "Next": "MarkActionFailed",
                        }
                    ],
                    "Next": "MarkActionComplete",
                },
                "MarkActionComplete": {
                    "Type": "Task",
                    "Resource": ("arn:aws:states:::aws-sdk:dynamodb:updateItem"),
                    "Parameters": {
                        "TableName": lease_table_name,
                        "Key": lease_key,
                        "ConditionExpression": ("ownerId = :owner AND fenceToken = :fence AND #status = :active"),
                        "UpdateExpression": ("SET #status = :available, completedAt = :completed"),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            **mark_lease_values,
                            ":active": {"S": "ACTIVE"},
                            ":available": {"S": "AVAILABLE"},
                            ":completed": {"S.$": "$$.State.EnteredTime"},
                        },
                    },
                    "ResultPath": "$.leaseCompletion",
                    "Next": "ReturnActionResult",
                },
                "ReturnActionResult": {
                    "Type": "Pass",
                    "OutputPath": "$.workerResult",
                    "End": True,
                },
                "MarkActionFailed": {
                    "Type": "Task",
                    "Resource": ("arn:aws:states:::aws-sdk:dynamodb:updateItem"),
                    "Parameters": {
                        "TableName": lease_table_name,
                        "Key": lease_key,
                        "ConditionExpression": ("ownerId = :owner AND fenceToken = :fence AND #status = :active"),
                        "UpdateExpression": ("SET #status = :failed, failedAt = :failedAt"),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            **mark_lease_values,
                            ":active": {"S": "ACTIVE"},
                            ":failed": {"S": "FAILED"},
                            ":failedAt": {"S.$": "$$.State.EnteredTime"},
                        },
                    },
                    "ResultPath": "$.leaseFailure",
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.markFailureError",
                            "Next": "UnfencedWorkerFailure",
                        }
                    ],
                    "Next": "ActionWorkerFailed",
                },
                "ActionWorkerFailed": {
                    "Type": "Fail",
                    "Error": "LaunchActionWorkerFailed",
                    "Cause": ("The scoped action worker did not complete"),
                },
                "UnfencedWorkerFailure": {
                    "Type": "Fail",
                    "Error": "LaunchLeaseFenceLost",
                    "Cause": ("The failed worker no longer owns the launch fence"),
                },
                "RunCleanupWorker": {
                    "Type": "Task",
                    "Resource": cleanup_activity_arn,
                    "HeartbeatSeconds": 60,
                    "TimeoutSeconds": 1740,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.cleanupError",
                            "Next": "CleanupWorkerFailed",
                        }
                    ],
                    "End": True,
                },
                "RunCleanupMaintenanceWorker": {
                    "Type": "Task",
                    "Resource": cleanup_activity_arn,
                    "HeartbeatSeconds": 60,
                    "TimeoutSeconds": 300,
                    "ResultPath": "$.maintenanceResult",
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.cleanupError",
                            "Next": "CleanupWorkerFailed",
                        }
                    ],
                    "Next": "CheckCleanupContinuation",
                },
                "CheckCleanupContinuation": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "And": [
                                {
                                    "Variable": ("$.maintenanceResult.nextCursor"),
                                    "IsPresent": True,
                                },
                                {
                                    "Variable": ("$.maintenanceResult.nextCursor"),
                                    "IsNull": False,
                                },
                            ],
                            "Next": "PrepareCleanupContinuation",
                        }
                    ],
                    "Default": "ReturnCleanupMaintenanceResult",
                },
                "PrepareCleanupContinuation": {
                    "Type": "Pass",
                    "Parameters": {
                        "schema": _LAUNCH_MAINTENANCE_SCHEMA,
                        "operation": "cleanup-expired",
                        "cursor.$": "$.maintenanceResult.nextCursor",
                        "page.$": "$.maintenanceResult.page",
                    },
                    "Next": "RunCleanupMaintenanceWorker",
                },
                "ReturnCleanupMaintenanceResult": {
                    "Type": "Pass",
                    "OutputPath": "$.maintenanceResult",
                    "End": True,
                },
                "CleanupWorkerFailed": {
                    "Type": "Fail",
                    "Error": "LaunchCleanupWorkerFailed",
                    "Cause": ("The independent cleanup worker did not complete"),
                },
            },
        }

    def _grant_operations_audit(
        self,
        role: iam.Role,
        *,
        state_table_names: tuple[str, str],
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectRecoveryInfrastructure",
                actions=[
                    "cloudformation:DescribeStacks",
                    "cloudformation:ListStackResources",
                ],
                resources=self._runtime_stack_arns(),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectRecoveryPoints",
                actions=[
                    "backup:DescribeBackupVault",
                    "backup:ListRecoveryPointsByBackupVault",
                ],
                resources=self._backup_vault_arns(),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectStateProtection",
                actions=[
                    "dynamodb:DescribeContinuousBackups",
                    "dynamodb:DescribeTable",
                ],
                resources=self._source_table_arns(state_table_names),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectProviderSecretMetadata",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:ListSecretVersionIds",
                ],
                resources=[
                    self.format_arn(
                        service="secretsmanager",
                        resource="secret",
                        resource_name="AxonLLMStack-*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectDataKeyRotation",
                actions=[
                    "kms:DescribeKey",
                    "kms:GetKeyRotationStatus",
                ],
                resources=[self._account_key_arn()],
                conditions=self._data_key_alias_condition(),
            )
        )

    def _grant_operations_recovery(
        self,
        role: iam.Role,
        *,
        state_table_names: tuple[str, str],
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectRecoveryInfrastructure",
                actions=[
                    "cloudformation:DescribeStacks",
                    "cloudformation:ListStackResources",
                ],
                resources=self._runtime_stack_arns(),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectRecoveryPoints",
                actions=[
                    "backup:DescribeBackupVault",
                    "backup:ListRecoveryPointsByBackupVault",
                ],
                resources=self._backup_vault_arns(),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RestoreStateTable",
                actions=[
                    "dynamodb:DescribeContinuousBackups",
                    "dynamodb:DescribeTable",
                    "dynamodb:RestoreTableToPointInTime",
                    "dynamodb:Scan",
                ],
                resources=self._source_table_arns(state_table_names),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ValidateAndRemoveRestoredState",
                actions=[
                    "dynamodb:DeleteTable",
                    "dynamodb:DescribeContinuousBackups",
                    "dynamodb:DescribeTable",
                    "dynamodb:DescribeTimeToLive",
                    "dynamodb:GetItem",
                    "dynamodb:RestoreTableToPointInTime",
                    "dynamodb:Scan",
                    "dynamodb:UpdateContinuousBackups",
                    "dynamodb:UpdateTable",
                    "dynamodb:UpdateTimeToLive",
                ],
                resources=self._restored_table_arns(state_table_names),
            )
        )
        via_dynamodb = {
            "kms:CallerAccount": self.account,
            "kms:ViaService": (f"dynamodb.{self.region}.{self.url_suffix}"),
        }
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UseStateKeysForRestore",
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                    "kms:ReEncryptFrom",
                    "kms:ReEncryptTo",
                ],
                resources=[self._account_key_arn()],
                conditions={
                    **self._data_key_alias_condition(),
                    "StringEquals": via_dynamodb,
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="GrantStateKeysToDynamoDb",
                actions=["kms:CreateGrant"],
                resources=[self._account_key_arn()],
                conditions={
                    **self._data_key_alias_condition(),
                    "Bool": {"kms:GrantIsForAWSResource": "true"},
                    "StringEquals": via_dynamodb,
                },
            )
        )

    def _runtime_stack_arns(self) -> list[str]:
        return [
            self.format_arn(
                service="cloudformation",
                resource="stack",
                resource_name=f"{name}/*",
            )
            for name in ("AxonLLMStack", "AxonLLMAgentCoreStack")
        ]

    def _agentcore_runtime_arns(
        self,
        *,
        deployment_namespace: str,
    ) -> list[str]:
        runtime_name = "axonllm" if not deployment_namespace else f"axonllm_{deployment_namespace.replace('-', '_')}"
        runtime = self.format_arn(
            service="bedrock-agentcore",
            resource="runtime",
            resource_name=f"{runtime_name}-*",
        )
        return [
            runtime,
            f"{runtime}/runtime-endpoint/*",
        ]

    def _agentcore_qualification_runtime_arns(self) -> list[str]:
        return self._agentcore_runtime_arns(
            deployment_namespace=_QUALIFICATION_NAMESPACE,
        )

    def _agentcore_state_table_arns(
        self,
        *,
        deployment_namespace: str,
    ) -> list[str]:
        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        table = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=f"axonllm-agentcore-state{physical_suffix}",
        )
        return [
            table,
            f"{table}/index/*",
        ]

    def _agentcore_qualification_state_table_arns(self) -> list[str]:
        primary = self._agentcore_state_table_arns(
            deployment_namespace=_QUALIFICATION_NAMESPACE,
        )
        table = primary[0]
        restored = f"{table}-restore-validation-*"
        return [
            *primary,
            restored,
            f"{restored}/index/*",
        ]

    def _source_table_arns(
        self,
        state_table_names: tuple[str, str],
    ) -> list[str]:
        return [
            self.format_arn(
                service="dynamodb",
                resource="table",
                resource_name=name,
            )
            for name in state_table_names
        ]

    def _restored_table_arns(
        self,
        state_table_names: tuple[str, str],
    ) -> list[str]:
        return [f"{table_arn}-restore-validation-*" for table_arn in self._source_table_arns(state_table_names)]

    def _backup_vault_arns(self) -> list[str]:
        return [
            self.format_arn(
                service="backup",
                resource="backup-vault",
                resource_name=prefix,
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )
            for prefix in ("axon-state-*", "axon-agent-*")
        ]

    def _account_key_arn(self) -> str:
        return self.format_arn(
            service="kms",
            resource="key",
            resource_name="*",
        )

    @staticmethod
    def _data_key_alias_condition() -> dict[str, dict[str, list[str]]]:
        return {
            "ForAnyValue:StringEquals": {
                "kms:ResourceAliases": [
                    "alias/axonllm/data",
                    "alias/axonllm/agentcore-data",
                ]
            }
        }

    @staticmethod
    def _qualification_data_key_alias_condition() -> dict[str, dict[str, list[str]]]:
        return {
            "ForAnyValue:StringEquals": {
                "kms:ResourceAliases": [
                    (f"alias/axonllm/agentcore-data-{_QUALIFICATION_NAMESPACE}"),
                ]
            }
        }

    @staticmethod
    def _release_signing_alias_condition() -> dict[
        str,
        dict[str, list[str]],
    ]:
        return {
            "ForAnyValue:StringLike": {
                "kms:ResourceAliases": [
                    "alias/axonllm/release-signing-v*",
                ]
            }
        }

    @staticmethod
    def _cdk_qualifier(deployment_namespace: str) -> str:
        if not deployment_namespace:
            return _PRODUCTION_CDK_QUALIFIER
        if deployment_namespace in {"external", "external-oidc"}:
            return _EXTERNAL_CDK_QUALIFIER
        return _QUALIFICATION_CDK_QUALIFIER

    @staticmethod
    def _github_principal(
        provider: iam.IOidcProvider,
        *,
        subject: str,
    ) -> iam.OpenIdConnectPrincipal:
        return iam.OpenIdConnectPrincipal(
            provider,
            conditions={
                "StringEquals": {
                    f"{_GITHUB_OIDC_ISSUER}:aud": "sts.amazonaws.com",
                    f"{_GITHUB_OIDC_ISSUER}:sub": subject,
                }
            },
        )

    @staticmethod
    def _github_signing_principal(
        provider: iam.IOidcProvider,
        *,
        subject: str,
    ) -> iam.OpenIdConnectPrincipal:
        return iam.OpenIdConnectPrincipal(
            provider,
            conditions={
                "StringEquals": {
                    f"{_GITHUB_OIDC_ISSUER}:aud": "sts.amazonaws.com",
                },
                "StringLike": {
                    f"{_GITHUB_OIDC_ISSUER}:sub": subject,
                },
            },
        )
