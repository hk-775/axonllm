from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import io
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))

import kms_evidence  # noqa: E402
import launch_gate_receipts as publisher  # noqa: E402
import launch_rehearsal_evidence as evidence  # noqa: E402


ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "axonllm-evidence-prod"
PREFIX = "deployment-evidence"
RELEASE_COMMIT = "1" * 40
WORKFLOW_COMMIT = RELEASE_COMMIT
NOW = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
AGENTCORE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/agentcore@sha256:{'a' * 64}"
CONTROL_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/fargate@sha256:{'b' * 64}"
STORAGE_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/11111111-1111-1111-1111-111111111111"
SIGNING_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/22222222-2222-2222-2222-222222222222"
MANIFEST_URI = f"s3://{BUCKET}/{PREFIX}/gate-set.json"
MANIFEST_SIGNATURE_URI = f"s3://{BUCKET}/{PREFIX}/gate-set-kms-signature.json"
REVIEWED_CONFIG_URI = f"s3://{BUCKET}/{PREFIX}/reviewed-launch-gates.json"
REVIEWED_CONFIG_VERSION_ID = "reviewed-config-version-7"
REVIEWED_CONFIG_BYTES = b'{"schema":"reviewed-gates/v2"}\n'
REVIEWED_CONFIG_SHA256 = hashlib.sha256(REVIEWED_CONFIG_BYTES).hexdigest()
SESSION_SECRET = "session-secret-value-that-must-never-leak"
UNRELATED_SECRET = "provider-secret-that-must-not-reach-the-child"


def _release() -> dict[str, str]:
    return {
        "commit": RELEASE_COMMIT,
        "region": REGION,
        "agentcoreImage": AGENTCORE_IMAGE,
        "controlPlaneImage": CONTROL_IMAGE,
    }


def _execution() -> dict[str, str]:
    return {
        "repository": "owner/repo",
        "workflowRef": ("owner/repo/.github/workflows/agentcore-launch-gates.yml@refs/heads/main"),
        "workflowCommit": WORKFLOW_COMMIT,
        "parentWorkflowRef": ("owner/repo/.github/workflows/launch-agentcore-production.yml@refs/heads/main"),
        "parentWorkflowCommit": WORKFLOW_COMMIT,
        "checkedOutCommit": RELEASE_COMMIT,
        "runId": "41",
        "runAttempt": "2",
        "reviewedConfigS3Uri": REVIEWED_CONFIG_URI,
        "reviewedConfigVersionId": REVIEWED_CONFIG_VERSION_ID,
        "reviewedConfigSha256": REVIEWED_CONFIG_SHA256,
    }


def _observations(gate: str) -> dict[str, Any]:
    primary = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-agentcore-state"
    restored = primary + "-restore-validation-20260812-abcd"
    values: dict[str, dict[str, Any]] = {
        "initializationTimeoutReplacement": {
            "timeoutExitCode": 124,
            "startupDeadlineSeconds": 60,
            "timedOutRuntimeId": "runtime-old",
            "replacementRuntimeId": "runtime-new",
            "replacementReadyStatusCode": 200,
        },
        "recoveryCutoverAndRollback": {
            "primaryTableArn": primary,
            "restoredTableArn": restored,
            "cutoverPhases": [
                "quiesced",
                "selected",
                "validation",
                "normal",
            ],
            "rollbackPhases": [
                "quiesced",
                "selected",
                "validation",
                "normal",
            ],
            "cutoverSelectedTableArn": restored,
            "rollbackSelectedTableArn": primary,
            "finalSelectedTableArn": primary,
            "productionEndpointStatusAfter": "READY",
            "controlPlaneDesiredCountAfter": 2,
            "controlPlaneRunningCountAfter": 2,
        },
        "securityEventDeliveryAndDlq": {
            "configuredDestinationCount": 2,
            "deliveredDestinationCount": 2,
            "outboxMessagesAfterDelivery": 0,
            "dlqMessagesAfterFailure": 1,
            "dlqAlarmState": "ALARM",
            "redrivenMessageCount": 1,
            "dlqMessagesAfterRedrive": 0,
            "outboxMessagesAfterRedrive": 0,
        },
        "providerRoutingStrategies": {
            "strategiesExercised": list(evidence.ROUTING_STRATEGIES),
            "candidateProviders": ["anthropic", "openai"],
            "observedProviders": ["anthropic", "openai"],
            "requestCount": 12,
            "successfulRequestCount": 12,
        },
        "providerFallbackRecovery": {
            "primaryProvider": "openai",
            "fallbackProvider": "anthropic",
            "observedProvider": "anthropic",
            "injectedFailureStatusCode": 503,
            "fallbackResponseStatusCode": 200,
            "postRecoveryStatusCode": 200,
            "primaryAttemptCount": 1,
            "fallbackAttemptCount": 1,
        },
        "controlPlaneFaultRecovery": {
            "faultedDependency": "dynamodb",
            "readyDuringFaultStatusCode": 503,
            "readDuringFaultStatusCode": 503,
            "mutationDuringFaultStatusCode": 503,
            "readyAfterRecoveryStatusCode": 200,
            "readAfterRecoveryStatusCode": 200,
        },
    }
    return deepcopy(values[gate])


def _encoded(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _cleanup_output() -> dict[str, Any]:
    return {
        "schema": evidence.COMMAND_OUTPUT_SCHEMA,
        "gate": "cleanup",
        "action": publisher.CLEANUP_ACTION,
        "release": _release(),
        "execution": _execution(),
        "observations": {
            "restoredSnapshotRefs": [],
            "clearedFaultIds": [],
            "clearedFixtureIds": [],
            "redrivenDlqCorrelationIds": [],
            "removedDlqCorrelationIds": [],
            "primaryStateSelected": True,
            "productionEndpointStatus": "READY",
            "faultsRemaining": 0,
            "fixturesRemaining": 0,
            "correlatedDlqMessagesRemaining": 0,
        },
    }


class TickClock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class GateRunner:
    def __init__(
        self,
        *,
        failure: str | None = None,
        target_action: str | None = None,
    ) -> None:
        self.failure = failure
        self.target_action = target_action or next(iter(evidence.EXPECTED_COMMANDS.values()))[0]
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        _cwd: Path,
        environment: Mapping[str, str],
    ) -> publisher.CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        self.environments.append(dict(environment))
        assert command[: len(publisher.COMMAND_PREFIX)] == (publisher.COMMAND_PREFIX)
        action = command[len(publisher.COMMAND_PREFIX)]
        assert "--action" not in command
        if action == publisher.CLEANUP_ACTION:
            assert "--gate" not in command
            if self.failure == "cleanup":
                return publisher.CommandResult(1, b"", b"cleanup failed")
            return publisher.CommandResult(
                0,
                _encoded(_cleanup_output()),
                b"",
            )

        assert "--gate" in command
        gate = command[command.index("--gate") + 1]
        names = evidence.EXPECTED_COMMANDS[gate]
        output = _encoded(
            {
                "schema": evidence.COMMAND_OUTPUT_SCHEMA,
                "gate": gate,
                "action": action,
                "release": _release(),
                "execution": _execution(),
                "observations": (_observations(gate) if action == names[-1] else None),
            }
        )
        if action == self.target_action:
            if self.failure == "exit":
                return publisher.CommandResult(7, output, b"")
            if self.failure == "undrained":
                return publisher.CommandResult(
                    publisher.COORDINATOR_NOT_DRAINED_EXIT_CODE,
                    output + SESSION_SECRET.encode("ascii"),
                    b"",
                )
            if self.failure == "stderr":
                return publisher.CommandResult(0, output, b"warning")
            if self.failure == "malformed":
                return publisher.CommandResult(0, b"{", b"")
            if self.failure == "secret":
                return publisher.CommandResult(
                    0,
                    output + SESSION_SECRET.encode("ascii"),
                    b"",
                )
        return publisher.CommandResult(0, output, b"")


class FakeS3:
    def __init__(
        self,
        *,
        fault: str | None = None,
        fail_put_at: int | None = None,
    ) -> None:
        self.fault = fault
        self.fail_put_at = fail_put_at
        self.puts: list[dict[str, Any]] = []
        self.versioning_requests: list[dict[str, Any]] = []
        self.object_lock_requests: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.retention_gets: list[dict[str, Any]] = []
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self._versions = 0

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        self.versioning_requests.append(dict(kwargs))
        return {"Status": "Enabled"}

    def get_object_lock_configuration(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.object_lock_requests.append(dict(kwargs))
        return {
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
            }
        }

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.puts.append(dict(kwargs))
        if self.fail_put_at == len(self.puts):
            raise RuntimeError("injected partial upload")
        payload = kwargs["Body"]
        assert isinstance(payload, bytes)
        self._versions += 1
        version = "null" if self.fault == "null-version" and self._versions == 1 else f"version-{self._versions}"
        checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        self.objects[(kwargs["Key"], version)] = {
            "payload": payload,
            "checksum": checksum,
            "kms": kwargs["SSEKMSKeyId"],
            "mode": kwargs["ObjectLockMode"],
            "retention": kwargs["ObjectLockRetainUntilDate"],
            "content_type": kwargs["ContentType"],
        }
        return {
            "VersionId": version,
            "ChecksumSHA256": ("wrong" if self.fault == "put-checksum" and self._versions == 1 else checksum),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.gets.append(dict(kwargs))
        stored = self.objects[(kwargs["Key"], kwargs["VersionId"])]
        retention = stored["retention"]
        if self.fault == "retention" and self._versions == 1:
            retention = NOW - timedelta(seconds=1)
        checksum = stored["checksum"]
        if self.fault == "read-checksum" and self._versions == 1:
            checksum = "wrong"
        return {
            "Body": io.BytesIO(stored["payload"]),
            "VersionId": kwargs["VersionId"],
            "ContentLength": len(stored["payload"]),
            "ChecksumSHA256": checksum,
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": stored["kms"],
            "ObjectLockMode": (
                "GOVERNANCE" if self.fault == "mutable-mode" and self._versions == 1 else stored["mode"]
            ),
            "ObjectLockRetainUntilDate": retention,
        }

    def get_object_retention(self, **kwargs: Any) -> dict[str, Any]:
        self.retention_gets.append(dict(kwargs))
        stored = self.objects[(kwargs["Key"], kwargs["VersionId"])]
        retention = stored["retention"]
        if self.fault == "retention" and self._versions == 1:
            retention = NOW - timedelta(seconds=1)
        return {
            "Retention": {
                "Mode": stored["mode"],
                "RetainUntilDate": retention,
            }
        }

    def bytes_for_reference(self, reference: Mapping[str, str]) -> bytes:
        key = reference["s3Uri"].removeprefix(f"s3://{BUCKET}/")
        return self.objects[(key, reference["versionId"])]["payload"]

    def bytes_for_key(self, key: str) -> bytes:
        matches = [stored["payload"] for (stored_key, _version), stored in self.objects.items() if stored_key == key]
        assert len(matches) == 1
        return matches[0]


class FakeKms:
    def __init__(self) -> None:
        self.signed: list[str] = []

    def sign(self, artifact: Path, bundle: Path, key_arn: str) -> None:
        raw = artifact.read_bytes()
        self.signed.append(artifact.name)
        value = {
            "schema": kms_evidence.BUNDLE_SCHEMA,
            "artifact": {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            },
            "signature": {
                "keyArn": key_arn,
                "messageType": kms_evidence.MESSAGE_TYPE,
                "signingAlgorithm": kms_evidence.SIGNING_ALGORITHM,
                "value": base64.b64encode(b"fake-signature").decode("ascii"),
            },
        }
        bundle.write_text(
            json.dumps(value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        bundle.chmod(0o600)

    def verify(self, artifact: Path, bundle: Path, key_arn: str) -> None:
        value = json.loads(bundle.read_text(encoding="utf-8"))
        raw = artifact.read_bytes()
        assert value["artifact"] == {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
        assert value["signature"]["keyArn"] == key_arn


def _execute_arguments(
    tmp_path: Path,
    *,
    runner: GateRunner,
) -> dict[str, Any]:
    reviewed_config = tmp_path / "reviewed-config.json"
    reviewed_config.write_bytes(REVIEWED_CONFIG_BYTES)
    reviewed_config.chmod(0o600)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o700)
    operation_root = tmp_path / "release"
    operation_root.mkdir()
    return {
        "release_commit": RELEASE_COMMIT,
        "region": REGION,
        "agentcore_image": AGENTCORE_IMAGE,
        "control_plane_image": CONTROL_IMAGE,
        "repository": "owner/repo",
        "workflow_ref": _execution()["workflowRef"],
        "workflow_commit": WORKFLOW_COMMIT,
        "parent_workflow_ref": _execution()["parentWorkflowRef"],
        "parent_workflow_commit": WORKFLOW_COMMIT,
        "run_id": "41",
        "run_attempt": "2",
        "reviewed_config": reviewed_config,
        "reviewed_config_uri": REVIEWED_CONFIG_URI,
        "reviewed_config_version_id": REVIEWED_CONFIG_VERSION_ID,
        "reviewed_config_sha256": REVIEWED_CONFIG_SHA256,
        "state_dir": state_dir,
        "execution_bundle": state_dir / publisher.EXECUTION_BUNDLE_FILE,
        "operation_root": operation_root,
        "runner": runner,
        "clock": TickClock(),
        "checkout_verifier": lambda _root, _commit: None,
        "environment": {
            "PATH": "/usr/bin",
            "AWS_ACCESS_KEY_ID": "test-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
            "AWS_SESSION_TOKEN": SESSION_SECRET,
            "AWS_REGION": REGION,
            "HOME": "/home/runner",
            "GITHUB_TOKEN": UNRELATED_SECRET,
            "GOOGLE_AI_API_KEY": UNRELATED_SECRET,
            "PYTHONPATH": "/untrusted/site-packages",
        },
    }


def test_execution_binding_rejects_tooling_checkout_from_another_commit() -> None:
    with pytest.raises(
        publisher.LaunchGatePublisherError,
        match="execution binding is invalid",
    ):
        publisher._validated_release_execution(
            release_commit=RELEASE_COMMIT,
            region=REGION,
            agentcore_image=AGENTCORE_IMAGE,
            control_plane_image=CONTROL_IMAGE,
            repository="owner/repo",
            workflow_ref=_execution()["workflowRef"],
            workflow_commit="2" * 40,
            parent_workflow_ref=_execution()["parentWorkflowRef"],
            parent_workflow_commit=RELEASE_COMMIT,
            run_id="41",
            run_attempt="2",
            reviewed_config_uri=REVIEWED_CONFIG_URI,
            reviewed_config_version_id=REVIEWED_CONFIG_VERSION_ID,
            reviewed_config_sha256=REVIEWED_CONFIG_SHA256,
        )


def _publish_arguments(
    execute_arguments: Mapping[str, Any],
    execute_outputs: Mapping[str, str],
    *,
    s3: FakeS3,
    kms: FakeKms | None = None,
) -> dict[str, Any]:
    fake_kms = kms or FakeKms()
    common_names = {
        "release_commit",
        "region",
        "agentcore_image",
        "control_plane_image",
        "repository",
        "workflow_ref",
        "workflow_commit",
        "parent_workflow_ref",
        "parent_workflow_commit",
        "run_id",
        "run_attempt",
        "reviewed_config",
        "reviewed_config_uri",
        "reviewed_config_version_id",
        "reviewed_config_sha256",
        "state_dir",
        "execution_bundle",
    }
    return {
        **{name: value for name, value in execute_arguments.items() if name in common_names},
        "execution_bundle_sha256": execute_outputs["execution_bundle_sha256"],
        "evidence_bucket": BUCKET,
        "evidence_prefix": PREFIX,
        "manifest_uri": MANIFEST_URI,
        "manifest_signature_uri": MANIFEST_SIGNATURE_URI,
        "storage_kms_key_arn": STORAGE_KEY,
        "signing_key_arn": SIGNING_KEY,
        "s3_client": s3,
        "signer": fake_kms.sign,
        "verifier": fake_kms.verify,
        "clock": TickClock(),
    }


def _run_execute(
    tmp_path: Path,
    runner: GateRunner,
) -> tuple[dict[str, Any], dict[str, str]]:
    arguments = _execute_arguments(tmp_path, runner=runner)
    outputs = publisher.execute_gates(**arguments)
    return arguments, outputs


def _expected_calls() -> list[tuple[str | None, str]]:
    calls = [(gate, action) for gate in evidence.ALL_GATES for action in evidence.EXPECTED_COMMANDS[gate]]
    calls.append((None, publisher.CLEANUP_ACTION))
    return calls


def _actual_calls(runner: GateRunner) -> list[tuple[str | None, str]]:
    calls: list[tuple[str | None, str]] = []
    for argv in runner.calls:
        action = argv[len(publisher.COMMAND_PREFIX)]
        gate = argv[argv.index("--gate") + 1] if "--gate" in argv else None
        calls.append((gate, action))
    return calls


def _assert_signature_pair(
    s3: FakeS3,
    *,
    artifact_key: str,
    signature_key: str,
) -> None:
    artifact = s3.bytes_for_key(artifact_key)
    bundle = json.loads(s3.bytes_for_key(signature_key))
    assert bundle["artifact"] == {
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "size": len(artifact),
    }
    assert bundle["signature"]["keyArn"] == SIGNING_KEY


def _assert_failed_terminal(
    s3: FakeS3,
    *,
    failure_stage: str,
    cleanup_status: str,
) -> None:
    base = f"{PREFIX}/launch-gates/owner/repo/{RELEASE_COMMIT}/41/2"
    artifact_key = f"{base}/attempt-terminal.json"
    signature_key = f"{base}/attempt-terminal-kms-signature.json"
    assert [request["Key"] for request in s3.puts] == [
        artifact_key,
        signature_key,
    ]
    terminal = json.loads(s3.bytes_for_key(artifact_key))
    assert terminal["schema"] == ("axonllm.agentcore-launch-gate-terminal/v1")
    assert terminal["release"] == _release()
    assert terminal["execution"] == _execution()
    assert terminal["status"] == "FAILED"
    assert terminal["failureStage"] == failure_stage
    assert terminal["cleanupStatus"] == cleanup_status
    _assert_signature_pair(
        s3,
        artifact_key=artifact_key,
        signature_key=signature_key,
    )


def test_phase_apis_do_not_share_aws_capabilities() -> None:
    execute_parameters = set(inspect.signature(publisher.execute_gates).parameters)
    publish_parameters = set(inspect.signature(publisher.publish_receipts).parameters)

    assert (
        not {
            "s3_client",
            "signer",
            "verifier",
            "storage_kms_key_arn",
            "signing_key_arn",
            "evidence_bucket",
        }
        & execute_parameters
    )
    assert (
        not {
            "runner",
            "operation_root",
            "checkout_verifier",
            "environment",
        }
        & publish_parameters
    )


def test_publishes_exact_signed_receipts_and_source_manifest(
    tmp_path: Path,
) -> None:
    runner = GateRunner()
    s3 = FakeS3()
    kms = FakeKms()

    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    calls_after_execute = list(runner.calls)
    outputs = publisher.publish_receipts(
        **_publish_arguments(
            execute_arguments,
            execute_outputs,
            s3=s3,
            kms=kms,
        )
    )

    assert _actual_calls(runner) == _expected_calls()
    assert runner.calls == calls_after_execute
    assert sum(len(actions) for actions in evidence.EXPECTED_COMMANDS.values()) == 25
    assert len(s3.puts) == 66
    assert len(kms.signed) == 8
    assert s3.puts[-2]["Key"] == (f"{PREFIX}/gate-set-kms-signature.json")
    assert s3.puts[-1]["Key"] == f"{PREFIX}/gate-set.json"
    assert "IfNoneMatch" not in s3.puts[-2]
    assert "IfNoneMatch" not in s3.puts[-1]
    assert all(request.get("IfNoneMatch") == "*" for request in s3.puts[:-2])

    manifest_reference = {
        "s3Uri": outputs["gate_manifest_uri"],
        "versionId": outputs["gate_manifest_version_id"],
        "sha256": outputs["gate_manifest_sha256"],
    }
    manifest_raw = s3.bytes_for_reference(manifest_reference)
    assert hashlib.sha256(manifest_raw).hexdigest() == (outputs["gate_manifest_sha256"])
    manifest = json.loads(manifest_raw)
    assert manifest["schema"] == evidence.SOURCE_SCHEMA
    assert manifest["release"] == _release()
    assert manifest["execution"] == _execution()
    assert set(manifest["gates"]) == set(evidence.ALL_GATES)
    evidence._validate_source_manifest(
        manifest,
        expected_release=_release(),
        evidence_bucket=BUCKET,
        evidence_prefix=PREFIX,
    )

    for gate, names in evidence.EXPECTED_COMMANDS.items():
        receipt_reference = manifest["gates"][gate]["artifact"]
        receipt = json.loads(s3.bytes_for_reference(receipt_reference))
        assert receipt["schema"] == evidence.GATE_SCHEMA
        assert receipt["environment"] == "production"
        assert [command["name"] for command in receipt["commands"]] == list(names)
        for command in receipt["commands"]:
            argv = command["argv"]
            assert argv[: len(publisher.COMMAND_PREFIX)] == list(publisher.COMMAND_PREFIX)
            assert argv[len(publisher.COMMAND_PREFIX)] == command["name"]
            assert "--action" not in argv
            assert argv[argv.index("--reviewed-config-s3-uri") + 1] == (REVIEWED_CONFIG_URI)
            assert argv[argv.index("--reviewed-config-version-id") + 1] == (REVIEWED_CONFIG_VERSION_ID)
            assert argv[argv.index("--reviewed-config-sha256") + 1] == (REVIEWED_CONFIG_SHA256)
            assert command["tool"] == publisher.TOOL
            assert command["commandSha256"] == evidence._canonical_sha(argv)
            assert command["exitCode"] == 0
            assert s3.bytes_for_reference(command["stderr"]) == b""
            assert hashlib.sha256(s3.bytes_for_reference(command["stdout"])).hexdigest() == command["stdout"]["sha256"]
            assert SESSION_SECRET not in json.dumps(command)

    terminal_pair = manifest["terminal"]
    terminal = json.loads(s3.bytes_for_reference(terminal_pair["artifact"]))
    assert terminal == {
        "schema": "axonllm.agentcore-launch-gate-terminal/v1",
        "release": _release(),
        "execution": _execution(),
        "status": "PASSED",
        "failureStage": None,
        "cleanupStatus": "SUCCEEDED",
        "cleanupObservations": _cleanup_output()["observations"],
        "startedAt": terminal["startedAt"],
        "completedAt": terminal["completedAt"],
    }
    terminal_artifact_key = terminal_pair["artifact"]["s3Uri"].removeprefix(f"s3://{BUCKET}/")
    terminal_signature_key = terminal_pair["signature"]["s3Uri"].removeprefix(f"s3://{BUCKET}/")
    _assert_signature_pair(
        s3,
        artifact_key=terminal_artifact_key,
        signature_key=terminal_signature_key,
    )

    all_requests = [
        *s3.versioning_requests,
        *s3.object_lock_requests,
        *s3.puts,
        *s3.gets,
        *s3.retention_gets,
    ]
    assert all_requests
    assert all(request["ExpectedBucketOwner"] == ACCOUNT for request in all_requests)
    assert len(s3.versioning_requests) == 1
    assert len(s3.object_lock_requests) == 1
    assert len(s3.gets) == len(s3.puts)
    assert len(s3.retention_gets) == len(s3.puts)
    expected_retention = NOW + timedelta(days=publisher.RETENTION_DAYS)
    assert all(
        request["ObjectLockMode"] == "COMPLIANCE" and request["ObjectLockRetainUntilDate"] == expected_retention
        for request in s3.puts
    )

    assert set(outputs) == {
        "gate_manifest_uri",
        "gate_manifest_version_id",
        "gate_manifest_sha256",
        "gate_manifest_signature_uri",
        "gate_manifest_signature_version_id",
        "gate_manifest_signature_sha256",
    }
    signature_reference = {
        "s3Uri": outputs["gate_manifest_signature_uri"],
        "versionId": outputs["gate_manifest_signature_version_id"],
        "sha256": outputs["gate_manifest_signature_sha256"],
    }
    signature_raw = s3.bytes_for_reference(signature_reference)
    assert hashlib.sha256(signature_raw).hexdigest() == (outputs["gate_manifest_signature_sha256"])
    assert not any(SESSION_SECRET.encode("ascii") in stored["payload"] for stored in s3.objects.values())
    bundle = execute_arguments["execution_bundle"]
    bundle_raw = bundle.read_bytes()
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(bundle_raw).hexdigest() == execute_outputs["execution_bundle_sha256"]
    assert bundle_raw == publisher._canonical_json_bytes(json.loads(bundle_raw))
    assert SESSION_SECRET.encode("ascii") not in bundle_raw
    assert UNRELATED_SECRET.encode("ascii") not in bundle_raw


@pytest.mark.parametrize("failure", ["exit", "stderr", "malformed"])
def test_action_failure_runs_cleanup_and_publishes_failure_terminal(
    tmp_path: Path,
    failure: str,
) -> None:
    runner = GateRunner(failure=failure)
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    calls_after_execute = list(runner.calls)

    with pytest.raises(
        publisher.LaunchGatePublisherError,
        match="immutable terminal evidence was published",
    ):
        publisher.publish_receipts(
            **_publish_arguments(
                execute_arguments,
                execute_outputs,
                s3=s3,
            )
        )

    assert execute_outputs["execution_status"] == "FAILED"
    assert runner.calls == calls_after_execute
    assert _actual_calls(runner)[-1] == (None, publisher.CLEANUP_ACTION)
    assert _actual_calls(runner).count((None, publisher.CLEANUP_ACTION)) == 1
    _assert_failed_terminal(
        s3,
        failure_stage="operation",
        cleanup_status="SUCCEEDED",
    )


def test_undrained_coordinator_suppresses_unsafe_cleanup(
    tmp_path: Path,
) -> None:
    runner = GateRunner(failure="undrained")
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    calls_after_execute = list(runner.calls)

    with pytest.raises(
        publisher.LaunchGatePublisherError,
        match="immutable terminal evidence was published",
    ):
        publisher.publish_receipts(
            **_publish_arguments(
                execute_arguments,
                execute_outputs,
                s3=s3,
            )
        )

    assert execute_outputs["execution_status"] == "FAILED"
    assert runner.calls == calls_after_execute
    assert (None, publisher.CLEANUP_ACTION) not in _actual_calls(runner)
    assert SESSION_SECRET.encode("ascii") not in execute_arguments["execution_bundle"].read_bytes()
    _assert_failed_terminal(
        s3,
        failure_stage="operation-and-cleanup",
        cleanup_status="FAILED",
    )


def test_cleanup_failure_publishes_failure_terminal_only(tmp_path: Path) -> None:
    runner = GateRunner(failure="cleanup")
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)

    with pytest.raises(
        publisher.LaunchGatePublisherError,
        match="immutable terminal evidence was published",
    ):
        publisher.publish_receipts(
            **_publish_arguments(
                execute_arguments,
                execute_outputs,
                s3=s3,
            )
        )

    assert execute_outputs["execution_status"] == "FAILED"
    assert _actual_calls(runner) == _expected_calls()
    _assert_failed_terminal(
        s3,
        failure_stage="cleanup",
        cleanup_status="FAILED",
    )


def test_child_environment_is_strictly_sanitized(tmp_path: Path) -> None:
    runner = GateRunner(failure="exit")

    _run_execute(tmp_path, runner)

    expected = {
        "PATH": "/usr/bin",
        "AWS_ACCESS_KEY_ID": "test-access-key-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
        "AWS_SESSION_TOKEN": SESSION_SECRET,
        "AWS_REGION": REGION,
        "HOME": "/home/runner",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    assert runner.environments
    assert all(environment == expected for environment in runner.environments)
    assert all(UNRELATED_SECRET not in environment.values() for environment in runner.environments)


@pytest.mark.parametrize(
    "fault",
    [
        "null-version",
        "put-checksum",
        "read-checksum",
        "retention",
        "mutable-mode",
    ],
)
def test_rejects_mutable_or_bad_s3_publication(
    tmp_path: Path,
    fault: str,
) -> None:
    runner = GateRunner()
    s3 = FakeS3(fault=fault)
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    calls_after_execute = list(runner.calls)

    with pytest.raises(publisher.LaunchGatePublisherError):
        publisher.publish_receipts(
            **_publish_arguments(
                execute_arguments,
                execute_outputs,
                s3=s3,
            )
        )

    assert _actual_calls(runner) == _expected_calls()
    assert runner.calls == calls_after_execute
    assert not any(request["Key"] == f"{PREFIX}/gate-set.json" for request in s3.puts)


def test_partial_upload_never_publishes_manifest(tmp_path: Path) -> None:
    runner = GateRunner()
    s3 = FakeS3(fail_put_at=5)
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)

    with pytest.raises(
        publisher.LaunchGatePublisherError,
        match="publish immutable",
    ):
        publisher.publish_receipts(
            **_publish_arguments(
                execute_arguments,
                execute_outputs,
                s3=s3,
            )
        )

    assert len(s3.objects) == 4
    assert not any(
        key
        in {
            f"{PREFIX}/gate-set.json",
            f"{PREFIX}/gate-set-kms-signature.json",
        }
        for key, _version in s3.objects
    )


def test_sensitive_environment_material_is_never_published(
    tmp_path: Path,
) -> None:
    runner = GateRunner(failure="secret")
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)

    with pytest.raises(
        publisher.LaunchGatePublisherError,
    ) as raised:
        publisher.publish_receipts(
            **_publish_arguments(
                execute_arguments,
                execute_outputs,
                s3=s3,
            )
        )

    assert SESSION_SECRET not in str(raised.value)
    assert SESSION_SECRET.encode("ascii") not in execute_arguments["execution_bundle"].read_bytes()
    assert _actual_calls(runner)[-1] == (None, publisher.CLEANUP_ACTION)
    _assert_failed_terminal(
        s3,
        failure_stage="operation",
        cleanup_status="SUCCEEDED",
    )
    assert not any(SESSION_SECRET.encode("ascii") in stored["payload"] for stored in s3.objects.values())


@pytest.mark.parametrize(
    "mutation",
    ["noncanonical", "extra-field", "public-mode", "symlink"],
)
def test_publish_rejects_unsafe_execution_bundle_before_aws(
    tmp_path: Path,
    mutation: str,
) -> None:
    runner = GateRunner()
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    bundle = execute_arguments["execution_bundle"]
    original = bundle.read_bytes()

    if mutation == "noncanonical":
        bundle.write_bytes(original + b"\n")
    elif mutation == "extra-field":
        value = json.loads(original)
        value["unexpected"] = True
        bundle.write_bytes(publisher._canonical_json_bytes(value))
    elif mutation == "public-mode":
        bundle.chmod(0o644)
    else:
        target = bundle.with_name("replacement.json")
        bundle.replace(target)
        bundle.symlink_to(target)

    publish_arguments = _publish_arguments(
        execute_arguments,
        execute_outputs,
        s3=s3,
    )
    if mutation in {"noncanonical", "extra-field"}:
        publish_arguments["execution_bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()

    with pytest.raises(publisher.LaunchGatePublisherError):
        publisher.publish_receipts(**publish_arguments)

    assert not s3.versioning_requests
    assert not s3.object_lock_requests
    assert not s3.puts


def test_duplicate_manifest_destinations_fail_without_rerunning_execution(
    tmp_path: Path,
) -> None:
    runner = GateRunner()
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    calls_after_execute = list(runner.calls)
    arguments = _publish_arguments(
        execute_arguments,
        execute_outputs,
        s3=s3,
    )
    arguments["manifest_signature_uri"] = MANIFEST_URI

    with pytest.raises(
        publisher.LaunchGatePublisherError,
        match="destinations must be distinct",
    ):
        publisher.publish_receipts(**arguments)

    assert runner.calls == calls_after_execute
    assert not s3.puts


def test_writes_only_complete_exact_github_output_triples(
    tmp_path: Path,
) -> None:
    runner = GateRunner()
    s3 = FakeS3()
    execute_arguments, execute_outputs = _run_execute(tmp_path, runner)
    outputs = publisher.publish_receipts(
        **_publish_arguments(
            execute_arguments,
            execute_outputs,
            s3=s3,
        )
    )
    github_output = tmp_path / "github-output"

    publisher._write_execution_github_output(
        github_output,
        execute_outputs,
    )
    publisher._write_github_output(github_output, outputs)

    written = dict(line.split("=", maxsplit=1) for line in github_output.read_text(encoding="utf-8").splitlines())
    assert written == {**execute_outputs, **outputs}
