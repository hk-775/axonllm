"""Security contracts for the release-foundation CDK target."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_RELEASE_SUBJECT = "repo:AxonLLM/axonllm:environment:release"
_PRODUCTION_SUBJECT = "repo:AxonLLM/axonllm:environment:production"
_WRITE_ACTIONS = {
    "ecr:CompleteLayerUpload",
    "ecr:InitiateLayerUpload",
    "ecr:PutImage",
    "ecr:UploadLayerPart",
}
_READ_ACTIONS = {
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
}


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return {actions}
    return set(actions)


@pytest.fixture(scope="module")
def synthesized_template(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("release-foundation")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_target": "release-foundation",
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(work_dir / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout
    return json.loads(
        (
            out_dir
            / "AxonLLMReleaseFoundationStack.template.json"
        ).read_text(encoding="utf-8")
    )


def test_release_repositories_are_retained_immutable_and_kms_encrypted(
    synthesized_template,
):
    repositories = _resources(
        synthesized_template,
        "AWS::ECR::Repository",
    )
    assert len(repositories) == 2
    assert {
        repository["Properties"]["RepositoryName"]
        for repository in repositories
    } == {"axonllm/fargate", "axonllm/agentcore"}

    for repository in repositories:
        properties = repository["Properties"]
        assert properties["ImageTagMutability"] == "IMMUTABLE"
        assert properties["ImageScanningConfiguration"] == {
            "ScanOnPush": True
        }
        assert properties["EncryptionConfiguration"]["EncryptionType"] == "KMS"
        assert "KmsKey" in properties["EncryptionConfiguration"]
        assert repository["DeletionPolicy"] == "Retain"
        assert repository["UpdateReplacePolicy"] == "Retain"


def test_registry_key_rotates_and_is_retained(synthesized_template):
    key = _resources(synthesized_template, "AWS::KMS::Key")
    assert len(key) == 1
    assert key[0]["Properties"]["EnableKeyRotation"] is True
    assert key[0]["DeletionPolicy"] == "Retain"
    assert key[0]["UpdateReplacePolicy"] == "Retain"

    alias = _resources(synthesized_template, "AWS::KMS::Alias")
    assert len(alias) == 1
    assert alias[0]["Properties"]["AliasName"] == (
        "alias/axonllm/release-ecr"
    )


def test_github_oidc_trust_is_exact_and_retained(synthesized_template):
    providers = _resources(
        synthesized_template,
        "AWS::IAM::OIDCProvider",
    )
    assert len(providers) == 1
    provider = providers[0]
    assert provider["Properties"]["Url"] == (
        "https://token.actions.githubusercontent.com"
    )
    assert provider["Properties"]["ClientIdList"] == ["sts.amazonaws.com"]
    assert provider["DeletionPolicy"] == "Retain"
    assert provider["UpdateReplacePolicy"] == "Retain"

    roles = _resources(synthesized_template, "AWS::IAM::Role")
    assert len(roles) == 2
    assert {
        role["Properties"]["RoleName"] for role in roles
    } == {"AxonLLMReleasePublisher", "AxonLLMReleaseVerifier"}
    for role in roles:
        assert role["Properties"]["MaxSessionDuration"] == 3600
        statements = role["Properties"][
            "AssumeRolePolicyDocument"
        ]["Statement"]
        assert len(statements) == 1
        statement = statements[0]
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        conditions = statement["Condition"]["StringEquals"]
        assert conditions[
            "token.actions.githubusercontent.com:aud"
        ] == "sts.amazonaws.com"
        expected_subject = (
            _RELEASE_SUBJECT
            if role["Properties"]["RoleName"]
            == "AxonLLMReleasePublisher"
            else _PRODUCTION_SUBJECT
        )
        assert conditions[
            "token.actions.githubusercontent.com:sub"
        ] == expected_subject


def test_publisher_and_verifier_permissions_are_separate(
    synthesized_template,
):
    policies = _resources(synthesized_template, "AWS::IAM::Policy")
    assert len(policies) == 2
    by_role: dict[str, list[dict]] = {}
    for policy in policies:
        role = policy["Properties"]["Roles"][0]["Ref"]
        by_role[role] = policy["Properties"]["PolicyDocument"]["Statement"]

    publisher = next(
        statements
        for role, statements in by_role.items()
        if "ReleasePublisherRole" in role
    )
    verifier = next(
        statements
        for role, statements in by_role.items()
        if "ReleaseVerifierRole" in role
    )
    publisher_actions = {
        action for statement in publisher for action in _actions(statement)
    }
    verifier_actions = {
        action for statement in verifier for action in _actions(statement)
    }

    assert _WRITE_ACTIONS <= publisher_actions
    assert _READ_ACTIONS <= publisher_actions
    assert "ecr:DescribeImages" in publisher_actions
    assert _READ_ACTIONS <= verifier_actions
    assert "ecr:DescribeImages" not in verifier_actions
    assert _WRITE_ACTIONS.isdisjoint(verifier_actions)
    assert "ecr:BatchDeleteImage" not in publisher_actions
    assert "ecr:DeleteRepository" not in publisher_actions
    assert "ecr:GetAuthorizationToken" in publisher_actions
    assert "ecr:GetAuthorizationToken" in verifier_actions


def test_repository_policy_denies_insecure_transport(
    synthesized_template,
):
    policies = _resources(
        synthesized_template,
        "AWS::ECR::Repository",
    )
    for repository in policies:
        statements = repository["Properties"]["RepositoryPolicyText"][
            "Statement"
        ]
        deny = next(
            statement
            for statement in statements
            if statement.get("Sid") == "DenyInsecureTransport"
        )
        assert deny["Effect"] == "Deny"
        assert deny["Principal"] == {"AWS": "*"}
        assert deny["Action"] == "ecr:*"
        assert "Resource" not in deny
        assert deny["Condition"] == {
            "Bool": {"aws:SecureTransport": "false"}
        }


def test_foundation_outputs_all_operator_inputs(synthesized_template):
    assert set(synthesized_template["Outputs"]) == {
        "AgentCoreRepositoryUri",
        "FargateRepositoryUri",
        "GitHubOidcProviderArn",
        "ReleasePublisherRoleArn",
        "ReleaseRegistryKeyArn",
        "ReleaseVerifierRoleArn",
    }


def test_foundation_rejects_other_regions(tmp_path):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    script = """
import aws_cdk as cdk
from release_foundation_stack import AxonLLMReleaseFoundationStack

app = cdk.App()
AxonLLMReleaseFoundationStack(
    app,
    "WrongRegion",
    env=cdk.Environment(account="111111111111", region="us-west-2"),
)
"""
    environment = os.environ.copy()
    environment.update(
        {
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(
                tmp_path / "jsii-cache"
            ),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "-c", script],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "must be deployed in us-east-1" in completed.stdout
