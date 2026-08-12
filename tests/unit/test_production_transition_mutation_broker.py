from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
from io import BytesIO
import hashlib
import json
from typing import Any

import pytest

from src.gateway.deployment import production_transition_mutation_broker as broker


NOW = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
REGION = "us-east-1"
REPOSITORY = "owner/repo"
RUN_ID = "42"
RUN_ATTEMPT = "1"
COMMIT = "a" * 40
TRANSITION_ID = "b" * 64
KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
ROLE_ARN = "arn:aws:iam::123456789012:role/cdk-axprod-cfn-exec-role-123456789012-us-east-1"
RUNTIME_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/runtime@sha256:{'1' * 64}"
CONTROL_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control@sha256:{'2' * 64}"
OLD_CONTROL_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control@sha256:{'3' * 64}"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm-runtime"
AGENTCORE_STACK = "AxonLLMAgentCoreStack"
CONTROL_STACK = "AxonLLMControlPlaneStack"
AGENTCORE_STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/AxonLLMAgentCoreStack/runtime-stack-id"
CONTROL_STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/AxonLLMControlPlaneStack/control-stack-id"
BASE = f"transitions/{REPOSITORY}/{RUN_ID}/{RUN_ATTEMPT}"


def _environment() -> dict[str, str]:
    return {
        broker.EVIDENCE_BUCKET_ENV: "axonllm-evidence",
        broker.EVIDENCE_PREFIX_ENV: "transitions",
        broker.SIGNING_KEY_ARN_ENV: KEY_ARN,
        broker.AGENTCORE_STACK_NAME_ENV: AGENTCORE_STACK,
        broker.CONTROL_PLANE_STACK_NAME_ENV: CONTROL_STACK,
        broker.EXECUTION_ROLE_ARN_ENV: ROLE_ARN,
        broker.AWS_REGION_ENV: REGION,
    }


def _dump(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _signature_bundle(artifact: bytes, *, key_arn: str = KEY_ARN) -> bytes:
    return _dump(
        {
            "schema": broker.KMS_BUNDLE_SCHEMA,
            "artifact": {
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "size": len(artifact),
            },
            "signature": {
                "keyArn": key_arn,
                "messageType": broker.KMS_MESSAGE_TYPE,
                "signingAlgorithm": broker.KMS_SIGNING_ALGORITHM,
                "value": base64.b64encode(b"fixture-signature").decode(),
            },
        }
    )


class AwsError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        delete_marker: bool = False,
    ) -> None:
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPHeaders": {"x-amz-delete-marker": ("true" if delete_marker else "false")}},
        }


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.gets: list[dict[str, Any]] = []
        self.lists: list[dict[str, Any]] = []
        self.response_overrides: dict[str, dict[str, Any]] = {}
        self.commit_versions: list[dict[str, Any]] = []
        self.commit_markers: list[dict[str, Any]] = []
        self._sequence = 0

    def add(self, key: str, raw: bytes) -> str:
        self._sequence += 1
        version = f"version-{self._sequence}-{key.rsplit('/', 1)[-1]}"
        self.objects[(key, version)] = raw
        return version

    def add_pair(
        self,
        artifact_name: str,
        signature_name: str,
        raw: bytes,
    ) -> tuple[str, str]:
        artifact_version = self.add(f"{BASE}/{artifact_name}", raw)
        signature_version = self.add(
            f"{BASE}/{signature_name}",
            _signature_bundle(raw),
        )
        return artifact_version, signature_version

    def replace(
        self,
        key: str,
        version: str,
        raw: bytes,
    ) -> None:
        self.objects[(key, version)] = raw

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.gets.append(kwargs)
        key = kwargs["Key"]
        version = kwargs["VersionId"]
        raw = self.objects[(key, version)]
        response = {
            "Key": key,
            "VersionId": version,
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": NOW + timedelta(days=30),
            "ContentLength": len(raw),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(raw).digest()).decode(),
            "Body": BytesIO(raw),
            "ResponseMetadata": {"HTTPHeaders": {"x-amz-version-id": version}},
        }
        response.update(self.response_overrides.get(key, {}))
        return response

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        self.lists.append(kwargs)
        return {
            "Versions": deepcopy(self.commit_versions),
            "DeleteMarkers": deepcopy(self.commit_markers),
            "IsTruncated": False,
        }


class FakeKms:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.valid = True

    def verify(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "KeyId": KEY_ARN,
            "SigningAlgorithm": broker.KMS_SIGNING_ALGORITHM,
            "SignatureValid": self.valid,
        }


def _parameters(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"ParameterKey": name, "ParameterValue": value} for name, value in sorted(values.items())]


def _outputs(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"OutputKey": name, "OutputValue": value} for name, value in sorted(values.items())]


def _agent_parameters() -> dict[str, str]:
    return {
        "VerifiedImageUri": RUNTIME_IMAGE,
        "CandidateEndpointName": "candidate_" + "c" * 32,
        "ProviderSecretVersion": "provider-version-1",
        "EnabledProviders": "openai,anthropic",
        "PublishCandidateEndpoint": "true",
        "PublishProductionEndpoint": "true",
        "ProductionRuntimeVersion": "7",
        "UnrelatedReviewedParameter": "preserved",
    }


def _agent_outputs(*, finalized: bool = False, rolled_back: bool = False) -> dict[str, str]:
    values = {
        "RuntimeImageUri": RUNTIME_IMAGE,
        "RuntimeArn": RUNTIME_ARN,
        "RuntimeVersion": "7",
        "ProviderSecretVersion": "provider-version-1",
        "EnabledProviders": "openai,anthropic",
        "RecoveryCutoverMode": "normal",
        "AlarmNotificationEmail": "alerts@example.com",
        "ApprovedHttpsPrefixListId": "pl-1234",
        "AthenaConfigurationFingerprint": "d" * 64,
        "BedrockInvokeResourceArns": ("arn:aws:bedrock:us-east-1::foundation-model/model"),
    }
    if not finalized and not rolled_back:
        values.update(
            {
                "CandidateRuntimeEndpointArn": (f"{RUNTIME_ARN}/runtime-endpoint/candidate_{'c' * 32}"),
                "CandidateRuntimeEndpointName": ("candidate_" + "c" * 32),
                "CandidateRuntimeVersion": "7",
            }
        )
    if not rolled_back:
        values.update(
            {
                "RuntimeEndpointArn": (f"{RUNTIME_ARN}/runtime-endpoint/production"),
                "RuntimeEndpointName": "production",
                "ProductionRuntimeVersion": "7",
            }
        )
    else:
        values.update(
            {
                "RuntimeEndpointArn": (f"{RUNTIME_ARN}/runtime-endpoint/production"),
                "RuntimeEndpointName": "production",
                "ProductionRuntimeVersion": "6",
            }
        )
    return values


def _previous_control_parameters() -> dict[str, str]:
    return {
        "AgentCoreStackName": AGENTCORE_STACK,
        "ControlPlaneVerifiedImageUri": OLD_CONTROL_IMAGE,
        "DeploymentTransitionId": "0" * 64,
        "ReviewedSetting": "before",
    }


def _target_control_parameters() -> dict[str, str]:
    return {
        "AgentCoreStackName": AGENTCORE_STACK,
        "ControlPlaneVerifiedImageUri": CONTROL_IMAGE,
        "DeploymentTransitionId": TRANSITION_ID,
        "ReviewedSetting": "after",
    }


def _control_outputs() -> dict[str, str]:
    return {
        "AgentCoreStackName": AGENTCORE_STACK,
        "ControlPlaneImageUri": CONTROL_IMAGE,
        "DeploymentTransitionId": TRANSITION_ID,
    }


def _restored_control_outputs() -> dict[str, str]:
    return {
        "AgentCoreStackName": AGENTCORE_STACK,
        "ControlPlaneImageUri": OLD_CONTROL_IMAGE,
        "DeploymentTransitionId": "0" * 64,
    }


class FakeCloudFormation:
    def __init__(self, *, stack_existed: bool = True) -> None:
        self.stacks: dict[str, dict[str, Any]] = {
            AGENTCORE_STACK: {
                "StackName": AGENTCORE_STACK,
                "StackId": AGENTCORE_STACK_ID,
                "RoleARN": ROLE_ARN,
                "StackStatus": "UPDATE_COMPLETE",
                "Parameters": _parameters(_agent_parameters()),
                "Outputs": _outputs(_agent_outputs()),
            },
            CONTROL_STACK: {
                "StackName": CONTROL_STACK,
                "StackId": CONTROL_STACK_ID,
                "RoleARN": ROLE_ARN,
                "StackStatus": "UPDATE_COMPLETE",
                "Parameters": _parameters(_target_control_parameters()),
                "Outputs": _outputs(_control_outputs()),
            },
        }
        if not stack_existed:
            self.stacks[CONTROL_STACK]["StackId"] = (
                "arn:aws:cloudformation:us-east-1:123456789012:stack/AxonLLMControlPlaneStack/first-launch-id"
            )
        self.updates: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.resource_calls: list[dict[str, Any]] = []
        self.load_balancer = (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/axonllm/1234567890abcdef"
        )

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        stack = self.stacks.get(kwargs["StackName"])
        if stack is None:
            raise AwsError(
                "ValidationError",
                f"Stack with id {kwargs['StackName']} does not exist",
            )
        return {"Stacks": [deepcopy(stack)]}

    def update_stack(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(deepcopy(kwargs))
        return {"StackId": self.stacks[kwargs["StackName"]]["StackId"]}

    def delete_stack(self, **kwargs: Any) -> None:
        self.deletes.append(deepcopy(kwargs))

    def list_stack_resources(self, **kwargs: Any) -> dict[str, Any]:
        self.resource_calls.append(deepcopy(kwargs))
        resources = []
        if self.load_balancer is not None:
            resources.append(
                {
                    "ResourceType": ("AWS::ElasticLoadBalancingV2::LoadBalancer"),
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": self.load_balancer,
                }
            )
        return {"StackResourceSummaries": resources}


class FakeElbv2:
    def __init__(self) -> None:
        self.protection_enabled = True
        self.describes: list[dict[str, Any]] = []
        self.modifies: list[dict[str, Any]] = []

    def describe_load_balancer_attributes(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.describes.append(deepcopy(kwargs))
        return {
            "Attributes": [
                {
                    "Key": "deletion_protection.enabled",
                    "Value": ("true" if self.protection_enabled else "false"),
                }
            ]
        }

    def modify_load_balancer_attributes(
        self,
        **kwargs: Any,
    ) -> None:
        self.modifies.append(deepcopy(kwargs))


def _intent(*, stack_existed: bool, rollback_at: datetime) -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "candidateEndpointName": "candidate_" + "c" * 32,
        "candidateRuntimeVersion": "7",
        "previousProductionRuntimeVersion": "6",
        "productionEndpointArn": (f"{RUNTIME_ARN}/runtime-endpoint/production"),
        "productionRuntimeVersion": "7",
        "providerSecretVersion": "provider-version-1",
        "runtimeArn": RUNTIME_ARN,
        "enabledProviders": "openai,anthropic",
        "region": REGION,
        "sharedRuntimeConfiguration": {
            "AlarmNotificationEmail": "alerts@example.com",
            "ApprovedHttpsPrefixListId": "pl-1234",
            "AthenaConfigurationFingerprint": "d" * 64,
            "BedrockInvokeResourceArns": ("arn:aws:bedrock:us-east-1::foundation-model/model"),
        },
        "controlPlane": {
            "previousParameters": (_previous_control_parameters() if stack_existed else None),
            "previousStackId": (CONTROL_STACK_ID if stack_existed else None),
            "stackExisted": stack_existed,
            "targetImage": CONTROL_IMAGE,
        },
        "transition": {
            "changeId": "CHG-2026-001",
            "deploymentCommit": COMMIT,
            "repository": REPOSITORY,
            "rollbackNotBefore": rollback_at.isoformat(timespec="seconds"),
            "runAttempt": RUN_ATTEMPT,
            "runId": RUN_ID,
            "transitionId": TRANSITION_ID,
        },
    }


def _setup() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "identity_mode": "managed-cognito",
        "aws_region": REGION,
        "runtime": {
            "verified_image_uri": RUNTIME_IMAGE,
            "enabled_providers": ["openai", "anthropic"],
        },
        "control_plane": {
            "verified_image_uri": CONTROL_IMAGE,
            "domain_name": "axon.example.com",
        },
    }


def _deployment_evidence(
    *,
    intent: dict[str, Any],
    setup_raw: bytes,
) -> dict[str, Any]:
    runtime = _agent_outputs()
    runtime["AgentCoreStackName"] = AGENTCORE_STACK
    return {
        "schema": broker.DEPLOYMENT_EVIDENCE_SCHEMA,
        "deployment": {
            "operation": "deploy",
            "changeId": "CHG-2026-001",
            "environment": "production",
            "repository": REPOSITORY,
            "commit": COMMIT,
            "workflowRef": (f"{REPOSITORY}/.github/workflows/deploy-agentcore-production.yml@refs/heads/main"),
            "workflowCommit": COMMIT,
            "parentWorkflowRef": (f"{REPOSITORY}/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
            "parentWorkflowCommit": COMMIT,
            "runId": RUN_ID,
            "runAttempt": RUN_ATTEMPT,
            "actor": "release-admin",
            "actorId": "1234",
            "triggeringActor": "release-admin",
            "generatedAt": NOW.isoformat(timespec="seconds"),
            "awsAccountId": ACCOUNT,
            "awsRegion": REGION,
        },
        "release": {"commit": COMMIT},
        "images": {
            "agentcore": {
                "reference": RUNTIME_IMAGE,
                "digest": RUNTIME_IMAGE.rsplit("@", 1)[1],
            },
            "controlPlane": {
                "reference": CONTROL_IMAGE,
                "digest": CONTROL_IMAGE.rsplit("@", 1)[1],
            },
        },
        "configuration": {"setupSha256": hashlib.sha256(setup_raw).hexdigest()},
        "stacks": {
            "identity": {},
            "runtime": runtime,
            "controlPlane": _control_outputs(),
        },
        "providerSecret": {},
        "recovery": {},
        "certification": {},
        "productionCertification": {},
        "productionValidation": {},
        "launchRehearsalSource": {},
        "launchRehearsal": {},
        "externalOidcCertificationSource": {},
        "externalOidcCertification": {},
        "qualificationTeardownSource": {},
        "qualificationTeardown": {},
    }


def _deployment_commit(
    evidence_raw: bytes,
    evidence_signature: bytes,
) -> dict[str, Any]:
    return {
        "schema": broker.DEPLOYMENT_COMMIT_SCHEMA,
        "deployment": {
            "repository": REPOSITORY,
            "commit": COMMIT,
            "runId": RUN_ID,
            "runAttempt": RUN_ATTEMPT,
        },
        "release": {"commit": COMMIT},
        "images": {
            "agentcore": RUNTIME_IMAGE,
            "controlPlane": CONTROL_IMAGE,
        },
        "artifacts": {
            "evidence": {
                "name": "agentcore-deployment.json",
                "sha256": hashlib.sha256(evidence_raw).hexdigest(),
            },
            "signature": {
                "name": ("agentcore-deployment-kms-signature.json"),
                "sha256": hashlib.sha256(evidence_signature).hexdigest(),
            },
        },
    }


class Fixture:
    def __init__(
        self,
        *,
        with_commit: bool,
        stack_existed: bool = True,
        rollback_at: datetime | None = None,
    ) -> None:
        self.s3 = FakeS3()
        self.kms = FakeKms()
        self.cf = FakeCloudFormation(stack_existed=stack_existed)
        self.elb = FakeElbv2()
        self.clients = broker.BrokerClients(
            s3=self.s3,
            kms=self.kms,
            cloudformation=self.cf,
            elbv2=self.elb,
        )
        self.intent = _intent(
            stack_existed=stack_existed,
            rollback_at=rollback_at or NOW - timedelta(hours=1),
        )
        self.intent_raw = _dump(self.intent)
        self.setup = _setup()
        self.setup_raw = _dump(self.setup)
        self.binding = {
            "schema": broker.RECOVERY_BINDING_SCHEMA,
            "intentSha256": hashlib.sha256(self.intent_raw).hexdigest(),
            "setupConfigSha256": hashlib.sha256(self.setup_raw).hexdigest(),
            "repository": REPOSITORY,
            "runId": RUN_ID,
            "runAttempt": RUN_ATTEMPT,
            "recordedAt": (NOW - timedelta(hours=4)).isoformat(timespec="seconds"),
        }
        self.event: dict[str, str] = {
            "repository": REPOSITORY,
            "runId": RUN_ID,
            "runAttempt": RUN_ATTEMPT,
        }
        self._add_pair(
            "promotion.json",
            "promotion-kms-signature.json",
            self.intent_raw,
            "intentVersionId",
            "intentSignatureVersionId",
        )
        self._add_pair(
            "transition-recovery-setup.json",
            "transition-recovery-setup-kms-signature.json",
            self.setup_raw,
            "recoverySetupVersionId",
            "recoverySetupSignatureVersionId",
        )
        self._add_pair(
            "transition-recovery-binding.json",
            "transition-recovery-binding-kms-signature.json",
            _dump(self.binding),
            "recoveryBindingVersionId",
            "recoveryBindingSignatureVersionId",
        )
        if with_commit:
            evidence_raw = _dump(
                _deployment_evidence(
                    intent=self.intent,
                    setup_raw=self.setup_raw,
                )
            )
            evidence_version = self.s3.add(
                f"{BASE}/agentcore-deployment.json",
                evidence_raw,
            )
            evidence_signature = _signature_bundle(evidence_raw)
            evidence_signature_version = self.s3.add(
                f"{BASE}/agentcore-deployment-kms-signature.json",
                evidence_signature,
            )
            commit_raw = _dump(
                _deployment_commit(
                    evidence_raw,
                    evidence_signature,
                )
            )
            commit_version, commit_signature_version = self.s3.add_pair(
                "agentcore-deployment-commit.json",
                ("agentcore-deployment-commit-kms-signature.json"),
                commit_raw,
            )
            self.event.update(
                {
                    "deploymentEvidenceVersionId": evidence_version,
                    "deploymentEvidenceSignatureVersionId": (evidence_signature_version),
                    "deploymentCommitVersionId": commit_version,
                    "deploymentCommitSignatureVersionId": (commit_signature_version),
                }
            )

    def _add_pair(
        self,
        artifact_name: str,
        signature_name: str,
        raw: bytes,
        artifact_field: str,
        signature_field: str,
    ) -> None:
        versions = self.s3.add_pair(
            artifact_name,
            signature_name,
            raw,
        )
        self.event[artifact_field] = versions[0]
        self.event[signature_field] = versions[1]

    def invoke(self) -> dict[str, str]:
        return broker.handle_event(
            self.event,
            clients=self.clients,
            environ=_environment(),
            now=NOW,
        )

    def replace_signed_json(
        self,
        artifact_name: str,
        signature_name: str,
        artifact_version_field: str,
        signature_version_field: str,
        value: dict[str, Any],
    ) -> None:
        raw = _dump(value)
        self.s3.replace(
            f"{BASE}/{artifact_name}",
            self.event[artifact_version_field],
            raw,
        )
        self.s3.replace(
            f"{BASE}/{signature_name}",
            self.event[signature_version_field],
            _signature_bundle(raw),
        )


def _request_parameter_values(request: dict[str, Any]) -> dict[str, Any]:
    return {
        item["ParameterKey"]: (item.get("ParameterValue") if "ParameterValue" in item else "USE_PREVIOUS")
        for item in request["Parameters"]
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stackName", "AttackerStack"),
        ("roleArn", "arn:aws:iam::123456789012:role/attacker"),
        ("parameters", {"PublishProductionEndpoint": "false"}),
        ("template", "https://attacker.invalid/template"),
    ],
)
def test_event_rejects_all_caller_supplied_mutation_targets(
    field: str,
    value: Any,
) -> None:
    fixture = Fixture(with_commit=False)
    fixture.event[field] = value

    with pytest.raises(
        broker.MutationBrokerError,
        match="strict schema",
    ):
        fixture.invoke()

    assert fixture.s3.gets == []
    assert fixture.cf.updates == []
    assert fixture.cf.deletes == []


def test_event_rejects_partial_commit_versions() -> None:
    fixture = Fixture(with_commit=False)
    fixture.event["deploymentCommitVersionId"] = "some-version"

    with pytest.raises(
        broker.MutationBrokerError,
        match="strict schema",
    ):
        fixture.invoke()


def test_fetches_only_derived_keys_at_exact_versions() -> None:
    fixture = Fixture(
        with_commit=False,
        rollback_at=NOW + timedelta(hours=1),
    )

    result = fixture.invoke()

    assert result["phase"] == "ROLLBACK_NOT_BEFORE"
    assert [call["Key"] for call in fixture.s3.gets] == [
        f"{BASE}/promotion.json",
        f"{BASE}/promotion-kms-signature.json",
        f"{BASE}/transition-recovery-setup.json",
        (f"{BASE}/transition-recovery-setup-kms-signature.json"),
        f"{BASE}/transition-recovery-binding.json",
        (f"{BASE}/transition-recovery-binding-kms-signature.json"),
    ]
    assert all(
        call["VersionId"]
        == fixture.event[
            {
                "promotion.json": "intentVersionId",
                "promotion-kms-signature.json": ("intentSignatureVersionId"),
                "transition-recovery-setup.json": ("recoverySetupVersionId"),
                "transition-recovery-setup-kms-signature.json": ("recoverySetupSignatureVersionId"),
                "transition-recovery-binding.json": ("recoveryBindingVersionId"),
                "transition-recovery-binding-kms-signature.json": ("recoveryBindingSignatureVersionId"),
            }[call["Key"].rsplit("/", 1)[1]]
        ]
        for call in fixture.s3.gets
    )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"DeleteMarker": True}, "delete marker"),
        ({"Key": f"{BASE}/other.json"}, "version or immutable"),
        ({"VersionId": "substituted"}, "version or immutable"),
        (
            {"ChecksumSHA256": base64.b64encode(b"x" * 32).decode()},
            "SHA-256",
        ),
    ],
)
def test_rejects_delete_marker_key_version_and_checksum_substitution(
    override: dict[str, Any],
    message: str,
) -> None:
    fixture = Fixture(with_commit=False)
    fixture.s3.response_overrides[f"{BASE}/promotion.json"] = override

    with pytest.raises(broker.MutationBrokerError, match=message):
        fixture.invoke()

    assert fixture.cf.updates == []
    assert fixture.elb.modifies == []


def test_rejects_artifact_tampering_even_with_valid_s3_checksum() -> None:
    fixture = Fixture(with_commit=False)
    key = f"{BASE}/promotion.json"
    fixture.s3.replace(
        key,
        fixture.event["intentVersionId"],
        fixture.intent_raw + b" ",
    )

    with pytest.raises(
        broker.MutationBrokerError,
        match="does not match its signature bundle",
    ):
        fixture.invoke()


def test_rejects_wrong_bundle_key_and_negative_kms_verdict() -> None:
    fixture = Fixture(with_commit=False)
    signature_key = f"{BASE}/promotion-kms-signature.json"
    fixture.s3.replace(
        signature_key,
        fixture.event["intentSignatureVersionId"],
        _signature_bundle(
            fixture.intent_raw,
            key_arn=("arn:aws:kms:us-east-1:123456789012:key/22222222-2222-2222-2222-222222222222"),
        ),
    )

    with pytest.raises(
        broker.MutationBrokerError,
        match="bundle binding",
    ):
        fixture.invoke()

    fixture = Fixture(with_commit=False)
    fixture.kms.valid = False
    with pytest.raises(broker.MutationBrokerError, match="KMS rejected"):
        fixture.invoke()


def test_rejects_recovery_binding_for_different_setup() -> None:
    fixture = Fixture(with_commit=False)
    binding = deepcopy(fixture.binding)
    binding["setupConfigSha256"] = "f" * 64
    fixture.replace_signed_json(
        "transition-recovery-binding.json",
        "transition-recovery-binding-kms-signature.json",
        "recoveryBindingVersionId",
        "recoveryBindingSignatureVersionId",
        binding,
    )

    with pytest.raises(
        broker.MutationBrokerError,
        match="does not match exact signed artifacts",
    ):
        fixture.invoke()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "other/repo"),
        ("runId", "999"),
        ("runAttempt", "2"),
    ],
)
def test_rejects_intent_identity_substitution(
    field: str,
    value: str,
) -> None:
    fixture = Fixture(with_commit=False)
    intent = deepcopy(fixture.intent)
    intent["transition"][field] = value
    fixture.replace_signed_json(
        "promotion.json",
        "promotion-kms-signature.json",
        "intentVersionId",
        "intentSignatureVersionId",
        intent,
    )

    with pytest.raises(
        broker.MutationBrokerError,
        match="transition identity",
    ):
        fixture.invoke()


def test_rejects_setup_image_that_differs_from_intent() -> None:
    fixture = Fixture(with_commit=False)
    setup = deepcopy(fixture.setup)
    setup["control_plane"]["verified_image_uri"] = OLD_CONTROL_IMAGE
    fixture.replace_signed_json(
        "transition-recovery-setup.json",
        "transition-recovery-setup-kms-signature.json",
        "recoverySetupVersionId",
        "recoverySetupSignatureVersionId",
        setup,
    )

    with pytest.raises(
        broker.MutationBrokerError,
        match="images or providers",
    ):
        fixture.invoke()


def test_rollback_waits_for_signed_not_before_without_mutating() -> None:
    fixture = Fixture(
        with_commit=False,
        rollback_at=NOW + timedelta(seconds=1),
    )

    result = fixture.invoke()

    assert result == {
        "status": "PENDING",
        "operation": "ROLLBACK",
        "phase": "ROLLBACK_NOT_BEFORE",
        "transitionId": TRANSITION_ID,
    }
    assert fixture.cf.updates == []
    assert fixture.cf.deletes == []
    assert fixture.elb.modifies == []


def test_omitted_existing_commit_signal_blocks_rollback() -> None:
    fixture = Fixture(with_commit=False)
    fixture.s3.commit_versions = [
        {
            "Key": (f"{BASE}/agentcore-deployment-commit-kms-signature.json"),
            "VersionId": "hidden-commit-version",
            "IsLatest": True,
        }
    ]

    with pytest.raises(
        broker.MutationBrokerError,
        match="exact versions were omitted",
    ):
        fixture.invoke()

    assert fixture.cf.updates == []


def test_delete_marker_on_commit_signal_blocks_rollback() -> None:
    fixture = Fixture(with_commit=False)
    fixture.s3.commit_markers = [
        {
            "Key": (f"{BASE}/agentcore-deployment-commit-kms-signature.json"),
            "VersionId": "marker",
            "IsLatest": True,
        }
    ]

    with pytest.raises(
        broker.MutationBrokerError,
        match="delete marker",
    ):
        fixture.invoke()


def test_invalid_supplied_commit_never_falls_back_to_rollback() -> None:
    fixture = Fixture(with_commit=True)
    commit_key = f"{BASE}/agentcore-deployment-commit.json"
    raw = json.loads(fixture.s3.objects[(commit_key, fixture.event["deploymentCommitVersionId"])])
    raw["deployment"]["commit"] = "f" * 40
    fixture.replace_signed_json(
        "agentcore-deployment-commit.json",
        "agentcore-deployment-commit-kms-signature.json",
        "deploymentCommitVersionId",
        "deploymentCommitSignatureVersionId",
        raw,
    )

    with pytest.raises(
        broker.MutationBrokerError,
        match="does not bind exact evidence",
    ):
        fixture.invoke()

    assert fixture.cf.updates == []
    assert fixture.cf.deletes == []


def test_valid_commit_finalizes_with_fixed_stack_role_and_parameters() -> None:
    fixture = Fixture(with_commit=True)

    result = fixture.invoke()

    assert result["status"] == "PENDING"
    assert result["operation"] == "FINALIZE"
    assert result["phase"] == "RUNTIME_UPDATE"
    assert len(fixture.cf.updates) == 1
    request = fixture.cf.updates[0]
    assert request["StackName"] == AGENTCORE_STACK
    assert request["RoleARN"] == ROLE_ARN
    assert request["UsePreviousTemplate"] is True
    assert "TemplateURL" not in request
    assert "TemplateBody" not in request
    values = _request_parameter_values(request)
    assert values == {
        "CandidateEndpointName": "candidate_" + "c" * 32,
        "EnabledProviders": "USE_PREVIOUS",
        "ProductionRuntimeVersion": "7",
        "ProviderSecretVersion": "USE_PREVIOUS",
        "PublishCandidateEndpoint": "false",
        "PublishProductionEndpoint": "true",
        "UnrelatedReviewedParameter": "USE_PREVIOUS",
        "VerifiedImageUri": "USE_PREVIOUS",
    }
    assert request["ClientRequestToken"] == broker._client_token(
        TRANSITION_ID,
        "runtime-finalize",
    )
    assert fixture.cf.deletes == []
    assert fixture.elb.modifies == []


def test_completed_finalize_is_idempotent_and_mutation_free() -> None:
    fixture = Fixture(with_commit=True)
    parameters = _agent_parameters()
    parameters.update(
        {
            "PublishCandidateEndpoint": "false",
            "PublishProductionEndpoint": "true",
            "ProductionRuntimeVersion": "7",
        }
    )
    fixture.cf.stacks[AGENTCORE_STACK]["Parameters"] = _parameters(parameters)
    fixture.cf.stacks[AGENTCORE_STACK]["Outputs"] = _outputs(_agent_outputs(finalized=True))

    first = fixture.invoke()
    second = fixture.invoke()

    assert first["status"] == "COMPLETE"
    assert second == first
    assert fixture.cf.updates == []
    assert fixture.cf.deletes == []


def test_commit_rejects_live_stack_role_and_image_substitution() -> None:
    fixture = Fixture(with_commit=True)
    fixture.cf.stacks[AGENTCORE_STACK]["RoleARN"] = "arn:aws:iam::123456789012:role/attacker"

    with pytest.raises(
        broker.MutationBrokerError,
        match="stack ownership",
    ):
        fixture.invoke()

    fixture = Fixture(with_commit=True)
    parameters = _agent_parameters()
    parameters["VerifiedImageUri"] = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/runtime@sha256:{'9' * 64}"
    fixture.cf.stacks[AGENTCORE_STACK]["Parameters"] = _parameters(parameters)
    with pytest.raises(
        broker.MutationBrokerError,
        match="parameters do not match",
    ):
        fixture.invoke()


def test_rollback_restores_existing_control_plane_before_runtime() -> None:
    fixture = Fixture(with_commit=False, stack_existed=True)

    result = fixture.invoke()

    assert result["phase"] == "CONTROL_PLANE_RESTORE"
    assert len(fixture.cf.updates) == 1
    request = fixture.cf.updates[0]
    assert request["StackName"] == CONTROL_STACK
    assert request["RoleARN"] == ROLE_ARN
    assert _request_parameter_values(request) == (_previous_control_parameters())
    assert request["ClientRequestToken"] == broker._client_token(
        TRANSITION_ID,
        "control-plane-restore",
    )
    assert all(update["StackName"] != AGENTCORE_STACK for update in fixture.cf.updates)


def test_runtime_rollback_starts_only_after_control_is_restored() -> None:
    fixture = Fixture(with_commit=False, stack_existed=True)
    fixture.cf.stacks[CONTROL_STACK]["Parameters"] = _parameters(_previous_control_parameters())
    fixture.cf.stacks[CONTROL_STACK]["Outputs"] = _outputs(_restored_control_outputs())

    result = fixture.invoke()

    assert result["phase"] == "RUNTIME_UPDATE"
    assert len(fixture.cf.updates) == 1
    request = fixture.cf.updates[0]
    assert request["StackName"] == AGENTCORE_STACK
    values = _request_parameter_values(request)
    assert values["ProductionRuntimeVersion"] == "6"
    assert values["PublishCandidateEndpoint"] == "false"
    assert values["PublishProductionEndpoint"] == "true"
    assert request["ClientRequestToken"] == broker._client_token(
        TRANSITION_ID,
        "runtime-rollback",
    )


def test_control_parameters_without_restored_outputs_block_runtime() -> None:
    fixture = Fixture(with_commit=False, stack_existed=True)
    fixture.cf.stacks[CONTROL_STACK]["Parameters"] = _parameters(_previous_control_parameters())

    with pytest.raises(
        broker.MutationBrokerError,
        match="outputs do not match prior signed parameters",
    ):
        fixture.invoke()

    assert fixture.cf.updates == []


def test_runtime_rollback_requires_exact_promoted_candidate_endpoint() -> None:
    fixture = Fixture(with_commit=False, stack_existed=True)
    fixture.cf.stacks[CONTROL_STACK]["Parameters"] = _parameters(_previous_control_parameters())
    fixture.cf.stacks[CONTROL_STACK]["Outputs"] = _outputs(_restored_control_outputs())
    fixture.cf.stacks[AGENTCORE_STACK]["Outputs"] = _outputs(_agent_outputs(finalized=True))

    with pytest.raises(
        broker.MutationBrokerError,
        match="exact promoted candidate transition",
    ):
        fixture.invoke()

    assert fixture.cf.updates == []


def test_first_launch_disables_alb_protection_as_only_mutation() -> None:
    fixture = Fixture(with_commit=False, stack_existed=False)

    result = fixture.invoke()

    assert result["phase"] == "CONTROL_PLANE_DELETE_PROTECTION"
    assert len(fixture.elb.modifies) == 1
    assert fixture.elb.modifies[0] == {
        "LoadBalancerArn": fixture.cf.load_balancer,
        "Attributes": [
            {
                "Key": "deletion_protection.enabled",
                "Value": "false",
            }
        ],
    }
    assert fixture.cf.deletes == []
    assert fixture.cf.updates == []


def test_first_launch_deletes_only_after_protection_is_disabled() -> None:
    fixture = Fixture(with_commit=False, stack_existed=False)
    fixture.elb.protection_enabled = False

    result = fixture.invoke()

    assert result["phase"] == "CONTROL_PLANE_DELETE"
    assert fixture.elb.modifies == []
    assert len(fixture.cf.deletes) == 1
    assert fixture.cf.deletes[0] == {
        "StackName": CONTROL_STACK,
        "RoleARN": ROLE_ARN,
        "ClientRequestToken": broker._client_token(
            TRANSITION_ID,
            "control-plane-delete",
        ),
    }
    assert fixture.cf.updates == []


def test_first_launch_deletion_requires_transition_ownership() -> None:
    fixture = Fixture(with_commit=False, stack_existed=False)
    parameters = _target_control_parameters()
    parameters["DeploymentTransitionId"] = "f" * 64
    fixture.cf.stacks[CONTROL_STACK]["Parameters"] = _parameters(parameters)

    with pytest.raises(
        broker.MutationBrokerError,
        match="not owned by signed transition",
    ):
        fixture.invoke()

    assert fixture.cf.deletes == []
    assert fixture.elb.modifies == []


def test_first_launch_runtime_rollback_waits_until_control_is_absent() -> None:
    fixture = Fixture(with_commit=False, stack_existed=False)
    del fixture.cf.stacks[CONTROL_STACK]

    result = fixture.invoke()

    assert result["phase"] == "RUNTIME_UPDATE"
    assert len(fixture.cf.updates) == 1
    assert fixture.cf.updates[0]["StackName"] == AGENTCORE_STACK


def test_in_progress_control_plane_is_polled_without_mutation() -> None:
    fixture = Fixture(with_commit=False, stack_existed=True)
    fixture.cf.stacks[CONTROL_STACK]["StackStatus"] = "UPDATE_IN_PROGRESS"

    result = fixture.invoke()

    assert result["phase"] == "CONTROL_PLANE_WAIT"
    assert fixture.cf.updates == []
    assert fixture.cf.deletes == []
    assert fixture.elb.modifies == []


def test_completed_rollback_is_idempotent_and_mutation_free() -> None:
    fixture = Fixture(with_commit=False, stack_existed=True)
    fixture.cf.stacks[CONTROL_STACK]["Parameters"] = _parameters(_previous_control_parameters())
    fixture.cf.stacks[CONTROL_STACK]["Outputs"] = _outputs(_restored_control_outputs())
    parameters = _agent_parameters()
    parameters.update(
        {
            "PublishCandidateEndpoint": "false",
            "PublishProductionEndpoint": "true",
            "ProductionRuntimeVersion": "6",
        }
    )
    fixture.cf.stacks[AGENTCORE_STACK]["Parameters"] = _parameters(parameters)
    fixture.cf.stacks[AGENTCORE_STACK]["Outputs"] = _outputs(_agent_outputs(rolled_back=True))

    result = fixture.invoke()

    assert result["status"] == "COMPLETE"
    assert result["operation"] == "ROLLBACK"
    assert fixture.cf.updates == []
    assert fixture.cf.deletes == []
    assert fixture.elb.modifies == []
