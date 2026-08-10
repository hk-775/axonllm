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
_SUBJECT_PREFIX = "repo:AxonLLM@313590914/axonllm@1276398779"
_SIGNING_SUBJECT = f"{_SUBJECT_PREFIX}:ref:refs/tags/v*"
_RELEASE_SUBJECT = f"{_SUBJECT_PREFIX}:environment:release"
_PRODUCTION_SUBJECT = f"{_SUBJECT_PREFIX}:environment:production"
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


def _literal_parts(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        separator, parts = value["Fn::Join"]
        return separator.join(
            part for part in parts if isinstance(part, str)
        )
    return ""


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


def test_release_keys_are_retained_and_purpose_scoped(synthesized_template):
    keys = _resources(synthesized_template, "AWS::KMS::Key")
    assert len(keys) == 2
    signing_key_logical_id, signing_key = next(
        (logical_id, resource)
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource["Type"] == "AWS::KMS::Key"
        and resource["Properties"]["Description"]
        == "AxonLLM private release evidence signing"
    )
    registry_key = next(
        key
        for key in keys
        if key["Properties"]["Description"]
        == "AxonLLM private release registry encryption"
    )
    assert registry_key["Properties"]["EnableKeyRotation"] is True
    assert "KeySpec" not in registry_key["Properties"]
    assert signing_key["Properties"]["KeySpec"] == "ECC_NIST_P256"
    assert signing_key["Properties"]["KeyUsage"] == "SIGN_VERIFY"
    assert "EnableKeyRotation" not in signing_key["Properties"]
    for key in keys:
        assert key["DeletionPolicy"] == "Retain"
        assert key["UpdateReplacePolicy"] == "Retain"

    aliases = _resources(synthesized_template, "AWS::KMS::Alias")
    aliases_by_name = {
        alias["Properties"]["AliasName"]: alias for alias in aliases
    }
    assert set(aliases_by_name) == {
        "alias/axonllm/release-ecr",
        "alias/axonllm/release-signing",
        "alias/axonllm/release-signing-v1",
    }
    signing_key_target = {
        "Fn::GetAtt": [signing_key_logical_id, "Arn"],
    }
    for alias_name in (
        "alias/axonllm/release-signing",
        "alias/axonllm/release-signing-v1",
    ):
        assert (
            aliases_by_name[alias_name]["Properties"]["TargetKeyId"]
            == signing_key_target
        )


def test_operations_table_names_are_explicit_parameters(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    assert parameters["FargateStateTableName"] == {
        "Type": "String",
        "Default": "axonllm-state",
        "AllowedPattern": "^[A-Za-z0-9_.-]{3,214}$",
        "Description": "Physical state table name used by AxonLLMStack",
    }
    assert parameters["AgentCoreStateTableName"] == {
        "Type": "String",
        "Default": "axonllm-agentcore-state",
        "AllowedPattern": "^[A-Za-z0-9_.-]{3,214}$",
        "Description": (
            "Physical state table name used by AxonLLMAgentCoreStack"
        ),
    }


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
    assert "repo:AxonLLM/axonllm:" not in json.dumps(roles)
    assert len(roles) == 5
    assert {
        role["Properties"]["RoleName"] for role in roles
    } == {
        "AxonLLMOperationsAudit",
        "AxonLLMOperationsRecovery",
        "AxonLLMReleasePublisher",
        "AxonLLMReleaseSigner",
        "AxonLLMReleaseVerifier",
    }
    for role in roles:
        expected_duration = (
            7200
            if role["Properties"]["RoleName"]
            == "AxonLLMOperationsRecovery"
            else 3600
        )
        assert role["Properties"]["MaxSessionDuration"] == expected_duration
        statements = role["Properties"][
            "AssumeRolePolicyDocument"
        ]["Statement"]
        assert len(statements) == 1
        statement = statements[0]
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        conditions = statement["Condition"]
        assert conditions["StringEquals"][
            "token.actions.githubusercontent.com:aud"
        ] == "sts.amazonaws.com"
        if role["Properties"]["RoleName"] == "AxonLLMReleaseSigner":
            assert "token.actions.githubusercontent.com:sub" not in (
                conditions["StringEquals"]
            )
            assert conditions["StringLike"][
                "token.actions.githubusercontent.com:sub"
            ] == _SIGNING_SUBJECT
            continue
        expected_subject = (
            _RELEASE_SUBJECT
            if role["Properties"]["RoleName"]
            == "AxonLLMReleasePublisher"
            else _PRODUCTION_SUBJECT
        )
        assert conditions["StringEquals"][
            "token.actions.githubusercontent.com:sub"
        ] == expected_subject


def test_signer_publisher_and_verifier_permissions_are_separate(
    synthesized_template,
):
    policies = _resources(synthesized_template, "AWS::IAM::Policy")
    assert len(policies) == 5
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
    signer = next(
        statements
        for role, statements in by_role.items()
        if "ReleaseSignerRole" in role
    )
    publisher_actions = {
        action for statement in publisher for action in _actions(statement)
    }
    verifier_actions = {
        action for statement in verifier for action in _actions(statement)
    }
    signer_actions = {
        action for statement in signer for action in _actions(statement)
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
    assert "kms:Verify" in publisher_actions
    assert "kms:Sign" not in publisher_actions
    assert "kms:Verify" in verifier_actions
    assert "kms:Sign" not in verifier_actions
    assert signer_actions == {"kms:Sign", "kms:Verify"}

    signing_key_logical_id = next(
        logical_id
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource["Type"] == "AWS::KMS::Key"
        and resource["Properties"]["Description"]
        == "AxonLLM private release evidence signing"
    )
    signer_kms_statements = [
        statement
        for statement in signer
        if any(action.startswith("kms:") for action in _actions(statement))
    ]
    assert len(signer_kms_statements) == 1
    assert _actions(signer_kms_statements[0]) == {
        "kms:Sign",
        "kms:Verify",
    }
    assert signer_kms_statements[0]["Resource"] == {
        "Fn::GetAtt": [signing_key_logical_id, "Arn"],
    }
    assert "Condition" not in signer_kms_statements[0]

    account_key_resource = {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":kms:us-east-1:",
                {"Ref": "AWS::AccountId"},
                ":key/*",
            ],
        ]
    }
    alias_condition = {
        "ForAnyValue:StringLike": {
            "kms:ResourceAliases": [
                "alias/axonllm/release-signing-v*",
            ]
        }
    }
    for statements in (publisher, verifier):
        kms_statements = [
            statement
            for statement in statements
            if any(
                action.startswith("kms:")
                for action in _actions(statement)
            )
        ]
        assert len(kms_statements) == 1
        assert _actions(kms_statements[0]) == {"kms:Verify"}
        assert kms_statements[0]["Resource"] == account_key_resource
        assert kms_statements[0]["Condition"] == alias_condition


def test_operations_roles_separate_metadata_audit_from_recovery(
    synthesized_template,
):
    policies = _resources(synthesized_template, "AWS::IAM::Policy")
    by_role: dict[str, list[dict]] = {}
    for policy in policies:
        role = policy["Properties"]["Roles"][0]["Ref"]
        by_role[role] = policy["Properties"]["PolicyDocument"]["Statement"]

    audit = next(
        statements
        for role, statements in by_role.items()
        if "OperationsAuditRole" in role
    )
    recovery = next(
        statements
        for role, statements in by_role.items()
        if "OperationsRecoveryRole" in role
    )
    audit_actions = {
        action for statement in audit for action in _actions(statement)
    }
    recovery_actions = {
        action for statement in recovery for action in _actions(statement)
    }

    assert {
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "kms:DescribeKey",
        "kms:GetKeyRotationStatus",
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds",
    } <= audit_actions
    assert not any(
        action.startswith("dynamodb:")
        and action
        not in {
            "dynamodb:DescribeContinuousBackups",
            "dynamodb:DescribeTable",
        }
        for action in audit_actions
    )
    assert "kms:Decrypt" not in audit_actions
    assert "secretsmanager:GetSecretValue" not in audit_actions
    audit_key_statement = next(
        statement
        for statement in audit
        if statement.get("Sid") == "InspectDataKeyRotation"
    )
    assert audit_key_statement["Condition"][
        "ForAnyValue:StringEquals"
    ]["kms:ResourceAliases"] == [
        "alias/axonllm/data",
        "alias/axonllm/agentcore-data",
    ]
    audit_resources = [
        resource
        for statement in audit
        for resource in (
            statement["Resource"]
            if isinstance(statement["Resource"], list)
            else [statement["Resource"]]
        )
    ]
    audit_resource_literals = [
        _literal_parts(resource) for resource in audit_resources
    ]
    assert any(
        resource.endswith(":secret:AxonLLMStack-*")
        for resource in audit_resource_literals
    )
    assert any(
        resource.endswith(":backup-vault:axon-state-*")
        for resource in audit_resource_literals
    )
    assert not any(
        ":secret/" in resource or ":backup-vault/" in resource
        for resource in audit_resource_literals
    )
    assert "dynamodb:RestoreTableToPointInTime" in recovery_actions
    assert "dynamodb:DeleteTable" in recovery_actions
    assert "kms:Decrypt" in recovery_actions
    assert "kms:CreateGrant" in recovery_actions
    assert not any(
        action.startswith("secretsmanager:")
        for action in recovery_actions
    )
    restored_table_statement = next(
        statement
        for statement in recovery
        if statement.get("Sid") == "ValidateAndRemoveRestoredState"
    )
    assert _actions(restored_table_statement) == {
        "dynamodb:DeleteTable",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:RestoreTableToPointInTime",
        "dynamodb:Scan",
        "dynamodb:UpdateContinuousBackups",
        "dynamodb:UpdateTable",
        "dynamodb:UpdateTimeToLive",
    }

    recovery_resources = [
        resource
        for statement in recovery
        for resource in (
            [statement["Resource"]]
            if not isinstance(statement["Resource"], list)
            else statement["Resource"]
        )
    ]
    assert "*" not in recovery_resources
    restored_resources = restored_table_statement["Resource"]
    assert len(restored_resources) == 2
    restored_table_parameters = set()
    for resource in restored_resources:
        parts = resource["Fn::Join"][1]
        assert parts[-1] == "-restore-validation-*"
        restored_table_parameters.add(parts[-2]["Ref"])
    assert restored_table_parameters == {
        "FargateStateTableName",
        "AgentCoreStateTableName",
    }

    key_statements = [
        statement
        for statement in recovery
        if any(action.startswith("kms:") for action in _actions(statement))
    ]
    assert key_statements
    for statement in key_statements:
        assert _literal_parts(statement["Resource"]).endswith(":key/*")
        assert statement["Condition"]["ForAnyValue:StringEquals"][
            "kms:ResourceAliases"
        ] == [
            "alias/axonllm/data",
            "alias/axonllm/agentcore-data",
        ]
        assert statement["Condition"]["StringEquals"][
            "kms:CallerAccount"
        ] == {"Ref": "AWS::AccountId"}
        assert statement["Condition"]["StringEquals"][
            "kms:ViaService"
        ] == {
            "Fn::Join": [
                "",
                [
                    "dynamodb.us-east-1.",
                    {"Ref": "AWS::URLSuffix"},
                ],
            ]
        }


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
        "OperationsAuditRoleArn",
        "OperationsRecoveryRoleArn",
        "ReleasePublisherRoleArn",
        "ReleaseRegistryKeyArn",
        "ReleaseSigningKeyArn",
        "ReleaseSignerRoleArn",
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
