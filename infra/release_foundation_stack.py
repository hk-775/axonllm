"""Private release registries and GitHub OIDC roles for AxonLLM."""

from aws_cdk import (
    CfnOutput,
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
