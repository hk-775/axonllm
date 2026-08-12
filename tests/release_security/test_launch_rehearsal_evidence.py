from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))

import launch_rehearsal_evidence as evidence  # noqa: E402
import deployment_evidence  # noqa: E402


ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "axonllm-evidence-prod"
PREFIX = "deployment-evidence"
NOW = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
RELEASE_COMMIT = "1" * 40
WORKFLOW_COMMIT = RELEASE_COMMIT
AGENTCORE_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/agentcore@"
    f"sha256:{'a' * 64}"
)
CONTROL_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/fargate@"
    f"sha256:{'b' * 64}"
)
STORAGE_KEY = (
    f"arn:aws:kms:{REGION}:{ACCOUNT}:"
    "key/11111111-1111-1111-1111-111111111111"
)
SIGNING_KEY = (
    f"arn:aws:kms:{REGION}:{ACCOUNT}:"
    "key/22222222-2222-2222-2222-222222222222"
)


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
        "workflowRef": (
            "owner/repo/.github/workflows/"
            "agentcore-launch-gates.yml@refs/heads/main"
        ),
        "workflowCommit": WORKFLOW_COMMIT,
        "parentWorkflowRef": (
            "owner/repo/.github/workflows/"
            "launch-agentcore-production.yml@refs/heads/main"
        ),
        "parentWorkflowCommit": RELEASE_COMMIT,
        "checkedOutCommit": RELEASE_COMMIT,
        "runId": "41",
        "runAttempt": "1",
        "reviewedConfigS3Uri": (
            f"s3://{BUCKET}/{PREFIX}/reviewed/launch-gates.json"
        ),
        "reviewedConfigVersionId": "reviewed-config-version",
        "reviewedConfigSha256": "c" * 64,
    }


def _producer() -> dict[str, str]:
    return {
        "repository": "owner/repo",
        "workflowRef": (
            "owner/repo/.github/workflows/"
            "agentcore-launch-rehearsal-evidence.yml@refs/heads/main"
        ),
        "workflowCommit": RELEASE_COMMIT,
        "parentWorkflowRef": (
            "owner/repo/.github/workflows/"
            "launch-agentcore-production.yml@refs/heads/main"
        ),
        "parentWorkflowCommit": RELEASE_COMMIT,
        "runId": "41",
        "runAttempt": "1",
    }


def _reference(
    name: str,
    *,
    digest: str | None = None,
    suffix: str = "json",
) -> dict[str, str]:
    return {
        "s3Uri": f"s3://{BUCKET}/{PREFIX}/gates/{name}.{suffix}",
        "versionId": f"version-{name}",
        "sha256": digest or hashlib.sha256(name.encode()).hexdigest(),
    }


def _pair(name: str) -> dict[str, dict[str, str]]:
    return {
        "artifact": _reference(name),
        "signature": _reference(f"{name}-signature"),
    }


def _source_manifest() -> dict[str, Any]:
    return {
        "schema": evidence.SOURCE_SCHEMA,
        "release": _release(),
        "execution": _execution(),
        "terminal": _pair("terminal"),
        "gates": {
            gate: _pair(gate)
            for gate in evidence.ALL_GATES
        },
    }


def _source() -> evidence.ValidatedSource:
    return evidence._validate_source_manifest(
        _source_manifest(),
        expected_release=_release(),
        evidence_bucket=BUCKET,
        evidence_prefix=PREFIX,
    )


def _commands(gate: str) -> list[dict[str, Any]]:
    base = NOW - timedelta(hours=2)
    commands: list[dict[str, Any]] = []
    for index, name in enumerate(evidence.EXPECTED_COMMANDS[gate]):
        started = base + timedelta(minutes=index * 2)
        completed = started + timedelta(minutes=1)
        argv = [
            "python",
            "scripts/operations/rehearse_agentcore_launch.py",
            name,
            "--gate",
            gate,
            "--reviewed-config",
            "/tmp/reviewed-launch-gates.json",
            "--control-plane-image",
            CONTROL_IMAGE,
        ]
        commands.append(
            {
                "name": name,
                "tool": (
                    "python:scripts/operations/"
                    "rehearse_agentcore_launch.py"
                ),
                "argv": argv,
                "commandSha256": evidence._canonical_sha(argv),
                "stdout": _reference(
                    f"{gate}-{index + 1}-stdout",
                    digest=hashlib.sha256(
                        _command_output(gate, index, "stdout")
                    ).hexdigest(),
                    suffix="json",
                ),
                "stderr": _reference(
                    f"{gate}-{index + 1}-stderr",
                    digest=hashlib.sha256(b"").hexdigest(),
                    suffix="log",
                ),
                "startedAt": started.isoformat(timespec="seconds"),
                "completedAt": completed.isoformat(timespec="seconds"),
                "exitCode": 0,
            }
        )
    return commands


def _command_output(gate: str, index: int, stream: str) -> bytes:
    if stream == "stderr":
        return b""
    names = evidence.EXPECTED_COMMANDS[gate]
    return _encoded(
        {
            "schema": evidence.COMMAND_OUTPUT_SCHEMA,
            "gate": gate,
            "action": names[index],
            "release": _release(),
            "execution": _execution(),
            "observations": (
                _observations(gate)
                if index == len(names) - 1
                else None
            ),
        }
    )


def _command_outputs() -> dict[tuple[str, int, str], bytes]:
    return {
        (gate, index, stream): _command_output(gate, index, stream)
        for gate in evidence.ALL_GATES
        for index in range(len(evidence.EXPECTED_COMMANDS[gate]))
        for stream in ("stdout", "stderr")
    }


def _observations(gate: str) -> dict[str, Any]:
    primary = (
        f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:"
        "table/axonllm-agentcore-state"
    )
    restored = primary + "-restore-validation-20260812-abcd"
    values: dict[str, dict[str, Any]] = {
        "initializationTimeoutReplacement": {
            "timeoutExitCode": 124,
            "startupDeadlineSeconds": 60,
            "timedOutRuntimeId": "runtime-old",
            "replacementRuntimeId": "runtime-new",
            "replacementReadyStatusCode": 200,
        },
        "queryBoundaryLimitsAndReconciliation": {
            "mutationStatusCode": 400,
            "multipleStatementsStatusCode": 400,
            "outOfDatasourceStatusCode": 403,
            "requestedMaxRows": 100,
            "returnedRowCount": 25,
            "scanLimitBytes": 1024 * 1024,
            "observedBytesScanned": 512 * 1024,
            "interruptedRequestId": "query-request-1",
            "terminalState": "CANCELLED",
            "reservationUnitsAfter": 0,
            "durableResultAuditCount": 1,
            "unavailableBindingState": "DEFERRED",
            "unavailableBindingReservationReleased": False,
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


def _receipt(gate: str) -> dict[str, Any]:
    return {
        "schema": evidence.GATE_SCHEMA,
        "gate": gate,
        "release": _release(),
        "execution": _execution(),
        "environment": "production",
        "commands": _commands(gate),
    }


def _receipts() -> dict[str, dict[str, Any]]:
    return {gate: _receipt(gate) for gate in evidence.ALL_GATES}


def _cleanup_observations() -> dict[str, Any]:
    return {
        "restoredSnapshotRefs": ["snapshot/control-plane"],
        "clearedFaultIds": ["fault/control-plane"],
        "clearedFixtureIds": ["fixture/query"],
        "redrivenDlqCorrelationIds": ["event/redriven"],
        "removedDlqCorrelationIds": ["event/removed"],
        "primaryStateSelected": True,
        "productionEndpointStatus": "READY",
        "faultsRemaining": 0,
        "fixturesRemaining": 0,
        "correlatedDlqMessagesRemaining": 0,
    }


def _terminal() -> dict[str, Any]:
    return {
        "schema": evidence.TERMINAL_SCHEMA,
        "release": _release(),
        "execution": _execution(),
        "status": "PASSED",
        "failureStage": None,
        "cleanupStatus": "SUCCEEDED",
        "cleanupObservations": _cleanup_observations(),
        "startedAt": (NOW - timedelta(hours=3)).isoformat(
            timespec="seconds"
        ),
        "completedAt": (NOW - timedelta(minutes=30)).isoformat(
            timespec="seconds"
        ),
    }


def _source_pair() -> evidence.ArtifactPair:
    artifact = evidence._validate_reference(
        _reference("gate-set"),
        expected_bucket=BUCKET,
        expected_prefix=PREFIX,
        location="source artifact",
    )
    signature = evidence._validate_reference(
        _reference("gate-set-signature"),
        expected_bucket=BUCKET,
        expected_prefix=PREFIX,
        location="source signature",
    )
    return evidence.ArtifactPair(artifact=artifact, signature=signature)


def _build(
    receipts: dict[str, dict[str, Any]] | None = None,
    command_outputs: dict[tuple[str, int, str], bytes] | None = None,
    script_checker: evidence.ScriptChecker | None = None,
    terminal: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return evidence.build_reports(
        _source(),
        receipts or _receipts(),
        terminal=_terminal() if terminal is None else terminal,
        command_outputs=command_outputs or _command_outputs(),
        script_checker=script_checker or (lambda _commit, _script: None),
        source_pair=_source_pair(),
        producer=_producer(),
        evidence_bucket=BUCKET,
        evidence_prefix=PREFIX,
        now=NOW,
    )


def test_builds_seven_gate_report_and_current_deployment_projection() -> None:
    detailed, compatibility = _build()

    assert detailed["schema"] == evidence.REPORT_SCHEMA
    assert set(detailed["gates"]) == set(evidence.ALL_GATES)
    assert detailed["releaseCommit"] == RELEASE_COMMIT
    assert detailed["producer"] == _producer()
    assert detailed["gateExecution"] == _execution()
    assert set(compatibility["gates"]) == set(evidence.CORE_GATES)
    assert compatibility["schema"] == evidence.COMPATIBILITY_SCHEMA
    for gate in evidence.CORE_GATES:
        immutable_id = compatibility["gates"][gate]["evidenceId"]
        assert immutable_id.startswith("s3://")
        assert "?versionId=" in immutable_id
        assert "&sha256=" in immutable_id
        assert compatibility["gates"][gate]["status"] == "PASS"


def test_detailed_report_is_accepted_by_deployment_evidence_contract() -> None:
    detailed, _ = _build()

    normalized = deployment_evidence._validate_launch_rehearsal(
        detailed,
        release_commit=RELEASE_COMMIT,
        region=REGION,
        agentcore_image=AGENTCORE_IMAGE,
        fargate_image=CONTROL_IMAGE,
        repository="owner/repo",
        evidence_bucket=BUCKET,
        evidence_prefix=PREFIX,
    )

    assert normalized == detailed


def test_deployment_contract_requires_protected_rehearsal_producer() -> None:
    detailed, _ = _build()
    detailed["producer"]["workflowRef"] = (
        "owner/repo/.github/workflows/other.yml@refs/heads/main"
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="protected rehearsal workflow",
    ):
        deployment_evidence._validate_launch_rehearsal(
            detailed,
            release_commit=RELEASE_COMMIT,
            region=REGION,
            agentcore_image=AGENTCORE_IMAGE,
            fargate_image=CONTROL_IMAGE,
            repository="owner/repo",
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
        )


def test_receipt_cannot_supply_a_hand_authored_pass() -> None:
    receipts = _receipts()
    receipts["initializationTimeoutReplacement"]["status"] = "PASS"

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="fields do not match schema",
    ):
        _build(receipts)


def test_gate_execution_requires_the_single_allowlisted_workflow() -> None:
    manifest = _source_manifest()
    manifest["execution"]["workflowRef"] = (
        "owner/repo/.github/workflows/other.yml@refs/heads/main"
    )

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="allowlisted launch-gate workflow",
    ):
        evidence._validate_source_manifest(
            manifest,
            expected_release=_release(),
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
        )


def test_source_manifest_requires_every_gate() -> None:
    manifest = _source_manifest()
    manifest["gates"].pop("providerFallbackRecovery")

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="every required launch gate",
    ):
        evidence._validate_source_manifest(
            manifest,
            expected_release=_release(),
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
        )


def test_build_requires_every_signed_receipt() -> None:
    receipts = _receipts()
    receipts.pop("securityEventDeliveryAndDlq")

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="every required launch gate",
    ):
        _build(receipts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "FAILED"),
        ("failureStage", "operation"),
        ("cleanupStatus", "FAILED"),
    ],
)
def test_build_requires_successful_terminal_record(
    field: str,
    value: Any,
) -> None:
    terminal = _terminal()
    terminal[field] = value

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="does not prove a successful current run",
    ):
        _build(terminal=terminal)


def test_terminal_requires_complete_cleanup() -> None:
    terminal = _terminal()
    terminal["cleanupObservations"]["fixturesRemaining"] = 1

    with pytest.raises(evidence.LaunchRehearsalError):
        _build(terminal=terminal)


def test_terminal_rejects_non_string_cleanup_inventory() -> None:
    terminal = _terminal()
    terminal["cleanupObservations"]["clearedFixtureIds"] = [{}]

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="cleanup clearedFixtureIds is malformed",
    ):
        _build(terminal=terminal)


def test_terminal_must_bracket_gate_commands() -> None:
    terminal = _terminal()
    terminal["startedAt"] = (NOW - timedelta(hours=1)).isoformat(
        timespec="seconds"
    )

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="outside the terminal run",
    ):
        _build(terminal=terminal)


@pytest.mark.parametrize(
    "failure",
    ["missing", "nonzero", "boolean_exit", "out_of_order", "hash_mismatch"],
)
def test_command_sequence_must_be_complete_and_successful(
    failure: str,
) -> None:
    receipts = _receipts()
    commands = receipts["providerFallbackRecovery"]["commands"]
    if failure == "missing":
        commands.pop()
    elif failure == "nonzero":
        commands[1]["exitCode"] = 1
    elif failure == "boolean_exit":
        commands[1]["exitCode"] = False
    elif failure == "hash_mismatch":
        commands[1]["argv"].append("--unbound")
    else:
        commands[1]["name"], commands[2]["name"] = (
            commands[2]["name"],
            commands[1]["name"],
        )

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="command receipt|successful command|exitCode",
    ):
        _build(receipts)


def test_gate_receipt_release_and_execution_must_match_manifest() -> None:
    receipts = _receipts()
    receipts["providerRoutingStrategies"]["execution"]["runId"] = "42"

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="not bound to this production gate run",
    ):
        _build(receipts)


def test_command_receipt_rejects_sensitive_argv() -> None:
    receipts = _receipts()
    command = receipts["providerRoutingStrategies"]["commands"][0]
    command["argv"].extend(["--token", "not-a-real-secret"])
    command["commandSha256"] = evidence._canonical_sha(command["argv"])

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="argv is unsafe",
    ):
        _build(receipts)


def test_command_receipt_cannot_complete_after_report_generation() -> None:
    receipts = _receipts()
    command = receipts["providerRoutingStrategies"]["commands"][-1]
    command["startedAt"] = (NOW + timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    command["completedAt"] = (NOW + timedelta(minutes=2)).isoformat(
        timespec="seconds"
    )

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="outside the accepted window",
    ):
        _build(receipts)


def test_substituted_command_output_bytes_are_rejected() -> None:
    outputs = _command_outputs()
    outputs[("providerRoutingStrategies", 0, "stdout")] = b"{}\n"

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="output bytes do not match immutable references",
    ):
        _build(command_outputs=outputs)


def test_nonexistent_command_script_is_rejected() -> None:
    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="does not exist at the release commit",
    ):
        _build(script_checker=evidence._verify_script_at_commit)


def test_mutable_image_reference_is_rejected() -> None:
    release = _release()
    release["agentcoreImage"] = (
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/agentcore:latest"
    )

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="immutable ECR digest URI",
    ):
        evidence._validate_release(release)


def test_target_image_repositories_cannot_be_substituted() -> None:
    release = _release()
    release["agentcoreImage"] = AGENTCORE_IMAGE.replace(
        "axonllm/agentcore",
        "axonllm/other",
    )

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="immutable ECR digest URI",
    ):
        evidence._validate_release(release)


@pytest.mark.parametrize(
    ("gate", "field", "value"),
    [
        (
            "initializationTimeoutReplacement",
            "replacementRuntimeId",
            "runtime-old",
        ),
        (
            "queryBoundaryLimitsAndReconciliation",
            "durableResultAuditCount",
            0,
        ),
        (
            "queryBoundaryLimitsAndReconciliation",
            "durableResultAuditCount",
            True,
        ),
        (
            "recoveryCutoverAndRollback",
            "finalSelectedTableArn",
            (
                f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:"
                "table/axonllm-agentcore-state-restore-validation-"
                "20260812-abcd"
            ),
        ),
        ("securityEventDeliveryAndDlq", "dlqAlarmState", "OK"),
        (
            "providerRoutingStrategies",
            "strategiesExercised",
            ["round-robin"],
        ),
        (
            "providerFallbackRecovery",
            "observedProvider",
            "openai",
        ),
        (
            "controlPlaneFaultRecovery",
            "readyDuringFaultStatusCode",
            200,
        ),
    ],
)
def test_gate_specific_partial_or_failed_observations_are_rejected(
    gate: str,
    field: str,
    value: Any,
) -> None:
    receipts = _receipts()
    outputs = _command_outputs()
    final_index = len(evidence.EXPECTED_COMMANDS[gate]) - 1
    output = json.loads(
        outputs[(gate, final_index, "stdout")].decode("utf-8")
    )
    output["observations"][field] = value
    encoded = _encoded(output)
    outputs[(gate, final_index, "stdout")] = encoded
    receipts[gate]["commands"][final_index]["stdout"]["sha256"] = (
        hashlib.sha256(encoded).hexdigest()
    )

    with pytest.raises(evidence.LaunchRehearsalError):
        _build(receipts, outputs)


def test_source_manifest_rejects_reused_object_version() -> None:
    manifest = _source_manifest()
    reused = deepcopy(
        manifest["gates"]["providerFallbackRecovery"]["artifact"]
    )
    manifest["gates"]["controlPlaneFaultRecovery"]["signature"] = reused

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="reuses an immutable object version",
    ):
        evidence._validate_source_manifest(
            manifest,
            expected_release=_release(),
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
        )


def test_reference_rejects_null_version_and_outside_prefix() -> None:
    reference = _reference("gate")
    reference["versionId"] = "null"
    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="immutable binding",
    ):
        evidence._validate_reference(
            reference,
            expected_bucket=BUCKET,
            expected_prefix=PREFIX,
            location="gate",
        )

    reference = _reference("gate")
    reference["s3Uri"] = f"s3://{BUCKET}/other/gate.json"
    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="outside the approved evidence prefix",
    ):
        evidence._validate_reference(
            reference,
            expected_bucket=BUCKET,
            expected_prefix=PREFIX,
            location="gate",
        )


def test_compatibility_report_rejects_arbitrary_evidence_id() -> None:
    detailed, compatibility = _build()
    compatibility["gates"]["initializationTimeoutReplacement"][
        "evidenceId"
    ] = "operator-says-pass"

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="not immutable evidence",
    ):
        evidence.verify_reports(
            detailed,
            compatibility,
            expected_release=_release(),
            expected_producer=_producer(),
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
        )


def test_detailed_report_requires_provider_and_control_fault_gates() -> None:
    detailed, compatibility = _build()
    detailed["gates"].pop("controlPlaneFaultRecovery")

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="missing required gates",
    ):
        evidence.verify_reports(
            detailed,
            compatibility,
            expected_release=_release(),
            expected_producer=_producer(),
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("VersionId", "wrong-version"),
        ("ServerSideEncryption", "AES256"),
        ("SSEKMSKeyId", SIGNING_KEY),
        ("ObjectLockMode", "GOVERNANCE"),
        (
            "ObjectLockRetainUntilDate",
            (NOW - timedelta(seconds=1)).isoformat(),
        ),
        ("ContentLength", 99),
    ],
)
def test_download_requires_exact_compliance_locked_version(
    field: str,
    value: Any,
) -> None:
    raw = _reference("locked-gate")
    reference = evidence._validate_reference(
        raw,
        expected_bucket=BUCKET,
        expected_prefix=PREFIX,
        location="locked gate",
    )
    metadata: dict[str, Any] = {
        "VersionId": reference.version_id,
        "ContentLength": 100,
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": STORAGE_KEY,
        "ObjectLockMode": "COMPLIANCE",
        "ObjectLockRetainUntilDate": (
            NOW + timedelta(days=30)
        ).isoformat(),
    }
    metadata[field] = value

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="not the required immutable COMPLIANCE version",
    ):
        evidence._validate_download_metadata(
            metadata,
            reference=reference,
            storage_key_arn=STORAGE_KEY,
            downloaded_size=100,
            now=NOW,
        )


def test_strict_json_rejects_duplicate_fields(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")

    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="not strict JSON",
    ):
        evidence._read_json(path)


def _encoded(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _materialized_gate_set() -> tuple[
    evidence.ArtifactPair,
    dict[tuple[str, str], bytes],
]:
    objects: dict[tuple[str, str], bytes] = {}
    gates: dict[str, Any] = {}
    for gate in evidence.ALL_GATES:
        receipt = _receipt(gate)
        receipt_bytes = _encoded(receipt)
        signature_bytes = _encoded(
            {"fixture": f"{gate}-kms-signature"}
        )
        artifact_raw = _reference(
            gate,
            digest=hashlib.sha256(receipt_bytes).hexdigest(),
        )
        signature_raw = _reference(
            f"{gate}-signature",
            digest=hashlib.sha256(signature_bytes).hexdigest(),
        )
        gates[gate] = {
            "artifact": artifact_raw,
            "signature": signature_raw,
        }
        objects[
            (artifact_raw["s3Uri"], artifact_raw["versionId"])
        ] = receipt_bytes
        objects[
            (signature_raw["s3Uri"], signature_raw["versionId"])
        ] = signature_bytes
        for index, command in enumerate(receipt["commands"]):
            for stream in ("stdout", "stderr"):
                reference = command[stream]
                objects[
                    (reference["s3Uri"], reference["versionId"])
                ] = _command_output(gate, index, stream)

    terminal_bytes = _encoded(_terminal())
    terminal_signature_bytes = _encoded(
        {"fixture": "terminal-kms-signature"}
    )
    terminal_artifact_raw = _reference(
        "terminal",
        digest=hashlib.sha256(terminal_bytes).hexdigest(),
    )
    terminal_signature_raw = _reference(
        "terminal-signature",
        digest=hashlib.sha256(terminal_signature_bytes).hexdigest(),
    )
    objects[
        (
            terminal_artifact_raw["s3Uri"],
            terminal_artifact_raw["versionId"],
        )
    ] = terminal_bytes
    objects[
        (
            terminal_signature_raw["s3Uri"],
            terminal_signature_raw["versionId"],
        )
    ] = terminal_signature_bytes
    manifest = {
        "schema": evidence.SOURCE_SCHEMA,
        "release": _release(),
        "execution": _execution(),
        "terminal": {
            "artifact": terminal_artifact_raw,
            "signature": terminal_signature_raw,
        },
        "gates": gates,
    }
    manifest_bytes = _encoded(manifest)
    manifest_signature_bytes = _encoded(
        {"fixture": "gate-set-kms-signature"}
    )
    artifact_raw = _reference(
        "gate-set",
        digest=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    signature_raw = _reference(
        "gate-set-signature",
        digest=hashlib.sha256(manifest_signature_bytes).hexdigest(),
    )
    objects[
        (artifact_raw["s3Uri"], artifact_raw["versionId"])
    ] = manifest_bytes
    objects[
        (signature_raw["s3Uri"], signature_raw["versionId"])
    ] = manifest_signature_bytes
    pair = evidence.ArtifactPair(
        artifact=evidence._validate_reference(
            artifact_raw,
            expected_bucket=BUCKET,
            expected_prefix=PREFIX,
            location="manifest artifact",
        ),
        signature=evidence._validate_reference(
            signature_raw,
            expected_bucket=BUCKET,
            expected_prefix=PREFIX,
            location="manifest signature",
        ),
    )
    return pair, objects


def test_producer_fetches_and_verifies_every_signed_object(
    tmp_path: Path,
) -> None:
    source_pair, objects = _materialized_gate_set()
    fetched: list[tuple[str, str]] = []
    verified: list[tuple[str, str]] = []

    def fetch(
        reference: evidence.S3Reference,
        destination: Path,
        storage_key: str,
        now: datetime,
    ) -> None:
        assert storage_key == STORAGE_KEY
        assert now == NOW
        identity = (reference.uri, reference.version_id)
        destination.write_bytes(objects[identity])
        assert hashlib.sha256(objects[identity]).hexdigest() == reference.sha256
        fetched.append(identity)

    def verify(
        artifact: Path,
        signature: Path,
        signing_key: str,
    ) -> None:
        assert signing_key == SIGNING_KEY
        assert artifact.is_file()
        assert signature.is_file()
        verified.append((artifact.name, signature.name))

    output = tmp_path / "detailed.json"
    compatible = tmp_path / "compatible.json"
    detailed, projection = evidence.produce_reports(
        source_pair=source_pair,
        expected_release=_release(),
        producer=_producer(),
        evidence_bucket=BUCKET,
        evidence_prefix=PREFIX,
        storage_key_arn=STORAGE_KEY,
        signing_key_arn=SIGNING_KEY,
        output=output,
        compatibility_output=compatible,
        now=NOW,
        fetcher=fetch,
        signature_verifier=verify,
        script_checker=lambda _commit, _script: None,
    )

    command_object_count = sum(
        2 * len(evidence.EXPECTED_COMMANDS[gate])
        for gate in evidence.ALL_GATES
    )
    assert len(fetched) == (
        4 + (2 * len(evidence.ALL_GATES)) + command_object_count
    )
    assert len(set(fetched)) == len(fetched)
    assert len(verified) == 2 + len(evidence.ALL_GATES)
    assert json.loads(output.read_text(encoding="utf-8")) == detailed
    assert json.loads(compatible.read_text(encoding="utf-8")) == projection
    assert stat_mode(output) == 0o600
    assert stat_mode(compatible) == 0o600


def test_producer_fails_before_emitting_reports_when_signature_is_invalid(
    tmp_path: Path,
) -> None:
    source_pair, objects = _materialized_gate_set()

    def fetch(
        reference: evidence.S3Reference,
        destination: Path,
        storage_key: str,
        now: datetime,
    ) -> None:
        del storage_key, now
        destination.write_bytes(
            objects[(reference.uri, reference.version_id)]
        )

    def reject_signature(
        artifact: Path,
        signature: Path,
        signing_key: str,
    ) -> None:
        del artifact, signature, signing_key
        raise evidence.LaunchRehearsalError(
            "KMS signature verification failed"
        )

    output = tmp_path / "detailed.json"
    compatible = tmp_path / "compatible.json"
    with pytest.raises(
        evidence.LaunchRehearsalError,
        match="KMS signature verification failed",
    ):
        evidence.produce_reports(
            source_pair=source_pair,
            expected_release=_release(),
            producer=_producer(),
            evidence_bucket=BUCKET,
            evidence_prefix=PREFIX,
            storage_key_arn=STORAGE_KEY,
            signing_key_arn=SIGNING_KEY,
            output=output,
            compatibility_output=compatible,
            now=NOW,
            fetcher=fetch,
            signature_verifier=reject_signature,
            script_checker=lambda _commit, _script: None,
        )

    assert not output.exists()
    assert not compatible.exists()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
