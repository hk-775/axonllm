"""Contracts for the bounded release-foundation CDK bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.gateway.deployment import release_foundation_bootstrap as bootstrap
from src.gateway.deployment import release_foundation_policy as policy


ACCOUNT_ID = "123456789012"
REGION = "us-east-1"
PARTITION = "aws"


def _actions(document: dict) -> set[str]:
    values: set[str] = set()
    for statement in document["Statement"]:
        action = statement.get("Action")
        if isinstance(action, str):
            values.add(action)
        elif isinstance(action, list):
            values.update(action)
    return values


def test_execution_policy_is_fixed_size_and_excludes_data_access():
    documents = policy.execution_policy_documents(
        partition=PARTITION,
        account_id=ACCOUNT_ID,
        region=REGION,
    )
    assert len(documents) == policy.EXECUTION_POLICY_PART_COUNT == 5
    assert all(
        len(json.dumps(document, separators=(",", ":"), sort_keys=True))
        <= policy.IAM_MANAGED_POLICY_SIZE_LIMIT
        for document in documents
    )
    serialized = json.dumps(documents, sort_keys=True)
    allowed = {
        action
        for document in documents
        for statement in document["Statement"]
        if statement["Effect"] == "Allow"
        for action in _actions({"Statement": [statement]})
    }
    for prohibited in (
        "AdministratorAccess",
        "hnb659fds",
    ):
        assert prohibited not in serialized
    for prohibited in (
        "iam:CreateAccessKey",
        "iam:CreateUser",
        "kms:Sign",
        "s3:PutObject",
    ):
        assert prohibited not in allowed
    crypto = [
        statement
        for document in documents
        for statement in document["Statement"]
        if {"kms:Decrypt", "kms:GenerateDataKey"}
        <= _actions({"Statement": [statement]})
    ]
    assert len(crypto) == 2
    assert all(
        "kms:ViaService"
        in statement["Condition"]["StringEquals"]
        for statement in crypto
    )
    asset_reads = [
        statement
        for document in documents
        for statement in document["Statement"]
        if statement.get("Sid") == "ReadDedicatedCdkTemplateAssets"
    ]
    assert len(asset_reads) == 1
    assert set(asset_reads[0]["Action"]) == {
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:ListBucket",
    }
    assert all(
        "cdk-axrel-assets-123456789012-us-east-1"
        in resource
        for resource in asset_reads[0]["Resource"]
    )
    bootstrap_reads = [
        statement
        for document in documents
        for statement in document["Statement"]
        if statement.get("Sid") == "ReadDedicatedCdkBootstrapVersion"
    ]
    assert len(bootstrap_reads) == 1
    assert set(bootstrap_reads[0]["Action"]) == {
        "ssm:GetParameter",
        "ssm:GetParameters",
    }
    assert bootstrap_reads[0]["Resource"] == (
        "arn:aws:ssm:us-east-1:123456789012:"
        "parameter/cdk-bootstrap/axrel/version"
    )
    log_access = [
        statement
        for document in documents
        for statement in document["Statement"]
        if statement.get("Sid") == "ManageReleaseFoundationLogs"
    ]
    assert len(log_access) == 1
    assert "logs:ListTagsForResource" in log_access[0]["Action"]
    assert {
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/lambda/axonllm-production-transition-mutation-broker"
        ),
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/lambda/axonllm-production-transition-mutation-broker:*"
        ),
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/lambda/axonllm-qualification-selector-mutation-broker"
        ),
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/lambda/axonllm-qualification-selector-mutation-broker:*"
        ),
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/vendedlogs/states/AxonLLMLaunchCoordinator"
        ),
        (
            "arn:aws:logs:us-east-1:123456789012:log-group:"
            "/aws/vendedlogs/states/AxonLLMLaunchCoordinator:*"
        ),
    } == set(log_access[0]["Resource"])


def test_execution_policy_binds_roles_boundaries_and_oidc_provider():
    document = policy.execution_policy_document(
        partition=PARTITION,
        account_id=ACCOUNT_ID,
        region=REGION,
    )
    create = next(
        statement
        for statement in document["Statement"]
        if statement["Sid"] == "CreateBoundedFoundationRoles"
    )
    assert {
        resource.rsplit("/", 1)[-1]
        for resource in create["Resource"]
    } == set(policy.FOUNDATION_ROLE_NAMES)
    assert create["Condition"]["StringEquals"] == {
        "iam:PermissionsBoundary": (
            "arn:aws:iam::123456789012:policy/"
            "AxonLLMReleaseFoundationRoleBoundary-axrel-us-east-1"
        ),
        "aws:RequestTag/Application": "AxonLLM",
        "aws:RequestTag/AxonLLMTrustDomain": "axrel",
    }
    managed = next(
        statement
        for statement in document["Statement"]
        if statement["Sid"] == "ManageBoundedFoundationRoles"
    )
    assert "iam:UpdateAssumeRolePolicy" in managed["Action"]
    assert managed["Resource"] == create["Resource"]
    oidc = next(
        statement
        for statement in document["Statement"]
        if statement["Sid"] == "ManageExactGitHubOidcProvider"
    )
    assert oidc["Resource"] == (
        "arn:aws:iam::123456789012:"
        "oidc-provider/token.actions.githubusercontent.com"
    )


def test_boundaries_prevent_identity_and_credential_escalation():
    service = policy.service_boundary_document(
        partition=PARTITION,
        account_id=ACCOUNT_ID,
        region=REGION,
    )
    identity_deny = next(
        statement
        for statement in service["Statement"]
        if statement["Sid"] == "DenyIdentityMutation"
    )
    assert identity_deny["Effect"] == "Deny"
    assert set(identity_deny["NotAction"]) == {
        "iam:Get*",
        "iam:List*",
        "iam:PassRole",
        "sts:AssumeRole",
    }
    assume_deny = next(
        statement
        for statement in service["Statement"]
        if statement["Sid"] == "DenyUnexpectedRoleAssumption"
    )
    assert assume_deny["Effect"] == "Deny"
    assert len(assume_deny["NotResource"]) == 14
    assert all(
        "cdk-" in resource
        for resource in assume_deny["NotResource"]
    )

    boundary = policy.bootstrap_boundary_document(
        partition=PARTITION,
        account_id=ACCOUNT_ID,
        region=REGION,
    )
    assert (
        len(json.dumps(boundary, separators=(",", ":"), sort_keys=True))
        <= policy.IAM_MANAGED_POLICY_SIZE_LIMIT
    )
    serialized = json.dumps(boundary, sort_keys=True)
    assert "iam:CreateAccessKey" in serialized
    assert "iam:UpdateAssumeRolePolicy" in serialized
    assert "secretsmanager:GetSecretValue" in serialized
    assert "s3:GetObject" in serialized
    assert "kms:Sign" in serialized
    assert "sso-admin:" not in serialized
    evidence_deny = next(
        statement
        for statement in boundary["Statement"]
        if statement["Sid"] == "DenyEvidenceObjectAccess"
    )
    assert evidence_deny["Resource"] == (
        "arn:aws:s3:::"
        "axonllm-deployment-evidence-123456789012-us-east-1/*"
    )
    stack_deny = next(
        statement
        for statement in boundary["Statement"]
        if statement["Sid"] == "DenyUnexpectedCloudFormationMutation"
    )
    assert all(
        "AxonLLMReleaseFoundation" in resource
        for resource in stack_deny["NotResource"]
    )
    baseline = next(
        statement
        for statement in boundary["Statement"]
        if statement["Sid"] == "AllowReviewedNonIdentityPermissions"
    )
    assert {"iam:*", "sts:*"} <= set(baseline["NotAction"])
    identity_allows = {
        statement["Sid"]: statement
        for statement in boundary["Statement"]
        if statement["Effect"] == "Allow"
        and any(
            action.startswith(("iam:", "sts:"))
            for action in _actions({"Statement": [statement]})
        )
    }
    assert set(identity_allows) == {
        "AllowCallerIdentityInspection",
        "AllowExactOidcManagement",
        "AllowExpectedManagedPolicyManagement",
        "AllowExpectedRoleManagement",
        "AllowExpectedRolePassing",
        "AllowIdentityMetadataInspection",
    }
    for sid, statement in identity_allows.items():
        if sid in {
            "AllowCallerIdentityInspection",
            "AllowIdentityMetadataInspection",
        }:
            continue
        assert statement["Resource"] != "*"
        assert "NotResource" not in statement


def test_execution_policy_scopes_regional_mutations_to_foundation_resources():
    document = policy.execution_policy_document(
        partition=PARTITION,
        account_id=ACCOUNT_ID,
        region=REGION,
    )
    for statement in document["Statement"]:
        if statement["Effect"] != "Allow" or statement["Resource"] != "*":
            continue
        actions = _actions({"Statement": [statement]})
        assert actions <= {
            "kms:CreateKey",
            "kms:ListAliases",
            "logs:DescribeLogGroups",
            "secretsmanager:GetRandomPassword",
            "states:ListActivities",
            "states:ValidateStateMachineDefinition",
        }
    lambda_access = next(
        statement
        for statement in document["Statement"]
        if statement["Sid"] == "ManageReleaseFoundationLambda"
    )
    assert all(
        ":function:axonllm-" in resource
        for resource in lambda_access["Resource"]
    )
    secret_access = next(
        statement
        for statement in document["Statement"]
        if statement["Sid"] == "ManageReleaseFoundationSecretsmanager"
    )
    assert secret_access["Resource"] == [
        (
            "arn:aws:secretsmanager:us-east-1:123456789012:"
            "secret:axonllm/launch/runtime-identity-*"
        )
    ]


def test_bootstrap_command_uses_only_dedicated_policy_parts(tmp_path):
    cdk = tmp_path / "cdk"
    arns = tuple(
        policy.execution_policy_arn(
            partition=PARTITION,
            account_id=ACCOUNT_ID,
            region=REGION,
            part=part,
        )
        for part in range(1, policy.EXECUTION_POLICY_PART_COUNT + 1)
    )
    command = bootstrap.cdk_bootstrap_command(
        cdk_cli=cdk,
        identity=bootstrap.AwsIdentity(
            account_id=ACCOUNT_ID,
            partition=PARTITION,
        ),
        region=REGION,
        execution_policy_arns=arns,
    )
    assert command[0] == str(cdk)
    assert command[1:3] == [
        "bootstrap",
        f"aws://{ACCOUNT_ID}/{REGION}",
    ]
    attached = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--cloudformation-execution-policies"
    ]
    assert attached == list(arns)
    assert command[command.index("--qualifier") + 1] == "axrel"
    assert command[command.index("--toolkit-stack-name") + 1] == (
        "AxonLLMToolkit-axrel"
    )
    assert command[command.index("--custom-permissions-boundary") + 1] == (
        "AxonLLMReleaseFoundationBootstrapBoundary-axrel-us-east-1"
    )
    assert "AdministratorAccess" not in " ".join(command)
    assert "hnb659fds" not in " ".join(command)


@pytest.mark.parametrize(
    ("part", "valid"),
    (
        (0, False),
        (1, True),
        (4, True),
        (5, True),
        (6, False),
        (True, False),
    ),
)
def test_execution_policy_part_names_are_bounded(part, valid):
    if not valid:
        with pytest.raises(ValueError):
            policy.execution_policy_name(REGION, part=part)
        return
    assert policy.execution_policy_name(REGION, part=part) == (
        "AxonLLMReleaseFoundationCloudFormationExecution-"
        f"axrel-us-east-1-part{part}"
    )


def test_install_requires_explicit_apply(capsys):
    assert bootstrap.main(["install"]) == 2
    assert "requires --apply" in capsys.readouterr().err


def test_verify_rejects_apply(capsys):
    assert bootstrap.main(["verify", "--apply"]) == 2
    assert "valid only with install" in capsys.readouterr().err


def test_policy_set_rejects_oversized_document_before_aws_calls(monkeypatch):
    class NoAwsCalls:
        def client(self, *_args, **_kwargs):
            raise AssertionError("policy validation must precede AWS calls")

    monkeypatch.setattr(
        bootstrap,
        "bootstrap_boundary_document",
        lambda **_kwargs: {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:ListAllMyBuckets",
                    "Resource": "x" * policy.IAM_MANAGED_POLICY_SIZE_LIMIT,
                }
            ],
        },
    )
    with pytest.raises(
        bootstrap.ReleaseFoundationBootstrapError,
        match="bootstrap-role boundary exceeds IAM's managed-policy size quota",
    ):
        bootstrap.ensure_policy_set(
            NoAwsCalls(),
            identity=bootstrap.AwsIdentity(
                account_id=ACCOUNT_ID,
                partition=PARTITION,
            ),
            region=REGION,
            apply=True,
        )


def test_compatibility_launcher_runs_outside_the_scripts_directory(
    tmp_path,
):
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "operations"
        / "bootstrap_release_foundation.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "bounded AxonLLM release-foundation" in completed.stdout


class _BootstrapIam:
    def __init__(self):
        boundary = policy.bootstrap_boundary_arn(
            partition=PARTITION,
            account_id=ACCOUNT_ID,
            region=REGION,
        )
        self.roles = {}
        self.inline = {}
        self.attached = {}
        self.tags = {}
        for purpose in bootstrap._BOOTSTRAP_ROLE_PURPOSES:
            name = (
                f"cdk-axrel-{purpose}-role-{ACCOUNT_ID}-{REGION}"
            )
            if purpose == "cfn-exec":
                trust = {
                    "Statement": [
                        {
                            "Action": "sts:AssumeRole",
                            "Effect": "Allow",
                            "Principal": {
                                "Service": "cloudformation.amazonaws.com"
                            },
                        }
                    ]
                }
            else:
                principal = f"arn:aws:iam::{ACCOUNT_ID}:root"
                trust = {
                    "Statement": [
                        {
                            "Action": "sts:AssumeRole",
                            "Condition": {
                                "Null": {"sts:ExternalId": "true"}
                            },
                            "Effect": "Allow",
                            "Principal": {"AWS": principal},
                        },
                        {
                            "Action": "sts:TagSession",
                            "Effect": "Allow",
                            "Principal": {"AWS": principal},
                        },
                    ]
                }
            role = {
                "RoleName": name,
                "AssumeRolePolicyDocument": trust,
            }
            if purpose in {"cfn-exec", "deploy"}:
                role["PermissionsBoundary"] = {
                    "PermissionsBoundaryArn": boundary,
                    "PermissionsBoundaryType": "Policy",
                }
            self.roles[name] = role
            self.inline[name] = {
                "cfn-exec": set(),
                "deploy": {"default"},
                "file-publishing": {
                    (
                        "cdk-axrel-file-publishing-role-default-policy-"
                        f"{ACCOUNT_ID}-{REGION}"
                    )
                },
                "image-publishing": {
                    (
                        "cdk-axrel-image-publishing-role-default-policy-"
                        f"{ACCOUNT_ID}-{REGION}"
                    )
                },
                "lookup": {"LookupRolePolicy"},
            }[purpose]
            self.attached[name] = {
                "cfn-exec": {
                    policy.execution_policy_arn(
                        partition=PARTITION,
                        account_id=ACCOUNT_ID,
                        region=REGION,
                        part=part,
                    )
                    for part in range(
                        1,
                        policy.EXECUTION_POLICY_PART_COUNT + 1,
                    )
                },
                "deploy": {
                    (
                        "arn:aws:iam::aws:policy/"
                        "AWSCloudFormationReadOnlyAccess"
                    )
                },
                "file-publishing": set(),
                "image-publishing": set(),
                "lookup": {
                    "arn:aws:iam::aws:policy/ReadOnlyAccess"
                },
            }[purpose]
            self.tags[name] = (
                {}
                if purpose == "cfn-exec"
                else {"aws-cdk:bootstrap-role": purpose}
            )

    def get_role(self, *, RoleName):
        return {"Role": self.roles[RoleName]}

    def list_role_policies(self, *, RoleName, **_kwargs):
        return {"PolicyNames": sorted(self.inline[RoleName])}

    def list_attached_role_policies(self, *, RoleName, **_kwargs):
        return {
            "AttachedPolicies": [
                {"PolicyArn": value}
                for value in sorted(self.attached[RoleName])
            ]
        }

    def list_role_tags(self, *, RoleName, **_kwargs):
        return {
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in sorted(self.tags[RoleName].items())
            ]
        }


class _BootstrapCloudFormation:
    def describe_stacks(self, *, StackName):
        assert StackName == policy.TOOLKIT_STACK_NAME
        return {
            "Stacks": [
                {
                    "EnableTerminationProtection": True,
                    "StackStatus": "UPDATE_COMPLETE",
                }
            ]
        }


class _BootstrapSsm:
    def get_parameter(self, *, Name):
        return {
            "Parameter": {
                "Name": Name,
                "Value": bootstrap._BOOTSTRAP_TEMPLATE_VERSION,
            }
        }


class _BootstrapSession:
    def __init__(self):
        self.iam = _BootstrapIam()

    def client(self, service, *, region_name):
        assert region_name == REGION
        return {
            "iam": self.iam,
            "cloudformation": _BootstrapCloudFormation(),
            "ssm": _BootstrapSsm(),
        }[service]


def _execution_policy_arns():
    return tuple(
        policy.execution_policy_arn(
            partition=PARTITION,
            account_id=ACCOUNT_ID,
            region=REGION,
            part=part,
        )
        for part in range(
            1,
            policy.EXECUTION_POLICY_PART_COUNT + 1,
        )
    )


def test_bootstrap_verifier_accepts_pinned_cdk_role_contract():
    bootstrap.verify_bootstrap(
        _BootstrapSession(),
        identity=bootstrap.AwsIdentity(
            account_id=ACCOUNT_ID,
            partition=PARTITION,
        ),
        region=REGION,
        expected_policy_arns=_execution_policy_arns(),
    )


def test_bootstrap_verifier_rejects_extra_deploy_role_authority():
    session = _BootstrapSession()
    name = f"cdk-axrel-deploy-role-{ACCOUNT_ID}-{REGION}"
    session.iam.attached[name].add(
        "arn:aws:iam::aws:policy/AdministratorAccess"
    )
    with pytest.raises(
        bootstrap.ReleaseFoundationBootstrapError,
        match="unexpected authority",
    ):
        bootstrap.verify_bootstrap(
            session,
            identity=bootstrap.AwsIdentity(
                account_id=ACCOUNT_ID,
                partition=PARTITION,
            ),
            region=REGION,
            expected_policy_arns=_execution_policy_arns(),
        )


class _PolicyVersionsIam:
    def __init__(self):
        self.deleted = []
        self.calls = 0

    def list_policy_versions(self, *, PolicyArn):
        assert PolicyArn == "policy"
        self.calls += 1
        if self.calls == 1:
            return {
                "Versions": [
                    {"VersionId": "v1", "IsDefaultVersion": True},
                    {"VersionId": "v2", "IsDefaultVersion": False},
                    {"VersionId": "v3", "IsDefaultVersion": False},
                    {"VersionId": "v4", "IsDefaultVersion": False},
                    {"VersionId": "v5", "IsDefaultVersion": False},
                ]
            }
        return {
            "Versions": [
                {"VersionId": "v1", "IsDefaultVersion": False},
                {"VersionId": "v3", "IsDefaultVersion": False},
                {"VersionId": "v4", "IsDefaultVersion": False},
                {"VersionId": "v5", "IsDefaultVersion": False},
                {"VersionId": "v6", "IsDefaultVersion": True},
            ]
        }

    def delete_policy_version(self, *, PolicyArn, VersionId):
        assert PolicyArn == "policy"
        self.deleted.append(VersionId)

    def create_policy_version(self, **kwargs):
        assert kwargs["PolicyArn"] == "policy"
        assert kwargs["SetAsDefault"] is True
        return {"PolicyVersion": {"VersionId": "v6"}}


def test_policy_replacement_retains_the_previous_default_for_rollback():
    iam = _PolicyVersionsIam()
    bootstrap._replace_policy_version(
        iam,
        policy_arn="policy",
        document={"Version": "2012-10-17", "Statement": []},
    )
    assert iam.deleted == ["v2", "v3", "v4", "v5"]
    assert "v1" not in iam.deleted
