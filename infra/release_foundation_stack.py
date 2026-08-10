"""Private release registries and GitHub OIDC roles for AxonLLM."""

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_kms as kms,
)
from constructs import Construct


_GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"
_GITHUB_RELEASE_SUBJECT = "repo:AxonLLM/axonllm:environment:release"
_GITHUB_PRODUCTION_SUBJECT = (
    "repo:AxonLLM/axonllm:environment:production"
)


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
            raise ValueError(
                "AxonLLM release foundation must be deployed in us-east-1"
            )

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
            description=(
                "Physical state table name used by AxonLLMAgentCoreStack"
            ),
        )
        state_table_names = (
            fargate_state_table_name.value_as_string,
            agentcore_state_table_name.value_as_string,
        )

        release_key = kms.Key(
            self,
            "ReleaseRegistryKey",
            alias="alias/axonllm/release-ecr",
            description="AxonLLM private release registry encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
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

        publisher = iam.Role(
            self,
            "ReleasePublisherRole",
            role_name="AxonLLMReleasePublisher",
            description=(
                "Publishes verified AxonLLM release artifacts from the "
                "protected GitHub release environment"
            ),
            assumed_by=self._github_principal(
                github_provider,
                subject=_GITHUB_RELEASE_SUBJECT,
            ),
            max_session_duration=Duration.hours(1),
        )
        verifier = iam.Role(
            self,
            "ReleaseVerifierRole",
            role_name="AxonLLMReleaseVerifier",
            description=(
                "Reads immutable AxonLLM images during deployment verification"
            ),
            assumed_by=self._github_principal(
                github_provider,
                subject=_GITHUB_PRODUCTION_SUBJECT,
            ),
            max_session_duration=Duration.hours(1),
        )
        operations_audit = iam.Role(
            self,
            "OperationsAuditRole",
            role_name="AxonLLMOperationsAudit",
            description=(
                "Audits AxonLLM recovery and secret-rotation metadata from "
                "the protected GitHub production environment"
            ),
            assumed_by=self._github_principal(
                github_provider,
                subject=_GITHUB_PRODUCTION_SUBJECT,
            ),
            max_session_duration=Duration.hours(1),
        )
        operations_recovery = iam.Role(
            self,
            "OperationsRecoveryRole",
            role_name="AxonLLMOperationsRecovery",
            description=(
                "Exercises AxonLLM point-in-time recovery from the protected "
                "GitHub production environment"
            ),
            assumed_by=self._github_principal(
                github_provider,
                subject=_GITHUB_PRODUCTION_SUBJECT,
            ),
            max_session_duration=Duration.hours(2),
        )

        repository_arns = [
            repository.repository_arn
            for repository in repositories.values()
        ]
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
            "kms:ViaService": (
                f"dynamodb.{self.region}.{self.url_suffix}"
            ),
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
        return [
            f"{table_arn}-restore-validation-*"
            for table_arn in self._source_table_arns(state_table_names)
        ]

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
