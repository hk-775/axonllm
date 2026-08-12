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
_REHEARSAL_EVIDENCE_PREFIX = "agentcore-production/rehearsal"
_QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX = "agentcore-production/qualification-teardown"
_TRANSITION_EVIDENCE_PREFIX = "agentcore-production/transitions"
_EXTERNAL_OIDC_EVIDENCE_PREFIX = "agentcore-external-oidc"
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
_LAUNCH_ACTIONS = {
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
}


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


def _resource_entries(
    template: dict,
    resource_type: str,
) -> list[tuple[str, dict]]:
    return [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
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
        return separator.join(part for part in parts if isinstance(part, str))
    return ""


def _github_subject(suffix: str) -> dict:
    return {
        "Fn::Join": [
            "",
            [
                {"Ref": "GitHubOidcSubjectPrefix"},
                suffix,
            ],
        ]
    }


def _role_logical_id(template: dict, role_name: str) -> str:
    return next(
        logical_id
        for logical_id, role in _resource_entries(
            template,
            "AWS::IAM::Role",
        )
        if role["Properties"]["RoleName"] == role_name
    )


def _role(template: dict, role_name: str) -> dict:
    return template["Resources"][_role_logical_id(template, role_name)]


def _role_statements(template: dict, role_name: str) -> list[dict]:
    role_logical_id = _role_logical_id(template, role_name)
    policies = [
        policy
        for resource_type in (
            "AWS::IAM::Policy",
            "AWS::IAM::ManagedPolicy",
        )
        for policy in _resources(template, resource_type)
        if {"Ref": role_logical_id} in policy["Properties"].get("Roles", [])
    ]
    assert policies
    return [statement for policy in policies for statement in policy["Properties"]["PolicyDocument"]["Statement"]]


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
    return json.loads((out_dir / "AxonLLMReleaseFoundationStack.template.json").read_text(encoding="utf-8"))


def test_release_repositories_are_retained_immutable_and_kms_encrypted(
    synthesized_template,
):
    repositories = _resources(
        synthesized_template,
        "AWS::ECR::Repository",
    )
    assert len(repositories) == 2
    assert {repository["Properties"]["RepositoryName"] for repository in repositories} == {
        "axonllm/fargate",
        "axonllm/agentcore",
    }

    for repository in repositories:
        properties = repository["Properties"]
        assert properties["ImageTagMutability"] == "IMMUTABLE"
        assert properties["ImageScanningConfiguration"] == {"ScanOnPush": True}
        assert properties["EncryptionConfiguration"]["EncryptionType"] == "KMS"
        assert "KmsKey" in properties["EncryptionConfiguration"]
        assert repository["DeletionPolicy"] == "Retain"
        assert repository["UpdateReplacePolicy"] == "Retain"


def test_release_keys_are_retained_and_purpose_scoped(synthesized_template):
    keys = _resources(synthesized_template, "AWS::KMS::Key")
    assert len(keys) == 7
    key_entries = {
        resource["Properties"]["Description"]: (
            logical_id,
            resource,
        )
        for logical_id, resource in _resource_entries(
            synthesized_template,
            "AWS::KMS::Key",
        )
    }
    assert set(key_entries) == {
        "AxonLLM private release registry encryption",
        "AxonLLM private release evidence signing",
        "Encrypts AxonLLM AgentCore launch coordinator state",
        "Encrypts immutable AxonLLM deployment evidence",
        "Signs AgentCore launch prerequisite and qualification evidence",
        "Signs AgentCore production transition intent and deployment evidence",
        "Signs terminal records produced by the production transition watchdog",
    }
    for description in (
        "AxonLLM private release registry encryption",
        "Encrypts AxonLLM AgentCore launch coordinator state",
        "Encrypts immutable AxonLLM deployment evidence",
    ):
        properties = key_entries[description][1]["Properties"]
        assert properties["EnableKeyRotation"] is True
        assert "KeySpec" not in properties

    signing_descriptions = (
        "AxonLLM private release evidence signing",
        "Signs AgentCore launch prerequisite and qualification evidence",
        "Signs AgentCore production transition intent and deployment evidence",
        "Signs terminal records produced by the production transition watchdog",
    )
    for description in signing_descriptions:
        properties = key_entries[description][1]["Properties"]
        assert properties["KeySpec"] == "ECC_NIST_P256"
        assert properties["KeyUsage"] == "SIGN_VERIFY"
        assert "EnableKeyRotation" not in properties
    for key in keys:
        assert key["DeletionPolicy"] == "Retain"
        assert key["UpdateReplacePolicy"] == "Retain"

    aliases = _resources(synthesized_template, "AWS::KMS::Alias")
    aliases_by_name = {alias["Properties"]["AliasName"]: alias for alias in aliases}
    assert set(aliases_by_name) == {
        "alias/axonllm/agentcore-launch-prerequisite-signing",
        "alias/axonllm/agentcore-launch-coordinator",
        "alias/axonllm/agentcore-production-transition-signing",
        "alias/axonllm/agentcore-production-transition-terminal-signing",
        "alias/axonllm/deployment-evidence",
        "alias/axonllm/release-ecr",
        "alias/axonllm/release-signing",
        "alias/axonllm/release-signing-v1",
    }
    release_signing_key_id = key_entries["AxonLLM private release evidence signing"][0]
    signing_key_target = {
        "Fn::GetAtt": [release_signing_key_id, "Arn"],
    }
    for alias_name in (
        "alias/axonllm/release-signing",
        "alias/axonllm/release-signing-v1",
    ):
        assert aliases_by_name[alias_name]["Properties"]["TargetKeyId"] == signing_key_target

    alias_targets = {
        "alias/axonllm/agentcore-launch-prerequisite-signing": (
            "Signs AgentCore launch prerequisite and qualification evidence"
        ),
        "alias/axonllm/agentcore-production-transition-signing": (
            "Signs AgentCore production transition intent and deployment evidence"
        ),
        "alias/axonllm/agentcore-production-transition-terminal-signing": (
            "Signs terminal records produced by the production transition watchdog"
        ),
    }
    for alias_name, description in alias_targets.items():
        assert aliases_by_name[alias_name]["Properties"]["TargetKeyId"] == {
            "Fn::GetAtt": [key_entries[description][0], "Arn"]
        }

    signer_statements = _role_statements(
        synthesized_template,
        "AxonLLMReleaseSigner",
    )
    assert len(signer_statements) == 1
    assert _actions(signer_statements[0]) == {
        "kms:Sign",
        "kms:Verify",
    }
    assert signer_statements[0]["Resource"] == signing_key_target


def test_deployment_evidence_store_is_immutable_encrypted_and_retained(
    synthesized_template,
):
    bucket = _resources(synthesized_template, "AWS::S3::Bucket")[0]
    properties = bucket["Properties"]
    assert properties["BucketName"] == {
        "Fn::Join": [
            "",
            [
                "axonllm-deployment-evidence-",
                {"Ref": "AWS::AccountId"},
                "-us-east-1",
            ],
        ]
    }
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["ObjectLockEnabled"] is True
    assert properties["ObjectLockConfiguration"] == {
        "ObjectLockEnabled": "Enabled",
        "Rule": {
            "DefaultRetention": {
                "Days": 2555,
                "Mode": "COMPLIANCE",
            }
        },
    }
    encryption = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert encryption == [
        {
            "BucketKeyEnabled": True,
            "ServerSideEncryptionByDefault": {
                "KMSMasterKeyID": {
                    "Fn::GetAtt": [
                        next(
                            logical_id
                            for logical_id, resource in synthesized_template["Resources"].items()
                            if resource["Type"] == "AWS::KMS::Key"
                            and resource["Properties"]["Description"]
                            == "Encrypts immutable AxonLLM deployment evidence"
                        ),
                        "Arn",
                    ]
                },
                "SSEAlgorithm": "aws:kms",
            },
        }
    ]
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"

    bucket_policy = _resources(
        synthesized_template,
        "AWS::S3::BucketPolicy",
    )[0]
    deny = next(
        statement
        for statement in bucket_policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Sid") == "DenyEvidenceDeletion"
    )
    assert _actions(deny) == {
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
    }
    assert deny["Effect"] == "Deny"
    assert deny["Principal"] == {"AWS": "*"}


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
        "Description": ("Physical state table name used by AxonLLMAgentCoreStack"),
    }


def test_github_oidc_trust_is_exact_and_retained(synthesized_template):
    subject_parameter = synthesized_template["Parameters"]["GitHubOidcSubjectPrefix"]
    assert subject_parameter["Default"] == _SUBJECT_PREFIX
    assert subject_parameter["AllowedPattern"] == (
        r"^repo:[A-Za-z0-9_.-]+@[0-9]+/"
        r"[A-Za-z0-9_.-]+@[0-9]+$"
    )
    providers = _resources(
        synthesized_template,
        "AWS::IAM::OIDCProvider",
    )
    assert len(providers) == 1
    provider = providers[0]
    assert provider["Properties"]["Url"] == ("https://token.actions.githubusercontent.com")
    assert provider["Properties"]["ClientIdList"] == ["sts.amazonaws.com"]
    assert provider["DeletionPolicy"] == "Retain"
    assert provider["UpdateReplacePolicy"] == "Retain"

    roles = _resources(synthesized_template, "AWS::IAM::Role")
    assert "repo:AxonLLM/axonllm:" not in json.dumps(roles)
    assert len(roles) == 17
    roles_by_name = {role["Properties"]["RoleName"] for role in roles}
    assert roles_by_name == {
        "AxonLLMLaunchActionWorkerRole",
        "AxonLLMLaunchCleanupWorkerRole",
        "AxonLLMLaunchCoordinatorExecutionRole",
        "AxonLLMLaunchCoordinatorSchedulerRole",
        "AxonLLMLaunchGatesRole",
        "AxonLLMAgentCoreDeployRole",
        "AxonLLMAgentCoreQualificationRole",
        "AxonLLMAgentCoreRehearsalEvidenceRole",
        "AxonLLMAgentCoreTransitionWatchdogRole",
        "AxonLLMExternalOidcCertificationRole",
        "AxonLLMOperationsAudit",
        "AxonLLMOperationsRecovery",
        "AxonLLMProductionTransitionMutationBrokerRole",
        "AxonLLMQualificationMutationBrokerRole",
        "AxonLLMReleasePublisher",
        "AxonLLMReleaseSigner",
        "AxonLLMReleaseVerifier",
    }
    github_roles = [
        role
        for role in roles
        if role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Action"] == "sts:AssumeRoleWithWebIdentity"
    ]
    assert len(github_roles) == 11
    expected_durations = {
        "AxonLLMAgentCoreDeployRole": 10800,
        "AxonLLMAgentCoreQualificationRole": 10800,
        "AxonLLMAgentCoreRehearsalEvidenceRole": 7200,
        "AxonLLMAgentCoreTransitionWatchdogRole": 3600,
        "AxonLLMExternalOidcCertificationRole": 7200,
        "AxonLLMLaunchGatesRole": 10800,
        "AxonLLMOperationsAudit": 3600,
        "AxonLLMOperationsRecovery": 7200,
        "AxonLLMReleasePublisher": 3600,
        "AxonLLMReleaseSigner": 3600,
        "AxonLLMReleaseVerifier": 3600,
    }
    expected_subject_suffixes = {
        "AxonLLMAgentCoreDeployRole": (":environment:agentcore-production-deploy"),
        "AxonLLMAgentCoreQualificationRole": (":environment:agentcore-qualification"),
        "AxonLLMAgentCoreRehearsalEvidenceRole": (":environment:agentcore-production-evidence"),
        "AxonLLMAgentCoreTransitionWatchdogRole": (":environment:agentcore-production-watchdog"),
        "AxonLLMExternalOidcCertificationRole": (":environment:agentcore-external-oidc-production-like"),
        "AxonLLMLaunchGatesRole": (":environment:agentcore-production-launch-gates"),
        "AxonLLMOperationsAudit": ":environment:production",
        "AxonLLMOperationsRecovery": ":environment:production",
        "AxonLLMReleasePublisher": ":environment:release",
        "AxonLLMReleaseVerifier": ":environment:production",
    }
    for role in github_roles:
        role_name = role["Properties"]["RoleName"]
        assert role["Properties"]["MaxSessionDuration"] == expected_durations[role_name]
        statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert len(statements) == 1
        statement = statements[0]
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        conditions = statement["Condition"]
        assert conditions["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
        if role_name == "AxonLLMReleaseSigner":
            assert "token.actions.githubusercontent.com:sub" not in (conditions["StringEquals"])
            assert conditions["StringLike"]["token.actions.githubusercontent.com:sub"] == _github_subject(
                ":ref:refs/tags/v*"
            )
            continue
        assert conditions["StringEquals"]["token.actions.githubusercontent.com:sub"] == _github_subject(
            expected_subject_suffixes[role_name]
        )

    service_principals = {
        role["Properties"]["RoleName"]: role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"][
            "Service"
        ]
        for role in roles
        if role not in github_roles
    }
    assert service_principals == {
        "AxonLLMLaunchActionWorkerRole": "ecs-tasks.amazonaws.com",
        "AxonLLMLaunchCleanupWorkerRole": "ecs-tasks.amazonaws.com",
        "AxonLLMLaunchCoordinatorExecutionRole": "states.amazonaws.com",
        "AxonLLMLaunchCoordinatorSchedulerRole": ("scheduler.amazonaws.com"),
        "AxonLLMProductionTransitionMutationBrokerRole": "lambda.amazonaws.com",
        "AxonLLMQualificationMutationBrokerRole": "lambda.amazonaws.com",
    }
    for role_name in {
        "AxonLLMLaunchActionWorkerRole",
        "AxonLLMLaunchCleanupWorkerRole",
        "AxonLLMLaunchCoordinatorExecutionRole",
        "AxonLLMLaunchCoordinatorSchedulerRole",
    }:
        statement = _role(synthesized_template, role_name)["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert statement["Action"] == "sts:AssumeRole"
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
        assert "aws:SourceArn" in statement["Condition"]["ArnLike"]
    for role_name in {
        "AxonLLMProductionTransitionMutationBrokerRole",
        "AxonLLMQualificationMutationBrokerRole",
    }:
        statement = _role(synthesized_template, role_name)["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert statement == {
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
        }


def test_signer_publisher_and_verifier_permissions_are_separate(
    synthesized_template,
):
    policies = _resources(synthesized_template, "AWS::IAM::Policy")
    assert len(policies) == 17
    by_role: dict[str, list[dict]] = {}
    for policy in policies:
        role = policy["Properties"]["Roles"][0]["Ref"]
        by_role[role] = policy["Properties"]["PolicyDocument"]["Statement"]

    publisher = next(statements for role, statements in by_role.items() if "ReleasePublisherRole" in role)
    verifier = next(statements for role, statements in by_role.items() if "ReleaseVerifierRole" in role)
    signer = next(statements for role, statements in by_role.items() if "ReleaseSignerRole" in role)
    publisher_actions = {action for statement in publisher for action in _actions(statement)}
    verifier_actions = {action for statement in verifier for action in _actions(statement)}
    signer_actions = {action for statement in signer for action in _actions(statement)}

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
        and resource["Properties"]["Description"] == "AxonLLM private release evidence signing"
    )
    signer_kms_statements = [
        statement for statement in signer if any(action.startswith("kms:") for action in _actions(statement))
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
            statement for statement in statements if any(action.startswith("kms:") for action in _actions(statement))
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

    audit = next(statements for role, statements in by_role.items() if "OperationsAuditRole" in role)
    recovery = next(statements for role, statements in by_role.items() if "OperationsRecoveryRole" in role)
    audit_actions = {action for statement in audit for action in _actions(statement)}
    recovery_actions = {action for statement in recovery for action in _actions(statement)}

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
    audit_key_statement = next(statement for statement in audit if statement.get("Sid") == "InspectDataKeyRotation")
    assert audit_key_statement["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"] == [
        "alias/axonllm/data",
        "alias/axonllm/agentcore-data",
    ]
    audit_resources = [
        resource
        for statement in audit
        for resource in (statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]])
    ]
    audit_resource_literals = [_literal_parts(resource) for resource in audit_resources]
    assert any(resource.endswith(":secret:AxonLLMStack-*") for resource in audit_resource_literals)
    assert any(resource.endswith(":backup-vault:axon-state-*") for resource in audit_resource_literals)
    assert not any(":secret/" in resource or ":backup-vault/" in resource for resource in audit_resource_literals)
    assert "dynamodb:RestoreTableToPointInTime" in recovery_actions
    assert "dynamodb:DeleteTable" in recovery_actions
    assert "kms:Decrypt" in recovery_actions
    assert "kms:CreateGrant" in recovery_actions
    assert not any(action.startswith("secretsmanager:") for action in recovery_actions)
    restored_table_statement = next(
        statement for statement in recovery if statement.get("Sid") == "ValidateAndRemoveRestoredState"
    )
    assert _actions(restored_table_statement) == {
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
    }

    recovery_resources = [
        resource
        for statement in recovery
        for resource in (
            [statement["Resource"]] if not isinstance(statement["Resource"], list) else statement["Resource"]
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
        statement for statement in recovery if any(action.startswith("kms:") for action in _actions(statement))
    ]
    assert key_statements
    for statement in key_statements:
        assert _literal_parts(statement["Resource"]).endswith(":key/*")
        assert statement["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"] == [
            "alias/axonllm/data",
            "alias/axonllm/agentcore-data",
        ]
        assert statement["Condition"]["StringEquals"]["kms:CallerAccount"] == {"Ref": "AWS::AccountId"}
        assert statement["Condition"]["StringEquals"]["kms:ViaService"] == {
            "Fn::Join": [
                "",
                [
                    "dynamodb.us-east-1.",
                    {"Ref": "AWS::URLSuffix"},
                ],
            ]
        }


def test_launch_authorities_have_prefix_scoped_separate_capabilities(
    synthesized_template,
):
    expectations = {
        "AxonLLMAgentCoreDeployRole": {
            "read": {
                f"{_REHEARSAL_EVIDENCE_PREFIX}/*",
                f"{_QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX}/*",
                f"{_TRANSITION_EVIDENCE_PREFIX}/*",
                f"{_EXTERNAL_OIDC_EVIDENCE_PREFIX}/*",
            },
            "write": {f"{_TRANSITION_EVIDENCE_PREFIX}/*"},
            "signing_key": (
                "Signs AgentCore production transition intent and deployment evidence"
            ),
            "required": {
                "cloudformation:CreateChangeSet",
                "kms:Sign",
                "sts:AssumeRole",
            },
            "forbidden": set(),
        },
        "AxonLLMAgentCoreRehearsalEvidenceRole": {
            "read": {
                f"{_REHEARSAL_EVIDENCE_PREFIX}/*",
                f"{_QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX}/*",
            },
            "write": {
                f"{_REHEARSAL_EVIDENCE_PREFIX}/*",
                f"{_QUALIFICATION_TEARDOWN_EVIDENCE_PREFIX}/*",
            },
            "signing_key": ("Signs AgentCore launch prerequisite and qualification evidence"),
            "required": {"kms:Sign", "s3:PutObject"},
            "forbidden": {
                "bedrock-agentcore:InvokeAgentRuntime",
                "cloudformation:CreateChangeSet",
                "dynamodb:PutItem",
                "iam:PassRole",
            },
        },
        "AxonLLMExternalOidcCertificationRole": {
            "read": {f"{_EXTERNAL_OIDC_EVIDENCE_PREFIX}/*"},
            "write": {f"{_EXTERNAL_OIDC_EVIDENCE_PREFIX}/*"},
            "signing_key": ("Signs AgentCore launch prerequisite and qualification evidence"),
            "required": {
                "bedrock-agentcore:InvokeAgentRuntime",
                "cloudformation:CreateChangeSet",
                "kms:Sign",
                "s3:PutObject",
                "sts:AssumeRole",
            },
            "forbidden": {
                "cognito-idp:AdminCreateUser",
            },
        },
        "AxonLLMAgentCoreQualificationRole": {
            "read": {
                f"{_REHEARSAL_EVIDENCE_PREFIX}/*",
                f"{_EXTERNAL_OIDC_EVIDENCE_PREFIX}/*",
            },
            "write": set(),
            "signing_key": None,
            "required": {
                "bedrock-agentcore:InvokeAgentRuntime",
                "cloudformation:CreateChangeSet",
                "cognito-idp:AdminCreateUser",
                "sts:AssumeRole",
            },
            "forbidden": {
                "kms:Sign",
                "s3:PutObject",
                "s3:PutObjectRetention",
            },
        },
    }
    for role_name, expected in expectations.items():
        statements = _role_statements(synthesized_template, role_name)
        actions = {action for statement in statements for action in _actions(statement)}
        assert expected["required"] <= actions
        assert expected["forbidden"].isdisjoint(actions)
        assert "s3:GetEncryptionConfiguration" in actions
        assert "s3:GetBucketEncryption" not in actions

        listing = next(statement for statement in statements if statement.get("Sid") == "ListBoundEvidenceVersions")
        assert set(listing["Condition"]["StringLike"]["s3:prefix"]) == expected["read"]
        read = next(statement for statement in statements if statement.get("Sid") == "ReadBoundImmutableEvidence")
        read_resources = read["Resource"] if isinstance(read["Resource"], list) else [read["Resource"]]
        assert {_literal_parts(resource).removeprefix("/") for resource in read_resources} == expected["read"]
        writes = [statement for statement in statements if statement.get("Sid") == "AppendBoundImmutableEvidence"]
        if not expected["write"]:
            assert writes == []
        else:
            assert len(writes) == 1
            write_resources = (
                writes[0]["Resource"] if isinstance(writes[0]["Resource"], list) else [writes[0]["Resource"]]
            )
            assert {_literal_parts(resource).removeprefix("/") for resource in write_resources} == expected["write"]

        signing = [statement for statement in statements if statement.get("Sid") == "SignOwnedLaunchEvidence"]
        if expected["signing_key"] is None:
            assert signing == []
        else:
            assert len(signing) == 1
            signing_key_id = next(
                logical_id
                for logical_id, key in _resource_entries(
                    synthesized_template,
                    "AWS::KMS::Key",
                )
                if key["Properties"]["Description"] == expected["signing_key"]
            )
            assert signing[0]["Resource"] == {"Fn::GetAtt": [signing_key_id, "Arn"]}

    watchdog = _role_statements(
        synthesized_template,
        "AxonLLMAgentCoreTransitionWatchdogRole",
    )
    watchdog_actions = {
        action for statement in watchdog for action in _actions(statement)
    }
    assert {
        "kms:Sign",
        "kms:Verify",
        "lambda:InvokeFunction",
        "s3:GetObjectVersion",
        "s3:PutObject",
    } <= watchdog_actions
    assert {
        "cloudformation:DeleteStack",
        "cloudformation:UpdateStack",
        "elasticloadbalancing:ModifyLoadBalancerAttributes",
        "iam:PassRole",
    }.isdisjoint(watchdog_actions)
    writes = next(
        statement
        for statement in watchdog
        if statement.get("Sid") == "AppendTerminalTransitionEvidence"
    )
    write_resources = (
        writes["Resource"]
        if isinstance(writes["Resource"], list)
        else [writes["Resource"]]
    )
    assert {
        _literal_parts(resource).removeprefix("/")
        for resource in write_resources
    } == {
        f"{_TRANSITION_EVIDENCE_PREFIX}/*/transition-terminal.json",
        (
            f"{_TRANSITION_EVIDENCE_PREFIX}/*/"
            "transition-terminal-kms-signature.json"
        ),
    }
    terminal_key_id = next(
        logical_id
        for logical_id, key in _resource_entries(
            synthesized_template,
            "AWS::KMS::Key",
        )
        if key["Properties"]["Description"]
        == "Signs terminal records produced by the production transition watchdog"
    )
    terminal_signing = next(
        statement
        for statement in watchdog
        if statement.get("Sid") == "SignTerminalTransitionEvidence"
    )
    assert terminal_signing["Resource"] == {
        "Fn::GetAtt": [terminal_key_id, "Arn"]
    }

    for role_name in (
        "AxonLLMAgentCoreDeployRole",
        "AxonLLMAgentCoreQualificationRole",
    ):
        statements = _role_statements(synthesized_template, role_name)
        caller = next(statement for statement in statements if statement.get("Sid") == "ConfirmDeploymentAccount")
        assert _actions(caller) == {"sts:GetCallerIdentity"}
        assert caller["Resource"] == "*"
        for sid in (
            "InspectCdkBootstrapPolicies",
            "InspectCdkBootstrapExecutionRole",
            "ReadCdkBootstrapVersion",
        ):
            statement = next(candidate for candidate in statements if candidate.get("Sid") == sid)
            assert statement["Resource"] != "*"


def test_launch_authorities_cannot_cross_production_and_qualification_namespaces(
    synthesized_template,
):
    role_scopes = {
        "AxonLLMAgentCoreDeployRole": {
            "runtime": ":runtime/axonllm-*",
            "stack": "AxonLLMAgentCoreStack",
            "state": "axonllm-agentcore-state",
            "identity": "AxonLLMIdentityStack",
        },
        "AxonLLMExternalOidcCertificationRole": {
            "runtime": ":runtime/axonllm_external-*",
            "stack": "AxonLLMAgentCoreStack-external",
            "state": "axonllm-agentcore-state-external",
            "identity": None,
        },
        "AxonLLMAgentCoreQualificationRole": {
            "runtime": ":runtime/axonllm_managed-*",
            "stack": "AxonLLMAgentCoreStack-managed",
            "state": "axonllm-agentcore-state-managed",
            "identity": "AxonLLMIdentityStack-managed",
        },
    }
    for role_name, expected in role_scopes.items():
        statements = _role_statements(
            synthesized_template,
            role_name,
        )
        runtime = next(statement for statement in statements if statement.get("Sid") == "InspectCertificationRuntime")
        runtime_resources = runtime["Resource"] if isinstance(runtime["Resource"], list) else [runtime["Resource"]]
        assert all(expected["runtime"] in _literal_parts(resource) for resource in runtime_resources)

        stack = next(statement for statement in statements if statement.get("Sid") == "InspectCertificationStack")
        assert _literal_parts(stack["Resource"]).endswith(f":stack/{expected['stack']}/*")

        fixtures = next(
            statement for statement in statements if statement.get("Sid") == "ManageOwnedCertificationFixtures"
        )
        fixture_resources = fixtures["Resource"] if isinstance(fixtures["Resource"], list) else [fixtures["Resource"]]
        fixture_literals = {_literal_parts(resource) for resource in fixture_resources}
        assert any(resource.endswith(f":table/{expected['state']}") for resource in fixture_literals)
        assert any(resource.endswith(f":table/{expected['state']}/index/*") for resource in fixture_literals)
        assert all(f":table/{expected['state']}" in resource for resource in fixture_literals)

        identity = [statement for statement in statements if statement.get("Sid") == "ManageOwnedCertificationUsers"]
        if expected["identity"] is None:
            assert identity == []
        else:
            assert len(identity) == 1
            assert _actions(identity[0]) == {
                "cognito-idp:AdminCreateUser",
                "cognito-idp:AdminDeleteUser",
                "cognito-idp:AdminGetUser",
                "cognito-idp:AdminInitiateAuth",
                "cognito-idp:AdminRespondToAuthChallenge",
                "cognito-idp:AdminUserGlobalSignOut",
                "cognito-idp:AssociateSoftwareToken",
                "cognito-idp:DescribeUserPool",
                "cognito-idp:DescribeUserPoolClient",
                "cognito-idp:VerifySoftwareToken",
            }
            assert identity[0]["Condition"] == {
                "StringEquals": {"aws:ResourceTag/aws:cloudformation:stack-name": (expected["identity"])}
            }
            target_health = next(
                statement for statement in statements if statement.get("Sid") == "InspectCertificationTargetHealth"
            )
            assert _actions(target_health) == {"elasticloadbalancing:DescribeTargetHealth"}
            assert _literal_parts(target_health["Resource"]).endswith(":targetgroup/*/*")
            assert target_health["Condition"] == {
                "StringEquals": {
                    "aws:ResourceTag/aws:cloudformation:stack-name": (
                        expected["identity"].replace(
                            "AxonLLMIdentityStack",
                            "AxonLLMControlPlaneStack",
                        )
                    )
                }
            }

    production_deploy = next(
        statement
        for statement in _role_statements(
            synthesized_template,
            "AxonLLMAgentCoreDeployRole",
        )
        if statement.get("Sid") == "DeployReviewedAxonStacks"
    )
    qualification_deploy = next(
        statement
        for statement in _role_statements(
            synthesized_template,
            "AxonLLMAgentCoreQualificationRole",
        )
        if statement.get("Sid") == "DeployReviewedAxonStacks"
    )
    production_resources = {_literal_parts(resource) for resource in production_deploy["Resource"]}
    qualification_resources = {_literal_parts(resource) for resource in qualification_deploy["Resource"]}
    assert all("-managed/" not in resource for resource in production_resources)
    assert not any("AxonLLMLaunchWorkersStack" in resource for resource in production_resources)
    assert len(qualification_resources) == 4
    assert all("-managed/" in resource for resource in qualification_resources)
    assert all("-*/" not in resource for resource in qualification_resources)

    external_deploy = next(
        statement
        for statement in _role_statements(
            synthesized_template,
            "AxonLLMExternalOidcCertificationRole",
        )
        if statement.get("Sid") == "DeployReviewedAxonStacks"
    )
    external_resources = {
        _literal_parts(resource)
        for resource in (
            external_deploy["Resource"]
            if isinstance(external_deploy["Resource"], list)
            else [external_deploy["Resource"]]
        )
    }
    assert len(external_resources) == 1
    assert next(iter(external_resources)).endswith(":stack/AxonLLMAgentCoreStack-external/*")

    qualification_statements = _role_statements(
        synthesized_template,
        "AxonLLMAgentCoreQualificationRole",
    )
    foundation = next(
        statement for statement in qualification_statements if statement.get("Sid") == "InspectReleaseFoundation"
    )
    assert _actions(foundation) == {"cloudformation:DescribeStacks"}
    assert _literal_parts(foundation["Resource"]).endswith(":stack/AxonLLMReleaseFoundationStack/*")
    expected_services = {
        "*/axonllm-launch-action-worker-managed",
        "*/axonllm-launch-cleanup-worker-managed",
    }
    for sid, action in (
        ("InspectQualificationLaunchWorkers", "ecs:DescribeServices"),
        ("ResizeQualificationLaunchWorkers", "ecs:UpdateService"),
    ):
        statement = next(candidate for candidate in qualification_statements if candidate.get("Sid") == sid)
        assert _actions(statement) == {action}
        resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
        assert {_literal_parts(resource).rsplit(":service/", 1)[1] for resource in resources} == expected_services

    production_sids = {
        statement.get("Sid")
        for statement in _role_statements(
            synthesized_template,
            "AxonLLMAgentCoreDeployRole",
        )
    }
    assert "InspectReleaseFoundation" not in production_sids
    assert "InspectQualificationLaunchWorkers" not in production_sids
    assert "ResizeQualificationLaunchWorkers" not in production_sids


def test_launch_coordinator_storage_is_retained_encrypted_and_fenced(
    synthesized_template,
):
    table_logical_id, table = next(
        entry
        for entry in _resource_entries(
            synthesized_template,
            "AWS::DynamoDB::Table",
        )
        if entry[1]["Properties"]["TableName"] == "axonllm-launch-rehearsal-leases"
    )
    properties = table["Properties"]
    assert properties["BillingMode"] == "PAY_PER_REQUEST"
    assert properties["DeletionProtectionEnabled"] is True
    assert properties["KeySchema"] == [{"AttributeName": "leaseKey", "KeyType": "HASH"}]
    assert properties["AttributeDefinitions"] == [
        {"AttributeName": "leaseKey", "AttributeType": "S"},
        {"AttributeName": "recordType", "AttributeType": "S"},
        {"AttributeName": "ownerExpiresAtEpoch", "AttributeType": "N"},
    ]
    assert properties["GlobalSecondaryIndexes"] == [
        {
            "IndexName": "owner-expiry",
            "KeySchema": [
                {"AttributeName": "recordType", "KeyType": "HASH"},
                {
                    "AttributeName": "ownerExpiresAtEpoch",
                    "KeyType": "RANGE",
                },
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expiresAtEpoch",
        "Enabled": True,
    }
    assert properties["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True,
        "RecoveryPeriodInDays": 35,
    }
    assert properties["ContributorInsightsSpecification"] == {"Enabled": True}
    assert properties["SSESpecification"]["SSEEnabled"] is True
    assert properties["SSESpecification"]["SSEType"] == "KMS"
    key_reference = properties["SSESpecification"]["KMSMasterKeyId"]
    key_logical_id = key_reference["Fn::GetAtt"][0]
    key = synthesized_template["Resources"][key_logical_id]
    assert key["Properties"]["Description"] == ("Encrypts AxonLLM AgentCore launch coordinator state")
    assert key["Properties"]["EnableKeyRotation"] is True
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    assert key["DeletionPolicy"] == "Retain"
    assert key["UpdateReplacePolicy"] == "Retain"

    deny = properties["ResourcePolicy"]["PolicyDocument"]["Statement"][0]
    assert deny == {
        "Action": "dynamodb:*",
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        "Effect": "Deny",
        "Principal": {"AWS": "*"},
        "Resource": "*",
        "Sid": "DenyInsecureTransport",
    }
    assert table_logical_id in json.dumps(
        _role_statements(
            synthesized_template,
            "AxonLLMLaunchCoordinatorExecutionRole",
        )
    )

    key_policy = key["Properties"]["KeyPolicy"]["Statement"]
    service_statements = {
        _literal_parts(statement["Principal"]["Service"]): statement
        for statement in key_policy
        if "Service" in statement.get("Principal", {})
    }
    assert set(service_statements) == {
        "cloudwatch.amazonaws.com",
        "logs.us-east-1.",
        "sns.amazonaws.com",
    }
    assert {
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
    } == _actions(service_statements["logs.us-east-1."])
    cloudwatch_key_use = service_statements["cloudwatch.amazonaws.com"]
    assert _actions(cloudwatch_key_use) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }
    assert cloudwatch_key_use["Condition"]["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    assert _literal_parts(cloudwatch_key_use["Condition"]["ArnLike"]["aws:SourceArn"]).endswith(
        ":alarm:axonllm-launch-*"
    )

    topic_logical_id, topic = next(
        (logical_id, resource)
        for logical_id, resource in _resource_entries(
            synthesized_template,
            "AWS::SNS::Topic",
        )
        if resource["Properties"]["TopicName"] == "axonllm-launch-coordinator-alarms"
    )
    assert topic["Properties"]["KmsMasterKeyId"] == {"Fn::GetAtt": [key_logical_id, "Arn"]}
    sns_key_use = service_statements["sns.amazonaws.com"]
    assert _actions(sns_key_use) == {
        "kms:Decrypt",
        "kms:GenerateDataKey",
    }
    assert sns_key_use["Condition"]["StringEquals"] == {
        "aws:SourceAccount": {"Ref": "AWS::AccountId"},
    }
    source_arn = sns_key_use["Condition"]["ArnEquals"]["aws:SourceArn"]
    assert _literal_parts(source_arn).endswith(
        ":sns:us-east-1::axonllm-launch-coordinator-alarms"
    )
    assert topic_logical_id not in json.dumps(key)

    receipt_queue_logical_id, _ = next(
        (logical_id, resource)
        for logical_id, resource in _resource_entries(
            synthesized_template,
            "AWS::SQS::Queue",
        )
        if resource["Properties"]["QueueName"] == "axonllm-launch-coordinator-alarm-receipts"
    )
    queue_policy = next(
        resource
        for resource in _resources(
            synthesized_template,
            "AWS::SQS::QueuePolicy",
        )
        if {"Ref": receipt_queue_logical_id} in resource["Properties"]["Queues"]
    )
    receive_alarm = next(
        statement
        for statement in queue_policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Sid") == "ReceiveExactCoordinatorAlarm"
    )
    assert receive_alarm["Principal"] == {"Service": "sns.amazonaws.com"}
    assert receive_alarm["Condition"] == {
        "ArnEquals": {
            "aws:SourceArn": {"Ref": topic_logical_id},
        },
        "StringEquals": {
            "aws:SourceAccount": {"Ref": "AWS::AccountId"},
        },
    }


def test_rehearsal_control_ledger_is_separate_retained_and_encrypted(
    synthesized_template,
):
    tables = {
        table["Properties"]["TableName"]: (logical_id, table)
        for logical_id, table in _resource_entries(
            synthesized_template,
            "AWS::DynamoDB::Table",
        )
    }
    assert set(tables) == {
        "axonllm-launch-rehearsal-leases",
        "axonllm-qualification-mutation-authorizations",
        "axonllm-rehearsal-control-ledger",
    }
    lease_id, lease = tables["axonllm-launch-rehearsal-leases"]
    authorization_id, authorization = tables[
        "axonllm-qualification-mutation-authorizations"
    ]
    ledger_id, ledger = tables["axonllm-rehearsal-control-ledger"]
    assert len({lease_id, authorization_id, ledger_id}) == 3
    properties = ledger["Properties"]
    assert properties["KeySchema"] == [{"AttributeName": "ledger_key", "KeyType": "HASH"}]
    assert properties["AttributeDefinitions"] == [{"AttributeName": "ledger_key", "AttributeType": "S"}]
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at_epoch",
        "Enabled": True,
    }
    assert properties["DeletionProtectionEnabled"] is True
    assert properties["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True,
        "RecoveryPeriodInDays": 35,
    }
    assert properties["SSESpecification"]["SSEEnabled"] is True
    assert properties["SSESpecification"]["SSEType"] == "KMS"
    assert properties["SSESpecification"]["KMSMasterKeyId"] == lease["Properties"]["SSESpecification"]["KMSMasterKeyId"]
    assert ledger["DeletionPolicy"] == "Retain"
    assert ledger["UpdateReplacePolicy"] == "Retain"
    assert properties["ResourcePolicy"]["PolicyDocument"]["Statement"][0]["Condition"] == {
        "Bool": {"aws:SecureTransport": "false"}
    }
    authorization_properties = authorization["Properties"]
    assert authorization_properties["KeySchema"] == [
        {"AttributeName": "authorizationId", "KeyType": "HASH"}
    ]
    assert authorization_properties["TimeToLiveSpecification"] == {
        "AttributeName": "expiresAtEpoch",
        "Enabled": True,
    }
    assert authorization_properties["DeletionProtectionEnabled"] is True
    assert authorization_properties["SSESpecification"]["KMSMasterKeyId"] == (
        lease["Properties"]["SSESpecification"]["KMSMasterKeyId"]
    )
    assert authorization["DeletionPolicy"] == "Retain"
    assert authorization["UpdateReplacePolicy"] == "Retain"

    for role_name in (
        "AxonLLMLaunchActionWorkerRole",
        "AxonLLMLaunchCleanupWorkerRole",
    ):
        statement = next(
            statement
            for statement in _role_statements(
                synthesized_template,
                role_name,
            )
            if statement.get("Sid") == "UseRehearsalControlLedger"
        )
        assert _actions(statement) == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
        }
        assert statement["Resource"] == {"Fn::GetAtt": [ledger_id, "Arn"]}


def test_launch_runtime_identity_is_generated_encrypted_and_retained(
    synthesized_template,
):
    secret_id, secret = next(
        iter(
            _resource_entries(
                synthesized_template,
                "AWS::SecretsManager::Secret",
            )
        )
    )
    properties = secret["Properties"]
    assert properties["Name"] == "axonllm/launch/runtime-identity"
    assert properties["GenerateSecretString"] == {
        "ExcludePunctuation": True,
        "IncludeSpace": False,
        "PasswordLength": 64,
        "RequireEachIncludedType": True,
    }
    assert "SecretString" not in properties
    assert "SecretStringTemplate" not in json.dumps(properties)
    assert properties["KmsKeyId"]["Fn::GetAtt"][1] == "Arn"
    assert secret["DeletionPolicy"] == "Retain"
    assert secret["UpdateReplacePolicy"] == "Retain"

    policy = _resources(
        synthesized_template,
        "AWS::SecretsManager::ResourcePolicy",
    )[0]
    assert policy["Properties"]["BlockPublicPolicy"] is True
    assert policy["Properties"]["SecretId"] == {"Fn::GetAtt": [secret_id, "Id"]}
    deny = policy["Properties"]["ResourcePolicy"]["Statement"][0]
    assert deny["Effect"] == "Deny"
    assert deny["Principal"] == {"AWS": "*"}
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert policy["DeletionPolicy"] == "Retain"
    assert policy["UpdateReplacePolicy"] == "Retain"


def test_only_qualification_can_replace_launch_runtime_identity(
    synthesized_template,
):
    secret_id = _resource_entries(
        synthesized_template,
        "AWS::SecretsManager::Secret",
    )[0][0]
    coordinator_key_id = next(
        logical_id
        for logical_id, key in _resource_entries(
            synthesized_template,
            "AWS::KMS::Key",
        )
        if key["Properties"]["Description"] == "Encrypts AxonLLM AgentCore launch coordinator state"
    )
    qualification = _role_statements(
        synthesized_template,
        "AxonLLMAgentCoreQualificationRole",
    )
    install = next(statement for statement in qualification if statement.get("Sid") == "InstallLaunchRuntimeIdentity")
    assert _actions(install) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
    }
    assert install["Resource"] == {"Fn::GetAtt": [secret_id, "Id"]}
    assert "Condition" not in install

    encryption = next(
        statement for statement in qualification if statement.get("Sid") == "EncryptLaunchRuntimeIdentity"
    )
    assert _actions(encryption) == {
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey",
    }
    assert encryption["Resource"] == {
        "Fn::GetAtt": [coordinator_key_id, "Arn"],
    }
    assert encryption["Condition"] == {
        "StringEquals": {
            "kms:CallerAccount": {"Ref": "AWS::AccountId"},
            "kms:ViaService": {
                "Fn::Join": [
                    "",
                    [
                        "secretsmanager.us-east-1.",
                        {"Ref": "AWS::URLSuffix"},
                    ],
                ]
            },
        }
    }

    for role_name in (
        "AxonLLMAgentCoreDeployRole",
        "AxonLLMAgentCoreRehearsalEvidenceRole",
        "AxonLLMAgentCoreTransitionWatchdogRole",
        "AxonLLMExternalOidcCertificationRole",
    ):
        statements = _role_statements(
            synthesized_template,
            role_name,
        )
        runtime_identity_statements = [
            statement
            for statement in statements
            if statement.get("Sid")
            in {
                "EncryptLaunchRuntimeIdentity",
                "InstallLaunchRuntimeIdentity",
            }
        ]
        assert runtime_identity_statements == []
        for statement in statements:
            if "secretsmanager:PutSecretValue" in _actions(statement):
                assert statement["Resource"] != {"Fn::GetAtt": [secret_id, "Id"]}


def test_launch_coordinator_is_versioned_standard_and_dispatches_29_actions(
    synthesized_template,
):
    state_machine_logical_id, state_machine = next(
        iter(
            _resource_entries(
                synthesized_template,
                "AWS::StepFunctions::StateMachine",
            )
        )
    )
    properties = state_machine["Properties"]
    assert properties["StateMachineName"] == "AxonLLMLaunchCoordinator"
    assert properties["StateMachineType"] == "STANDARD"
    assert properties["EncryptionConfiguration"]["Type"] == ("CUSTOMER_MANAGED_KMS_KEY")
    assert properties["EncryptionConfiguration"]["KmsDataKeyReusePeriodSeconds"] == 300
    assert properties["LoggingConfiguration"]["Level"] == "ALL"
    assert properties["LoggingConfiguration"]["IncludeExecutionData"] is False
    destinations = properties["LoggingConfiguration"]["Destinations"]
    assert len(destinations) == 1
    log_group_arn = destinations[0]["CloudWatchLogsLogGroup"]["LogGroupArn"]
    assert set(log_group_arn) == {"Fn::GetAtt"}
    log_group_logical_id, attribute = log_group_arn["Fn::GetAtt"]
    assert attribute == "Arn"
    log_group = synthesized_template["Resources"][log_group_logical_id]
    assert log_group["Type"] == "AWS::Logs::LogGroup"
    assert log_group["Properties"]["LogGroupName"] == (
        "/aws/vendedlogs/states/AxonLLMLaunchCoordinator"
    )
    assert properties["TracingConfiguration"] == {"Enabled": True}
    assert {tag["Key"]: tag["Value"] for tag in properties["Tags"]} == {
        "Application": "AxonLLM",
        "Environment": "production",
        "Purpose": "agentcore-launch-rehearsal",
    }
    assert state_machine["DeletionPolicy"] == "Retain"
    assert state_machine["UpdateReplacePolicy"] == "Retain"

    definition = properties["Definition"]
    assert definition["TimeoutSeconds"] == 1800
    validation = definition["States"]["ValidateOperation"]
    action_choice = validation["Choices"][1]["And"][1]["Or"]
    assert len(action_choice) == 29
    assert {choice["StringEquals"] for choice in action_choice} == _LAUNCH_ACTIONS
    assert validation["Default"] == "RejectOperation"

    acquire = definition["States"]["AcquireActionLease"]
    assert acquire["Resource"].endswith("aws-sdk:dynamodb:updateItem")
    assert acquire["Parameters"]["ReturnValues"] == "ALL_NEW"
    assert "ADD fenceToken :one" in acquire["Parameters"]["UpdateExpression"]
    assert "attribute_not_exists(leaseKey)" in acquire["Parameters"]["ConditionExpression"]
    assert "idempotencyKey = :idempotency" in acquire["Parameters"]["ConditionExpression"]
    for state_name in ("MarkActionComplete", "MarkActionFailed"):
        state = definition["States"][state_name]
        assert "fenceToken = :fence" in state["Parameters"]["ConditionExpression"]
        assert state["ResultPath"] in {
            "$.leaseCompletion",
            "$.leaseFailure",
        }
    assert definition["States"]["MarkActionFailed"]["Parameters"]["ExpressionAttributeValues"].keys() == {
        ":active",
        ":failed",
        ":failedAt",
        ":fence",
        ":owner",
    }

    activities = {
        resource["Properties"]["Name"]: (logical_id, resource)
        for logical_id, resource in _resource_entries(
            synthesized_template,
            "AWS::StepFunctions::Activity",
        )
    }
    assert set(activities) == {
        "axonllm-agentcore-launch-actions",
        "axonllm-agentcore-launch-cleanup",
    }
    for _, activity in activities.values():
        encryption = activity["Properties"]["EncryptionConfiguration"]
        assert encryption["Type"] == "CUSTOMER_MANAGED_KMS_KEY"
        assert encryption["KmsDataKeyReusePeriodSeconds"] == 300
        assert activity["DeletionPolicy"] == "Retain"
        assert activity["UpdateReplacePolicy"] == "Retain"
    action_activity_id = activities["axonllm-agentcore-launch-actions"][0]
    cleanup_activity_id = activities["axonllm-agentcore-launch-cleanup"][0]
    assert definition["States"]["RunActionWorker"]["Resource"] == {"Fn::GetAtt": [action_activity_id, "Arn"]}
    assert definition["States"]["RunCleanupWorker"]["Resource"] == {"Fn::GetAtt": [cleanup_activity_id, "Arn"]}
    assert definition["States"]["RunCleanupMaintenanceWorker"]["Resource"] == {
        "Fn::GetAtt": [cleanup_activity_id, "Arn"]
    }
    assert definition["States"]["RunActionWorker"]["HeartbeatSeconds"] == 60
    assert definition["States"]["RunCleanupWorker"]["HeartbeatSeconds"] == 60
    maintenance = definition["States"]["RunCleanupMaintenanceWorker"]
    assert maintenance["HeartbeatSeconds"] == 60
    assert maintenance["TimeoutSeconds"] == 300
    assert maintenance["ResultPath"] == "$.maintenanceResult"
    continuation = definition["States"]["PrepareCleanupContinuation"]
    assert continuation["Parameters"] == {
        "schema": ("axonllm.agentcore-launch-rehearsal-maintenance/v1"),
        "operation": "cleanup-expired",
        "cursor.$": "$.maintenanceResult.nextCursor",
        "page.$": "$.maintenanceResult.page",
    }
    assert continuation["Next"] == "RunCleanupMaintenanceWorker"
    assert definition["States"]["ReturnCleanupMaintenanceResult"] == {
        "Type": "Pass",
        "OutputPath": "$.maintenanceResult",
        "End": True,
    }

    version_logical_id, version = next(
        iter(
            _resource_entries(
                synthesized_template,
                "AWS::StepFunctions::StateMachineVersion",
            )
        )
    )
    assert version["Properties"]["StateMachineArn"] == {"Fn::GetAtt": [state_machine_logical_id, "Arn"]}
    assert version["Properties"]["StateMachineRevisionId"] == {
        "Fn::GetAtt": [
            state_machine_logical_id,
            "StateMachineRevisionId",
        ]
    }
    assert version["DeletionPolicy"] == "Retain"
    assert version["UpdateReplacePolicy"] == "Retain"

    launch_start = next(
        statement
        for statement in _role_statements(
            synthesized_template,
            "AxonLLMLaunchGatesRole",
        )
        if statement.get("Sid") == "StartExactLaunchCoordinatorVersion"
    )
    assert launch_start["Resource"] == {"Fn::GetAtt": [version_logical_id, "Arn"]}


def test_launch_role_only_starts_and_observes_the_exact_coordinator(
    synthesized_template,
):
    statements = _role_statements(
        synthesized_template,
        "AxonLLMLaunchGatesRole",
    )
    states_statements = [
        statement for statement in statements if any(action.startswith("states:") for action in _actions(statement))
    ]
    assert {action for statement in states_statements for action in _actions(statement)} == {
        "states:DescribeExecution",
        "states:StartExecution",
        "states:StopExecution",
    }
    actions = {action for statement in statements for action in _actions(statement)}
    assert actions <= {
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey",
        "kms:Sign",
        "kms:Verify",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketPolicy",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
        "s3:GetObject",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
        "s3:ListBucketVersions",
        "s3:PutObject",
        "s3:PutObjectRetention",
        "sns:GetTopicAttributes",
        "sns:ListSubscriptionsByTopic",
        "sns:Publish",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ReceiveMessage",
        "states:DescribeExecution",
        "states:StartExecution",
        "states:StopExecution",
    }
    assert not any(
        action
        in {
            "iam:PassRole",
            "states:ListExecutions",
            "states:StartSyncExecution",
        }
        for statement in statements
        for action in _actions(statement)
    )

    start = next(statement for statement in states_statements if _actions(statement) == {"states:StartExecution"})
    assert start["Resource"]["Fn::GetAtt"][1] == "Arn"
    version_logical_id = start["Resource"]["Fn::GetAtt"][0]
    assert synthesized_template["Resources"][version_logical_id]["Type"] == "AWS::StepFunctions::StateMachineVersion"

    observe = next(
        statement
        for statement in states_statements
        if _actions(statement) == {"states:DescribeExecution", "states:StopExecution"}
    )
    assert _literal_parts(observe["Resource"]).endswith(":execution:AxonLLMLaunchCoordinator:*")
    key_use = next(statement for statement in statements if statement.get("Sid") == "UseCoordinatorKeyViaStates")
    assert _actions(key_use) == {
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:GenerateDataKey",
    }
    assert _literal_parts(key_use["Condition"]["StringEquals"]["kms:ViaService"]) == "states.us-east-1."

    alarm_delivery = next(statement for statement in statements if statement.get("Sid") == "VerifyLaunchAlarmDelivery")
    assert _actions(alarm_delivery) == {
        "sns:GetTopicAttributes",
        "sns:ListSubscriptionsByTopic",
        "sns:Publish",
    }
    alarm_receipt = next(statement for statement in statements if statement.get("Sid") == "ConsumeLaunchAlarmReceipt")
    assert _actions(alarm_receipt) == {
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ReceiveMessage",
    }
    coordinator_key_id = next(
        logical_id
        for logical_id, key in _resource_entries(
            synthesized_template,
            "AWS::KMS::Key",
        )
        if key["Properties"]["Description"] == "Encrypts AxonLLM AgentCore launch coordinator state"
    )
    sns_key_use = next(statement for statement in statements if statement.get("Sid") == "EncryptLaunchAlarmViaSns")
    assert _actions(sns_key_use) == {
        "kms:Decrypt",
        "kms:GenerateDataKey",
    }
    assert sns_key_use["Resource"] == {"Fn::GetAtt": [coordinator_key_id, "Arn"]}
    assert _literal_parts(sns_key_use["Condition"]["StringEquals"]["kms:ViaService"]) == "sns.us-east-1."
    sqs_key_use = next(
        statement for statement in statements if statement.get("Sid") == "DecryptLaunchAlarmReceiptViaSqs"
    )
    assert _actions(sqs_key_use) == {"kms:Decrypt"}
    assert sqs_key_use["Resource"] == {"Fn::GetAtt": [coordinator_key_id, "Arn"]}
    assert _literal_parts(sqs_key_use["Condition"]["StringEquals"]["kms:ViaService"]) == "sqs.us-east-1."
    evidence_listing = next(
        statement for statement in statements if statement.get("Sid") == "ListBoundEvidenceVersions"
    )
    assert evidence_listing["Condition"]["StringLike"]["s3:prefix"] == [f"{_REHEARSAL_EVIDENCE_PREFIX}/*"]


def test_coordinator_worker_roles_are_separate_activity_pollers(
    synthesized_template,
):
    activity_entries = {
        resource["Properties"]["Name"]: logical_id
        for logical_id, resource in _resource_entries(
            synthesized_template,
            "AWS::StepFunctions::Activity",
        )
    }
    role_expectations = {
        "AxonLLMLaunchActionWorkerRole": {
            "activity": "axonllm-agentcore-launch-actions",
            "lease_sid": "UseAssignedFencedLaunchLease",
            "lease_actions": {
                "dynamodb:GetItem",
                "dynamodb:TransactWriteItems",
                "dynamodb:UpdateItem",
            },
            "domain_sid": "ExerciseOwnedDomainState",
            "domain_required": {
                "dynamodb:DeleteTable",
                "dynamodb:RestoreTableToPointInTime",
                "dynamodb:UpdateTimeToLive",
            },
            "domain_forbidden": {"dynamodb:BatchWriteItem"},
            "runtime_sid": "ExerciseBoundAgentCoreRuntime",
        },
        "AxonLLMLaunchCleanupWorkerRole": {
            "activity": "axonllm-agentcore-launch-cleanup",
            "lease_sid": "CleanOwnedFencedLaunchLeases",
            "lease_actions": {
                "dynamodb:BatchWriteItem",
                "dynamodb:DeleteItem",
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:TransactWriteItems",
                "dynamodb:UpdateItem",
            },
            "domain_sid": "RemoveOwnedDomainState",
            "domain_required": {
                "dynamodb:BatchWriteItem",
                "dynamodb:DeleteTable",
                "dynamodb:UpdateTimeToLive",
            },
            "domain_forbidden": {"dynamodb:RestoreTableToPointInTime"},
            "runtime_sid": "VerifyCleanedAgentCoreRuntime",
        },
    }
    for role_name, expected in role_expectations.items():
        statements = _role_statements(synthesized_template, role_name)
        poll = next(statement for statement in statements if statement.get("Sid") == "PollDedicatedLaunchActivity")
        assert _actions(poll) == {"states:GetActivityTask"}
        assert poll["Resource"] == {
            "Fn::GetAtt": [
                activity_entries[expected["activity"]],
                "Arn",
            ]
        }
        callback = next(statement for statement in statements if statement.get("Sid") == "CompleteAssignedLaunchTask")
        assert _actions(callback) == {
            "states:SendTaskFailure",
            "states:SendTaskHeartbeat",
            "states:SendTaskSuccess",
        }
        assert callback["Resource"] == "*"
        assert callback["Condition"] == {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}
        lease_statement = next(statement for statement in statements if statement.get("Sid") == expected["lease_sid"])
        assert _actions(lease_statement) == expected["lease_actions"]
        lease_resources = lease_statement["Resource"]
        if role_name == "AxonLLMLaunchCleanupWorkerRole":
            assert len(lease_resources) == 2
            assert lease_resources[0]["Fn::GetAtt"][1] == "Arn"
            assert _literal_parts(lease_resources[1]).endswith("/index/owner-expiry")
        else:
            assert lease_resources["Fn::GetAtt"][1] == "Arn"

        domain_statement = next(statement for statement in statements if statement.get("Sid") == expected["domain_sid"])
        assert expected["domain_required"] <= _actions(domain_statement)
        assert expected["domain_forbidden"].isdisjoint(_actions(domain_statement))
        assert "FargateStateTableName" not in json.dumps(domain_statement["Resource"])
        domain_resources = json.dumps(domain_statement["Resource"])
        assert "axonllm-agentcore-state-managed" in domain_resources
        assert "axonllm-agentcore-state-*" not in domain_resources

        runtime = next(statement for statement in statements if statement.get("Sid") == expected["runtime_sid"])
        assert _actions(runtime) == {
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:InvokeAgentRuntime",
        }
        assert all(":runtime/axonllm_managed-*" in _literal_parts(resource) for resource in runtime["Resource"])

        identity = next(statement for statement in statements if statement.get("Sid") == "ReadLaunchRuntimeIdentity")
        assert _actions(identity) == {
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
        }
        identity_logical_id = identity["Resource"]["Fn::GetAtt"][0]
        assert synthesized_template["Resources"][identity_logical_id]["Type"] == "AWS::SecretsManager::Secret"
        assert any(statement.get("Sid") == "UseCoordinatorKeyViaSecretsmanager" for statement in statements)

        security_events = next(
            statement
            for statement in statements
            if statement.get("Sid")
            in {
                "ExerciseSecurityEventDelivery",
                "DrainOwnedSecurityEventFixtures",
            }
        )
        assert {
            "sqs:ChangeMessageVisibility",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:ReceiveMessage",
        } <= _actions(security_events)
        if role_name == "AxonLLMLaunchActionWorkerRole":
            assert "sqs:SendMessage" in _actions(security_events)
        else:
            assert "sqs:SendMessage" not in _actions(security_events)
        assert all(
            "AxonLLMAgentCoreStack-managed-SecurityEvent" in _literal_parts(resource)
            for resource in security_events["Resource"]
        )

        selectors = next(
            statement
            for statement in statements
            if statement.get("Sid") == "InspectReviewedQualificationSelectors"
        )
        assert _actions(selectors) == {"cloudformation:DescribeStacks"}
        assert all(
            name in json.dumps(selectors["Resource"])
            for name in (
                "AxonLLMAgentCoreStack-managed",
                "AxonLLMControlPlaneStack-managed",
            )
        )
        assert "AxonLLMAgentCoreStack/*" not in json.dumps(selectors["Resource"])

        broker_invoke = next(
            statement
            for statement in statements
            if _actions(statement) == {"lambda:InvokeFunction"}
        )
        assert set(broker_invoke["Resource"]) == {"Ref"}
        version_id = broker_invoke["Resource"]["Ref"]
        assert synthesized_template["Resources"][version_id]["Type"] == (
            "AWS::Lambda::Version"
        )
        all_actions = {
            action for statement in statements for action in _actions(statement)
        }
        assert {
            "cloudformation:UpdateStack",
            "iam:PassRole",
        }.isdisjoint(all_actions)

        ecs_read = next(
            statement for statement in statements if statement.get("Sid") == "InspectQualificationControlService"
        )
        assert _actions(ecs_read) == {"ecs:DescribeServices"}
        assert ecs_read["Resource"] == "*"
        ecs_write = next(
            statement for statement in statements if statement.get("Sid") == "ResizeQualificationControlService"
        )
        assert _actions(ecs_write) == {"ecs:UpdateService"}
        assert "AxonLLMControlPlaneStack-managed-Service*" in _literal_parts(ecs_write["Resource"])

        scaling = next(
            statement for statement in statements if statement.get("Sid") == "ManageQualificationControlScaling"
        )
        assert _actions(scaling) == {
            "application-autoscaling:DescribeScalableTargets",
            "application-autoscaling:RegisterScalableTarget",
        }
        assert scaling["Resource"] == "*"

        event_logs = next(
            statement
            for statement in statements
            if statement.get("Sid")
            in {
                "ExerciseSecurityEventLogDelivery",
                "RemoveOwnedSecurityEventStreams",
            }
        )
        assert _actions(event_logs) == (
            {"logs:DeleteLogStream"}
            if role_name == "AxonLLMLaunchCleanupWorkerRole"
            else {"logs:CreateLogStream", "logs:FilterLogEvents"}
        )
        assert all(
            "AxonLLMAgentCoreStack-managed-SecurityEventLogGroup" in _literal_parts(resource)
            for resource in event_logs["Resource"]
        )

    action_statements = _role_statements(
        synthesized_template,
        "AxonLLMLaunchActionWorkerRole",
    )
    cleanup_statements = _role_statements(
        synthesized_template,
        "AxonLLMLaunchCleanupWorkerRole",
    )
    assert any(statement.get("Sid") == "GrantAgentCoreStateKeysForRestore" for statement in action_statements)
    assert not any(statement.get("Sid") == "GrantAgentCoreStateKeysForRestore" for statement in cleanup_statements)
    for statements in (action_statements, cleanup_statements):
        data_key = next(
            statement for statement in statements if statement.get("Sid") == "UseRuntimeDataKeyThroughDomainServices"
        )
        assert data_key["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"] == [
            "alias/axonllm/agentcore-data-managed"
        ]

    execution_statements = _role_statements(
        synthesized_template,
        "AxonLLMLaunchCoordinatorExecutionRole",
    )
    lease_statement = next(
        statement for statement in execution_statements if statement.get("Sid") == "AdvanceFencedLaunchLease"
    )
    assert _actions(lease_statement) == {"dynamodb:UpdateItem"}
    assert not any(
        action in {"iam:PassRole", "sts:AssumeRole"}
        for statement in execution_statements
        for action in _actions(statement)
    )
    transaction_policies = [
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::IAM::Policy",
        )
        if "dynamodb:TransactWriteItems"
        in {
            action
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            for action in _actions(statement)
        }
    ]
    assert len(transaction_policies) == 3
    assert all(
        policy.get("Metadata")
        == {"cfn-lint": {"config": {"ignore_checks": ["W3037"]}}}
        for policy in transaction_policies
    )
    assert not any(
        "W3037"
        in json.dumps(policy.get("Metadata", {}))
        for policy in _resources(
            synthesized_template,
            "AWS::IAM::Policy",
        )
        if policy not in transaction_policies
    )

    scheduler_trust = _role(
        synthesized_template,
        "AxonLLMLaunchCoordinatorSchedulerRole",
    )["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert _literal_parts(scheduler_trust["Condition"]["ArnLike"]["aws:SourceArn"]).endswith(
        ":scheduler:us-east-1::schedule-group/axonllm-launch-coordinator"
    )

    # The foundation contains only the bounded mutation brokers. Deployable
    # worker compute and the 29 domain implementations live in the assigned
    # launch-worker stack.
    functions = _resources(
        synthesized_template,
        "AWS::Lambda::Function",
    )
    assert {
        function["Properties"]["FunctionName"] for function in functions
    } == {
        "axonllm-production-transition-mutation-broker",
        "axonllm-qualification-selector-mutation-broker",
    }
    assert all(
        function["Properties"]["Handler"] == "index.lambda_handler"
        and function["Properties"]["ReservedConcurrentExecutions"] == 1
        for function in functions
    )
    assert (
        _resources(
            synthesized_template,
            "AWS::ECS::TaskDefinition",
        )
        == []
    )
    assert (
        _resources(
            synthesized_template,
            "AWS::ECS::Service",
        )
        == []
    )


def test_cleanup_and_watchdog_are_durable_scheduled_and_actionable(
    synthesized_template,
):
    version_logical_id = _resource_entries(
        synthesized_template,
        "AWS::StepFunctions::StateMachineVersion",
    )[0][0]
    schedules = _resources(
        synthesized_template,
        "AWS::Scheduler::Schedule",
    )
    assert len(schedules) == 2
    by_name = {schedule["Properties"]["Name"]: schedule for schedule in schedules}
    assert set(by_name) == {
        "axonllm-launch-coordinator-cleanup",
        "axonllm-launch-coordinator-watchdog",
    }
    assert by_name["axonllm-launch-coordinator-cleanup"]["Properties"]["ScheduleExpression"] == "rate(15 minutes)"
    assert by_name["axonllm-launch-coordinator-watchdog"]["Properties"]["ScheduleExpression"] == "rate(5 minutes)"
    operations = set()
    target_roles = set()
    dead_letter_queues = set()
    for schedule in schedules:
        properties = schedule["Properties"]
        assert properties["State"] == "ENABLED"
        assert properties["FlexibleTimeWindow"] == {"Mode": "OFF"}
        assert properties["GroupName"] == "axonllm-launch-coordinator"
        assert properties["KmsKeyArn"]["Fn::GetAtt"][1] == "Arn"
        target = properties["Target"]
        assert target["Arn"] == {"Fn::GetAtt": [version_logical_id, "Arn"]}
        assert target["RetryPolicy"] == {
            "MaximumEventAgeInSeconds": 86400,
            "MaximumRetryAttempts": 185,
        }
        payload = json.loads(target["Input"])
        assert payload["schema"] == ("axonllm.agentcore-launch-rehearsal-maintenance/v1")
        operations.add(payload["operation"])
        target_roles.add(json.dumps(target["RoleArn"], sort_keys=True))
        dead_letter_queues.add(
            json.dumps(
                target["DeadLetterConfig"]["Arn"],
                sort_keys=True,
            )
        )
        assert schedule["DeletionPolicy"] == "Retain"
        assert schedule["UpdateReplacePolicy"] == "Retain"
    assert operations == {"cleanup-expired", "watchdog"}
    assert len(target_roles) == 1
    assert len(dead_letter_queues) == 1

    queue = _resources(synthesized_template, "AWS::SQS::Queue")[0]
    assert queue["Properties"]["QueueName"] == ("axonllm-launch-coordinator-scheduler-dlq")
    assert queue["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 60 * 60
    assert queue["Properties"]["KmsMasterKeyId"]["Fn::GetAtt"][1] == "Arn"
    assert queue["DeletionPolicy"] == "Retain"
    queue_policy = _resources(
        synthesized_template,
        "AWS::SQS::QueuePolicy",
    )[0]
    deny = next(
        statement
        for statement in queue_policy["Properties"]["PolicyDocument"]["Statement"]
        if statement["Effect"] == "Deny"
    )
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}

    alarms = {
        alarm["Properties"]["AlarmName"]: alarm
        for alarm in _resources(
            synthesized_template,
            "AWS::CloudWatch::Alarm",
        )
    }
    assert set(alarms) == {
        "axonllm-launch-coordinator-execution-failures",
        "axonllm-launch-coordinator-scheduler-dead-letters",
        "axonllm-launch-rehearsal-watchdog",
    }
    for alarm in alarms.values():
        assert alarm["Properties"]["AlarmActions"]
        assert alarm["Properties"]["OKActions"]
    watchdog = alarms["axonllm-launch-rehearsal-watchdog"]["Properties"]
    assert watchdog["Namespace"] == "AxonLLM/LaunchCoordinator"
    assert watchdog["MetricName"] == "WatchdogHeartbeat"
    assert watchdog["TreatMissingData"] == "breaching"
    assert watchdog["ComparisonOperator"] == "LessThanThreshold"
    assert watchdog["DatapointsToAlarm"] == 2

    log_group = next(
        resource
        for resource in _resources(
            synthesized_template,
            "AWS::Logs::LogGroup",
        )
        if resource["Properties"]["LogGroupName"]
        == "/aws/vendedlogs/states/AxonLLMLaunchCoordinator"
    )
    assert log_group["Properties"]["LogGroupName"] == ("/aws/vendedlogs/states/AxonLLMLaunchCoordinator")
    assert log_group["Properties"]["DeletionProtectionEnabled"] is True
    assert log_group["Properties"]["RetentionInDays"] == 3653
    assert log_group["Properties"]["KmsKeyId"]["Fn::GetAtt"][1] == "Arn"
    assert log_group["DeletionPolicy"] == "Retain"


def test_repository_policy_denies_insecure_transport(
    synthesized_template,
):
    policies = _resources(
        synthesized_template,
        "AWS::ECR::Repository",
    )
    for repository in policies:
        statements = repository["Properties"]["RepositoryPolicyText"]["Statement"]
        deny = next(statement for statement in statements if statement.get("Sid") == "DenyInsecureTransport")
        assert deny["Effect"] == "Deny"
        assert deny["Principal"] == {"AWS": "*"}
        assert deny["Action"] == "ecr:*"
        assert "Resource" not in deny
        assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_foundation_outputs_all_operator_inputs(synthesized_template):
    assert set(synthesized_template["Outputs"]) == {
        "AgentCoreDeployRoleArn",
        "AgentCoreLaunchGatesRoleArn",
        "AgentCoreQualificationRoleArn",
        "AgentCoreRehearsalEvidenceRoleArn",
        "AgentCoreRepositoryUri",
        "AgentCoreTransitionWatchdogRoleArn",
        "DeploymentEvidenceBucketArn",
        "DeploymentEvidenceBucketName",
        "DeploymentEvidenceKeyArn",
        "DeploymentEvidencePrefix",
        "ExternalOidcCertificationRoleArn",
        "ExternalOidcEvidencePrefix",
        "FargateRepositoryUri",
        "GitHubOidcProviderArn",
        "LaunchCoordinatorActionActivityArn",
        "LaunchCoordinatorActionWorkerRoleArn",
        "LaunchCoordinatorAlarmReceiptQueueArn",
        "LaunchCoordinatorAlarmReceiptQueueUrl",
        "LaunchCoordinatorAlarmTopicArn",
        "LaunchCoordinatorCleanupActivityArn",
        "LaunchCoordinatorCleanupWorkerRoleArn",
        "LaunchCoordinatorExecutionRoleArn",
        "LaunchCoordinatorKeyArn",
        "LaunchCoordinatorLeaseTableArn",
        "LaunchCoordinatorScheduleGroupArn",
        "LaunchCoordinatorSchedulerDeadLetterQueueArn",
        "LaunchCoordinatorSchedulerRoleArn",
        "LaunchCoordinatorStateMachineArn",
        "LaunchCoordinatorStateMachineVersionArn",
        "LaunchCoordinatorWatchdogAlarmArn",
        "LaunchPrerequisiteSigningKeyArn",
        "LaunchRehearsalEvidencePrefix",
        "LaunchRuntimeIdentitySecretArn",
        "OperationsAuditRoleArn",
        "OperationsRecoveryRoleArn",
        "ProductionTransitionSigningKeyArn",
        "ProductionTransitionTerminalSigningKeyArn",
        "ProductionTransitionMutationBrokerVersionArn",
        "QualificationMutationAuthorizationTableArn",
        "QualificationMutationBrokerVersionArn",
        "QualificationTeardownEvidencePrefix",
        "RehearsalControlLedgerTableArn",
        "RehearsalControlLedgerTableName",
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
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(tmp_path / "jsii-cache"),
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
