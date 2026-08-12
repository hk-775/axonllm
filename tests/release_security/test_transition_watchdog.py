from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import reconcile_deployment_transition as watchdog  # noqa: E402
import validate_workflows  # noqa: E402


WORKFLOW = ROOT / ".github" / "workflows" / "reconcile-agentcore-production-transition.yml"
JOURNAL_ROOT = "evidence/owner/repo"
BASE = f"{JOURNAL_ROOT}/42/1"
TRANSITION_ID = "b" * 64
INTENT_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
TERMINAL_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-2222-2222-222222222222"
UNAPPROVED_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/33333333-3333-3333-3333-333333333333"
BROKER_VERSION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:AxonLLMProductionTransitionMutationBroker:17"

BASE_NAMES = (
    "promotion.json",
    "promotion-kms-signature.json",
    "transition-recovery-setup.json",
    "transition-recovery-setup-kms-signature.json",
    "transition-recovery-binding.json",
    "transition-recovery-binding-kms-signature.json",
)
COMMIT_NAMES = (
    "agentcore-deployment.json",
    "agentcore-deployment-kms-signature.json",
    "agentcore-deployment-commit.json",
    "agentcore-deployment-commit-kms-signature.json",
)
TERMINAL_NAMES = (
    "transition-terminal.json",
    "transition-terminal-kms-signature.json",
)


def _workflow() -> dict[str, Any]:
    value = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(value, dict)
    return value


def _version_id(key: str) -> str:
    return f"version-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def _version(key: str, *, latest: bool = True) -> dict[str, Any]:
    return {
        "Key": key,
        "VersionId": _version_id(key),
        "IsLatest": latest,
    }


def _listing(*keys: str) -> dict[str, Any]:
    return {
        "Versions": [_version(key) for key in keys],
        "DeleteMarkers": [],
    }


def _keys(
    *,
    commit: bool = False,
    terminal: bool = False,
) -> tuple[str, ...]:
    names = list(BASE_NAMES)
    if commit:
        names.extend(COMMIT_NAMES)
    if terminal:
        names.extend(TERMINAL_NAMES)
    return tuple(f"{BASE}/{name}" for name in names)


def _intent() -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "candidateRuntimeVersion": "7",
        "previousProductionRuntimeVersion": "6",
        "transition": {
            "changeId": "CHG-2026-001",
            "deploymentCommit": "a" * 40,
            "repository": "owner/repo",
            "rollbackNotBefore": "2026-08-11T16:00:00+00:00",
            "runAttempt": "1",
            "runId": "42",
            "transitionId": TRANSITION_ID,
        },
    }


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        evidence_bucket="bucket",
        evidence_prefix="evidence",
        repository="owner/repo",
        intent_signing_key_arn=INTENT_KEY_ARN,
        terminal_signing_key_arn=TERMINAL_KEY_ARN,
        mutation_broker_version_arn=BROKER_VERSION_ARN,
        retain_until=datetime.now(timezone.utc) + timedelta(days=30),
    )


def _broker_result(
    *,
    status: str,
    operation: str,
    phase: str,
    transition_id: str = TRANSITION_ID,
) -> dict[str, str]:
    return {
        "status": status,
        "operation": operation,
        "phase": phase,
        "transitionId": transition_id,
    }


class ListingS3:
    def __init__(self, listing: dict[str, Any]) -> None:
        self.listing = listing
        self.calls: list[dict[str, Any]] = []

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.listing


class LambdaClient:
    def __init__(
        self,
        result: dict[str, Any] | None = None,
        *,
        raw: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.result = result
        self.raw = raw
        self.metadata = metadata or {}
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        raw = self.raw
        if raw is None:
            raw = json.dumps(self.result, separators=(",", ":")).encode()
        response: dict[str, Any] = {
            "StatusCode": 200,
            "ExecutedVersion": "17",
            "Payload": BytesIO(raw),
        }
        response.update(self.metadata)
        return response


def _bundle(key_arn: str) -> dict[str, Any]:
    return {
        "schema": watchdog.kms_evidence.BUNDLE_SCHEMA,
        "artifact": {
            "sha256": "0" * 64,
            "size": 1,
        },
        "signature": {
            "keyArn": key_arn,
            "messageType": "DIGEST",
            "signingAlgorithm": "ECDSA_SHA_256",
            "value": base64.b64encode(b"signature").decode(),
        },
    }


def test_watchdog_workflow_remains_protected_and_serialized() -> None:
    workflow = _workflow()
    assert set(workflow["on"]) == {
        "workflow_run",
        "schedule",
        "workflow_dispatch",
    }
    assert workflow["concurrency"] == {
        "group": "agentcore-production-deployment",
        "cancel-in-progress": "false",
    }
    job = workflow["jobs"]["reconcile"]
    assert job["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert job["runs-on"] == {
        "group": "axonllm-production",
        "labels": "axonllm-production-allowlisted",
    }
    assert validate_workflows.validate_workflow(WORKFLOW) == 3


def test_journal_selects_one_nonterminal_transition() -> None:
    assert watchdog._journal_base(
        _listing(*_keys()),
        journal_root=JOURNAL_ROOT,
    ) == (BASE, "42", "1")


def test_journal_ignores_a_terminal_transition() -> None:
    assert (
        watchdog._journal_base(
            _listing(*_keys(terminal=True)),
            journal_root=JOURNAL_ROOT,
        )
        is None
    )


def test_journal_listing_follows_version_pagination() -> None:
    calls: list[dict[str, Any]] = []

    class PaginatedS3:
        def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "Versions": [_version(_keys()[0])],
                    "DeleteMarkers": [],
                    "IsTruncated": True,
                    "NextKeyMarker": "next-key",
                    "NextVersionIdMarker": "next-version",
                }
            return {
                "Versions": [_version(key) for key in _keys()[1:]],
                "DeleteMarkers": [],
                "IsTruncated": False,
            }

    listing = watchdog._list_journal_versions(
        PaginatedS3(),
        bucket="bucket",
        journal_root=JOURNAL_ROOT,
    )

    assert calls == [
        {
            "Bucket": "bucket",
            "Prefix": f"{JOURNAL_ROOT}/",
        },
        {
            "Bucket": "bucket",
            "Prefix": f"{JOURNAL_ROOT}/",
            "KeyMarker": "next-key",
            "VersionIdMarker": "next-version",
        },
    ]
    assert watchdog._journal_base(
        listing,
        journal_root=JOURNAL_ROOT,
    ) == (BASE, "42", "1")


@pytest.mark.parametrize(
    "hazard",
    [
        "duplicate-version",
        "non-latest",
        "delete-marker",
        "invalid-version-id",
        "outside-prefix",
    ],
)
def test_journal_version_index_fails_closed(hazard: str) -> None:
    listing = _listing(*_keys())
    if hazard == "duplicate-version":
        listing["Versions"].append(_version(_keys()[0]))
    elif hazard == "non-latest":
        listing["Versions"][0]["IsLatest"] = False
    elif hazard == "delete-marker":
        listing["DeleteMarkers"].append(
            {
                "Key": _keys()[0],
                "VersionId": "delete-version",
                "IsLatest": True,
            }
        )
    elif hazard == "invalid-version-id":
        listing["Versions"][0]["VersionId"] = " version"
    else:
        listing["Versions"][0]["Key"] = "different/root/promotion.json"

    with pytest.raises(watchdog.ReconciliationError):
        watchdog._journal_versions(
            listing,
            journal_root=JOURNAL_ROOT,
        )


def test_multiple_nonterminal_transitions_fail_closed() -> None:
    other = f"{JOURNAL_ROOT}/43/1"
    listing = _listing(
        *_keys(),
        *(f"{other}/{name}" for name in BASE_NAMES),
    )

    with pytest.raises(
        watchdog.ReconciliationError,
        match="multiple nonterminal",
    ):
        watchdog._journal_base(
            listing,
            journal_root=JOURNAL_ROOT,
        )


def test_broker_event_contains_only_exact_base_versions() -> None:
    versions = watchdog._journal_versions(
        _listing(*_keys()),
        journal_root=JOURNAL_ROOT,
    )

    event = watchdog._build_broker_event(
        versions,
        base=BASE,
        repository="owner/repo",
        run_id="42",
        run_attempt="1",
    )

    assert event == {
        "repository": "owner/repo",
        "runId": "42",
        "runAttempt": "1",
        "intentVersionId": _version_id(f"{BASE}/promotion.json"),
        "intentSignatureVersionId": _version_id(f"{BASE}/promotion-kms-signature.json"),
        "recoverySetupVersionId": _version_id(f"{BASE}/transition-recovery-setup.json"),
        "recoverySetupSignatureVersionId": _version_id(f"{BASE}/transition-recovery-setup-kms-signature.json"),
        "recoveryBindingVersionId": _version_id(f"{BASE}/transition-recovery-binding.json"),
        "recoveryBindingSignatureVersionId": _version_id(f"{BASE}/transition-recovery-binding-kms-signature.json"),
    }


def test_broker_event_includes_all_four_commit_versions() -> None:
    versions = watchdog._journal_versions(
        _listing(*_keys(commit=True)),
        journal_root=JOURNAL_ROOT,
    )

    event = watchdog._build_broker_event(
        versions,
        base=BASE,
        repository="owner/repo",
        run_id="42",
        run_attempt="1",
    )

    assert {
        name: event[name]
        for name in (
            "deploymentEvidenceVersionId",
            "deploymentEvidenceSignatureVersionId",
            "deploymentCommitVersionId",
            "deploymentCommitSignatureVersionId",
        )
    } == {
        "deploymentEvidenceVersionId": _version_id(f"{BASE}/agentcore-deployment.json"),
        "deploymentEvidenceSignatureVersionId": _version_id(f"{BASE}/agentcore-deployment-kms-signature.json"),
        "deploymentCommitVersionId": _version_id(f"{BASE}/agentcore-deployment-commit.json"),
        "deploymentCommitSignatureVersionId": _version_id(f"{BASE}/agentcore-deployment-commit-kms-signature.json"),
    }


@pytest.mark.parametrize("missing_name", COMMIT_NAMES)
def test_partial_commit_version_set_never_builds_an_event(
    missing_name: str,
) -> None:
    keys = tuple(key for key in _keys(commit=True) if key != f"{BASE}/{missing_name}")
    versions = watchdog._journal_versions(
        _listing(*keys),
        journal_root=JOURNAL_ROOT,
    )

    with pytest.raises(watchdog.ReconciliationError):
        watchdog._build_broker_event(
            versions,
            base=BASE,
            repository="owner/repo",
            run_id="42",
            run_attempt="1",
        )


def test_unsigned_deployment_evidence_is_not_treated_as_rollback() -> None:
    versions = watchdog._journal_versions(
        _listing(
            *_keys(),
            f"{BASE}/agentcore-deployment.json",
        ),
        journal_root=JOURNAL_ROOT,
    )

    with pytest.raises(
        watchdog.ReconciliationError,
        match="without its commit signature",
    ):
        watchdog._build_broker_event(
            versions,
            base=BASE,
            repository="owner/repo",
            run_id="42",
            run_attempt="1",
        )


@pytest.mark.parametrize(
    "value",
    [
        "AxonLLMProductionTransitionMutationBroker",
        ("arn:aws:lambda:us-east-1:123456789012:function:AxonLLMProductionTransitionMutationBroker"),
        ("arn:aws:lambda:us-east-1:123456789012:function:AxonLLMProductionTransitionMutationBroker:live"),
        ("arn:aws:lambda:us-east-1:123456789012:function:AxonLLMProductionTransitionMutationBroker:$LATEST"),
        ("arn:aws:lambda:us-east-1:123456789012:function:AxonLLMProductionTransitionMutationBroker:0"),
        f" {BROKER_VERSION_ARN}",
    ],
)
def test_mutation_broker_requires_an_exact_numeric_version_arn(
    value: str,
) -> None:
    with pytest.raises(
        watchdog.ReconciliationError,
        match="exact Lambda numeric version ARN",
    ):
        watchdog._mutation_broker_version(value)


def test_broker_invocation_is_synchronous_and_version_pinned() -> None:
    event = {
        "repository": "owner/repo",
        "runId": "42",
        "runAttempt": "1",
    }
    client = LambdaClient(
        _broker_result(
            status="PENDING",
            operation="ROLLBACK",
            phase="RUNTIME_WAIT",
        )
    )

    result = watchdog._invoke_mutation_broker(
        client,
        version_arn=BROKER_VERSION_ARN,
        event=event,
        transition_id=TRANSITION_ID,
        expected_operation="ROLLBACK",
    )

    assert result["status"] == "PENDING"
    assert client.calls == [
        {
            "FunctionName": BROKER_VERSION_ARN,
            "InvocationType": "RequestResponse",
            "Payload": json.dumps(
                event,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        }
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        {"StatusCode": 202},
        {"ExecutedVersion": "16"},
        {"FunctionError": "Unhandled"},
        {"FunctionError": ""},
    ],
)
def test_broker_invocation_metadata_fails_closed(
    metadata: dict[str, Any],
) -> None:
    client = LambdaClient(
        _broker_result(
            status="PENDING",
            operation="ROLLBACK",
            phase="RUNTIME_WAIT",
        ),
        metadata=metadata,
    )

    with pytest.raises(
        watchdog.ReconciliationError,
        match="exact requested version",
    ):
        watchdog._invoke_mutation_broker(
            client,
            version_arn=BROKER_VERSION_ARN,
            event={},
            transition_id=TRANSITION_ID,
            expected_operation="ROLLBACK",
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"[]",
        b'{"status":"PENDING","status":"COMPLETE"}',
        b'{"status":NaN}',
        b" " * (watchdog.MAX_LAMBDA_RESPONSE_BYTES + 1),
    ],
)
def test_broker_response_requires_bounded_strict_json(raw: bytes) -> None:
    client = LambdaClient(raw=raw)

    with pytest.raises(watchdog.ReconciliationError):
        watchdog._invoke_mutation_broker(
            client,
            version_arn=BROKER_VERSION_ARN,
            event={},
            transition_id=TRANSITION_ID,
            expected_operation="ROLLBACK",
        )


@pytest.mark.parametrize(
    "result",
    [
        {
            **_broker_result(
                status="PENDING",
                operation="ROLLBACK",
                phase="RUNTIME_WAIT",
            ),
            "extra": "field",
        },
        _broker_result(
            status="PENDING",
            operation="ROLLBACK",
            phase="RUNTIME_WAIT",
            transition_id="f" * 64,
        ),
        _broker_result(
            status="PENDING",
            operation="FINALIZE",
            phase="RUNTIME_WAIT",
        ),
        _broker_result(
            status="UNKNOWN",
            operation="ROLLBACK",
            phase="RUNTIME_WAIT",
        ),
        _broker_result(
            status="PENDING",
            operation="ROLLBACK",
            phase="UNKNOWN",
        ),
        _broker_result(
            status="PENDING",
            operation="FINALIZE",
            phase="ROLLBACK_NOT_BEFORE",
        ),
        _broker_result(
            status="COMPLETE",
            operation="ROLLBACK",
            phase="RUNTIME_UPDATE",
        ),
    ],
)
def test_broker_response_identity_and_state_fail_closed(
    result: dict[str, Any],
) -> None:
    with pytest.raises(watchdog.ReconciliationError):
        watchdog._invoke_mutation_broker(
            LambdaClient(result),
            version_arn=BROKER_VERSION_ARN,
            event={},
            transition_id=TRANSITION_ID,
            expected_operation="ROLLBACK",
        )


class ExactObjectS3:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls: list[dict[str, Any]] = []
        self.overrides: dict[str, Any] = {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        response: dict[str, Any] = {
            "Key": kwargs["Key"],
            "VersionId": kwargs["VersionId"],
            "ContentLength": len(self.raw),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(self.raw).digest()).decode(),
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": (datetime.now(timezone.utc) + timedelta(days=1)),
            "Body": BytesIO(self.raw),
        }
        response.update(self.overrides)
        return response


def test_exact_version_fetch_uses_and_checks_the_listed_version(
    tmp_path: Path,
) -> None:
    client = ExactObjectS3(b"signed artifact")
    output = tmp_path / "artifact.json"

    watchdog._fetch_exact_version(
        client,
        bucket="bucket",
        key=f"{BASE}/promotion.json",
        version_id="immutable-version",
        output=output,
    )

    assert output.read_bytes() == b"signed artifact"
    assert client.calls == [
        {
            "Bucket": "bucket",
            "Key": f"{BASE}/promotion.json",
            "VersionId": "immutable-version",
            "ChecksumMode": "ENABLED",
        }
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"VersionId": "different-version"},
        {"DeleteMarker": True},
        {"ObjectLockMode": "GOVERNANCE"},
        {"ChecksumSHA256": base64.b64encode(b"x" * 32).decode()},
    ],
)
def test_exact_version_fetch_rejects_substitution(
    tmp_path: Path,
    overrides: dict[str, Any],
) -> None:
    client = ExactObjectS3(b"signed artifact")
    client.overrides = overrides

    with pytest.raises(watchdog.ReconciliationError):
        watchdog._fetch_exact_version(
            client,
            bucket="bucket",
            key=f"{BASE}/promotion.json",
            version_id="immutable-version",
            output=tmp_path / "artifact.json",
        )


def _install_reconcile_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pair_calls: list[dict[str, Any]],
) -> None:
    def require_pair(
        _client: Any,
        *,
        artifact_name: str,
        signature_name: str,
        artifact_version: str,
        signature_version: str,
        directory: Path,
        signing_key_arn: str,
        **_kwargs: Any,
    ) -> tuple[Path, Path]:
        artifact = directory / artifact_name
        signature = directory / signature_name
        value = _intent() if artifact_name == "promotion.json" else {}
        artifact.write_text(json.dumps(value), encoding="utf-8")
        signature.write_text("signature", encoding="utf-8")
        pair_calls.append(
            {
                "artifact": artifact_name,
                "artifact_version": artifact_version,
                "signature_version": signature_version,
                "key": signing_key_arn,
            }
        )
        return artifact, signature

    monkeypatch.setattr(watchdog, "_require_pair", require_pair)
    monkeypatch.setattr(
        watchdog.deployment_transition,
        "verify_recovery_binding",
        lambda _args: {},
    )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("ROLLBACK_NOT_BEFORE", "deferred"),
        ("CONTROL_PLANE_WAIT", "pending"),
        ("RUNTIME_UPDATE", "pending"),
    ],
)
def test_pending_broker_response_never_creates_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected: str,
) -> None:
    pair_calls: list[dict[str, Any]] = []
    _install_reconcile_stubs(monkeypatch, pair_calls=pair_calls)
    monkeypatch.setattr(
        watchdog,
        "_fetch_state",
        lambda *_args, **_kwargs: pytest.fail("terminal state must not be read while broker is pending"),
    )
    monkeypatch.setattr(
        watchdog,
        "_put_locked",
        lambda *_args, **_kwargs: pytest.fail("terminal must not be written while broker is pending"),
    )
    monkeypatch.setattr(
        watchdog.kms_evidence,
        "sign_artifact",
        lambda *_args, **_kwargs: pytest.fail("terminal must not be signed while broker is pending"),
    )
    client = LambdaClient(
        _broker_result(
            status="PENDING",
            operation="ROLLBACK",
            phase=phase,
        )
    )

    result = watchdog.reconcile(
        _args(),
        s3_client=ListingS3(_listing(*_keys())),
        lambda_client=client,
    )

    assert result == expected
    assert len(pair_calls) == 3
    assert {call["key"] for call in pair_calls} == {INTENT_KEY_ARN}
    event = json.loads(client.calls[0]["Payload"])
    assert "deploymentCommitSignatureVersionId" not in event


def test_complete_rollback_signs_only_with_terminal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_calls: list[dict[str, Any]] = []
    _install_reconcile_stubs(monkeypatch, pair_calls=pair_calls)
    fetched: list[str] = []

    def fetch_state(
        _client: Any,
        *,
        key: str,
        **_kwargs: Any,
    ) -> str:
        fetched.append(key)
        return watchdog.deployment_transition.S3_ABSENT

    writes: list[tuple[str, bytes]] = []
    signed_with: list[str] = []

    def put_locked(
        _client: Any,
        *,
        key: str,
        path: Path,
        **_kwargs: Any,
    ) -> None:
        writes.append((key, path.read_bytes()))

    def sign(
        _artifact: Path,
        bundle: Path,
        key_arn: str,
    ) -> dict[str, Any]:
        signed_with.append(key_arn)
        value = _bundle(key_arn)
        bundle.write_text(json.dumps(value), encoding="utf-8")
        return value

    monkeypatch.setattr(watchdog, "_fetch_state", fetch_state)
    monkeypatch.setattr(watchdog, "_put_locked", put_locked)
    monkeypatch.setattr(watchdog.kms_evidence, "sign_artifact", sign)
    monkeypatch.setattr(
        watchdog.kms_evidence,
        "verify_artifact",
        lambda *_args, **_kwargs: None,
    )

    result = watchdog.reconcile(
        _args(),
        s3_client=ListingS3(_listing(*_keys())),
        lambda_client=LambdaClient(
            _broker_result(
                status="COMPLETE",
                operation="ROLLBACK",
                phase="COMPLETE",
            )
        ),
    )

    assert result == "rolled-back"
    assert signed_with == [TERMINAL_KEY_ARN]
    assert {call["key"] for call in pair_calls} == {INTENT_KEY_ARN}
    assert fetched == [
        f"{BASE}/transition-terminal.json",
        f"{BASE}/transition-terminal-kms-signature.json",
    ]
    assert [key for key, _raw in writes] == [
        f"{BASE}/transition-terminal.json",
        f"{BASE}/transition-terminal-kms-signature.json",
    ]
    terminal = json.loads(writes[0][1])
    assert terminal["outcome"] == "rolled-back"
    assert terminal["intentSha256"] == hashlib.sha256(json.dumps(_intent()).encode()).hexdigest()


def test_complete_finalize_passes_all_commit_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_calls: list[dict[str, Any]] = []
    _install_reconcile_stubs(monkeypatch, pair_calls=pair_calls)
    verified_commits: list[tuple[str, str, str]] = []

    def verify_commit(
        _evidence: Path,
        _signature: Path,
        _commit: Path,
        *,
        repository: str,
        run_id: str,
        run_attempt: str,
    ) -> None:
        verified_commits.append((repository, run_id, run_attempt))

    monkeypatch.setattr(watchdog, "_verify_deployment_commit", verify_commit)
    monkeypatch.setattr(
        watchdog,
        "_fetch_state",
        lambda *_args, **_kwargs: watchdog.deployment_transition.S3_ABSENT,
    )
    monkeypatch.setattr(
        watchdog,
        "_put_locked",
        lambda *_args, **_kwargs: None,
    )

    def sign(
        _artifact: Path,
        bundle: Path,
        key_arn: str,
    ) -> dict[str, Any]:
        value = _bundle(key_arn)
        bundle.write_text(json.dumps(value), encoding="utf-8")
        return value

    monkeypatch.setattr(watchdog.kms_evidence, "sign_artifact", sign)
    monkeypatch.setattr(
        watchdog.kms_evidence,
        "verify_artifact",
        lambda *_args, **_kwargs: None,
    )
    client = LambdaClient(
        _broker_result(
            status="COMPLETE",
            operation="FINALIZE",
            phase="COMPLETE",
        )
    )

    result = watchdog.reconcile(
        _args(),
        s3_client=ListingS3(_listing(*_keys(commit=True))),
        lambda_client=client,
    )

    assert result == "committed"
    assert verified_commits == [("owner/repo", "42", "1")]
    assert len(pair_calls) == 5
    assert {call["key"] for call in pair_calls} == {INTENT_KEY_ARN}
    event = json.loads(client.calls[0]["Payload"])
    assert all(field in event for field, _name in watchdog._COMMIT_EVENT_OBJECTS)


def test_partial_commit_fails_before_lambda_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watchdog,
        "_require_pair",
        lambda *_args, **_kwargs: pytest.fail("artifacts must not be fetched for a partial commit"),
    )
    client = LambdaClient(
        _broker_result(
            status="COMPLETE",
            operation="FINALIZE",
            phase="COMPLETE",
        )
    )
    partial_keys = tuple(key for key in _keys(commit=True) if not key.endswith("/agentcore-deployment.json"))

    with pytest.raises(
        watchdog.ReconciliationError,
        match="partial or ambiguous",
    ):
        watchdog.reconcile(
            _args(),
            s3_client=ListingS3(_listing(*partial_keys)),
            lambda_client=client,
        )

    assert client.calls == []


def test_reconciliation_requires_distinct_signing_keys() -> None:
    args = _args()
    args.terminal_signing_key_arn = args.intent_signing_key_arn
    client = LambdaClient(
        _broker_result(
            status="PENDING",
            operation="ROLLBACK",
            phase="RUNTIME_WAIT",
        )
    )

    with pytest.raises(
        watchdog.ReconciliationError,
        match="must be distinct",
    ):
        watchdog.reconcile(
            args,
            s3_client=ListingS3(_listing(*_keys())),
            lambda_client=client,
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "declared_key",
    [INTENT_KEY_ARN, TERMINAL_KEY_ARN],
)
def test_historical_terminal_audit_selects_declared_allowed_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    declared_key: str,
) -> None:
    del tmp_path
    versions = watchdog._journal_versions(
        _listing(*_keys(terminal=True)),
        journal_root=JOURNAL_ROOT,
    )

    def require_intent(
        _client: Any,
        *,
        directory: Path,
        signing_key_arn: str,
        artifact_name: str,
        signature_name: str,
        **_kwargs: Any,
    ) -> tuple[Path, Path]:
        assert signing_key_arn == INTENT_KEY_ARN
        intent = directory / artifact_name
        signature = directory / signature_name
        intent.write_text(json.dumps(_intent()), encoding="utf-8")
        signature.write_text("signature", encoding="utf-8")
        return intent, signature

    def fetch_terminal(
        _client: Any,
        *,
        directory: Path,
        artifact_name: str,
        signature_name: str,
        **_kwargs: Any,
    ) -> tuple[Path, Path]:
        terminal = directory / artifact_name
        bundle = directory / signature_name
        record = watchdog.deployment_transition.create_record(
            argparse.Namespace(
                intent=directory / "promotion.json",
                output=terminal,
                outcome="committed",
                repository="owner/repo",
                run_id="42",
                run_attempt="1",
            )
        )
        terminal.write_text(json.dumps(record), encoding="utf-8")
        bundle.write_text(
            json.dumps(_bundle(declared_key)),
            encoding="utf-8",
        )
        return terminal, bundle

    verified_keys: list[str] = []
    monkeypatch.setattr(watchdog, "_require_pair", require_intent)
    monkeypatch.setattr(watchdog, "_fetch_pair", fetch_terminal)
    monkeypatch.setattr(
        watchdog.kms_evidence,
        "verify_artifact",
        lambda _artifact, _bundle_path, key: verified_keys.append(key),
    )

    watchdog._verify_terminal_pairs(
        object(),
        versions=versions,
        bucket="bucket",
        journal_root=JOURNAL_ROOT,
        repository="owner/repo",
        intent_signing_key_arn=INTENT_KEY_ARN,
        terminal_signing_key_arn=TERMINAL_KEY_ARN,
    )

    assert verified_keys == [declared_key]


def test_historical_terminal_audit_rejects_unapproved_declared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = watchdog._journal_versions(
        _listing(*_keys(terminal=True)),
        journal_root=JOURNAL_ROOT,
    )

    def require_intent(
        _client: Any,
        *,
        directory: Path,
        artifact_name: str,
        signature_name: str,
        **_kwargs: Any,
    ) -> tuple[Path, Path]:
        intent = directory / artifact_name
        signature = directory / signature_name
        intent.write_text(json.dumps(_intent()), encoding="utf-8")
        signature.write_text("signature", encoding="utf-8")
        return intent, signature

    def fetch_terminal(
        _client: Any,
        *,
        directory: Path,
        artifact_name: str,
        signature_name: str,
        **_kwargs: Any,
    ) -> tuple[Path, Path]:
        terminal = directory / artifact_name
        bundle = directory / signature_name
        terminal.write_text("{}", encoding="utf-8")
        bundle.write_text(
            json.dumps(_bundle(UNAPPROVED_KEY_ARN)),
            encoding="utf-8",
        )
        return terminal, bundle

    verified: list[str] = []
    monkeypatch.setattr(watchdog, "_require_pair", require_intent)
    monkeypatch.setattr(watchdog, "_fetch_pair", fetch_terminal)
    monkeypatch.setattr(
        watchdog.kms_evidence,
        "verify_artifact",
        lambda _artifact, _bundle_path, key: verified.append(key),
    )

    with pytest.raises(
        watchdog.ReconciliationError,
        match="unapproved key",
    ):
        watchdog._verify_terminal_pairs(
            object(),
            versions=versions,
            bucket="bucket",
            journal_root=JOURNAL_ROOT,
            repository="owner/repo",
            intent_signing_key_arn=INTENT_KEY_ARN,
            terminal_signing_key_arn=TERMINAL_KEY_ARN,
        )

    assert verified == []


def test_terminal_writes_remain_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b'{"terminal":"record"}\n'
    path = tmp_path / "transition-terminal.json"
    path.write_bytes(raw)
    puts: list[dict[str, Any]] = []

    class S3:
        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            body = kwargs.pop("Body")
            puts.append({**kwargs, "Body": body.read()})
            return {"VersionId": "terminal-version"}

    def fetch_state(
        _client: Any,
        *,
        output: Path,
        **_kwargs: Any,
    ) -> str:
        output.write_bytes(raw)
        return watchdog.deployment_transition.S3_PRESENT

    monkeypatch.setattr(watchdog, "_fetch_state", fetch_state)
    retain_until = datetime.now(timezone.utc) + timedelta(days=30)

    watchdog._put_locked(
        S3(),
        bucket="bucket",
        key=f"{BASE}/transition-terminal.json",
        path=path,
        retain_until=retain_until,
    )

    assert puts == [
        {
            "Bucket": "bucket",
            "Key": f"{BASE}/transition-terminal.json",
            "Body": raw,
            "ChecksumAlgorithm": "SHA256",
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": retain_until,
            "IfNoneMatch": "*",
        }
    ]


def test_parser_requires_split_keys_and_exact_broker_version() -> None:
    retain_until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    args = watchdog._parser().parse_args(
        [
            "--evidence-bucket",
            "bucket",
            "--evidence-prefix",
            "evidence",
            "--repository",
            "owner/repo",
            "--intent-signing-key-arn",
            INTENT_KEY_ARN,
            "--terminal-signing-key-arn",
            TERMINAL_KEY_ARN,
            "--mutation-broker-version-arn",
            BROKER_VERSION_ARN,
            "--retain-until",
            retain_until,
        ]
    )

    assert args.intent_signing_key_arn == INTENT_KEY_ARN
    assert args.terminal_signing_key_arn == TERMINAL_KEY_ARN
    assert args.mutation_broker_version_arn == BROKER_VERSION_ARN
