#!/usr/bin/env python3
"""Reconcile one signed, nonterminal AgentCore production transition."""

from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence

import deployment_evidence
import deployment_transition
import kms_evidence


class ReconciliationError(RuntimeError):
    """Raised when a transition cannot be reconciled safely."""


MAX_LAMBDA_RESPONSE_BYTES = 16 * 1024
MAX_VERSIONED_OBJECT_BYTES = 16 * 1024 * 1024
LAMBDA_VERSION_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):lambda:"
    r"(?P<region>[a-z]{2}(?:-gov)?-[a-z]+-[1-9][0-9]*):"
    r"(?P<account>[0-9]{12}):function:"
    r"(?P<name>[A-Za-z0-9_-]{1,64}):(?P<version>[1-9][0-9]*)$"
)

_BASE_EVENT_OBJECTS = (
    ("intentVersionId", "promotion.json"),
    ("intentSignatureVersionId", "promotion-kms-signature.json"),
    ("recoverySetupVersionId", "transition-recovery-setup.json"),
    (
        "recoverySetupSignatureVersionId",
        "transition-recovery-setup-kms-signature.json",
    ),
    ("recoveryBindingVersionId", "transition-recovery-binding.json"),
    (
        "recoveryBindingSignatureVersionId",
        "transition-recovery-binding-kms-signature.json",
    ),
)
_COMMIT_EVENT_OBJECTS = (
    ("deploymentEvidenceVersionId", "agentcore-deployment.json"),
    (
        "deploymentEvidenceSignatureVersionId",
        "agentcore-deployment-kms-signature.json",
    ),
    ("deploymentCommitVersionId", "agentcore-deployment-commit.json"),
    (
        "deploymentCommitSignatureVersionId",
        "agentcore-deployment-commit-kms-signature.json",
    ),
)
_PENDING_PHASES = {
    "FINALIZE": frozenset(
        {
            "CONTROL_PLANE_WAIT",
            "RUNTIME_WAIT",
            "RUNTIME_UPDATE",
        }
    ),
    "ROLLBACK": frozenset(
        {
            "ROLLBACK_NOT_BEFORE",
            "CONTROL_PLANE_WAIT",
            "CONTROL_PLANE_RESTORE",
            "CONTROL_PLANE_DELETE_PROTECTION",
            "CONTROL_PLANE_DELETE",
            "RUNTIME_WAIT",
            "RUNTIME_UPDATE",
        }
    ),
}


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReconciliationError(f"{location} must be a JSON object")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value, _ = deployment_transition._read_json(path)
    return value


def _unique_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ReconciliationError(f"duplicate JSON field: {name}")
        value[name] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ReconciliationError(f"invalid JSON constant: {value}")


def _strict_json(raw: bytes, location: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ReconciliationError,
        RecursionError,
    ) as exc:
        raise ReconciliationError(f"{location} is not strict UTF-8 JSON") from exc
    if type(value) is not dict:
        raise ReconciliationError(f"{location} must be a JSON object")
    return value


def _version_id(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value != value.strip()
        or any(ord(character) < 0x21 for character in value)
    ):
        raise ReconciliationError(f"{location} has an invalid S3 VersionId")
    return value


def _journal_versions(
    listing: Mapping[str, Any],
    *,
    journal_root: str,
) -> dict[str, str]:
    raw_versions = listing.get("Versions", [])
    markers = listing.get("DeleteMarkers", [])
    if not isinstance(raw_versions, list) or not isinstance(markers, list):
        raise ReconciliationError("S3 version listing is malformed")
    prefix = f"{journal_root}/"
    for raw in markers:
        marker = _object(raw, "S3 delete marker")
        key = marker.get("Key")
        if not isinstance(key, str) or not key.startswith(prefix):
            raise ReconciliationError("S3 delete marker is outside the requested journal")
        raise ReconciliationError("transition journal contains a delete marker")

    versions: dict[str, str] = {}
    for raw in raw_versions:
        version = _object(raw, "S3 object version")
        key = version.get("Key")
        if not isinstance(key, str) or not key.startswith(prefix):
            raise ReconciliationError("S3 object version is outside the requested journal")
        if key in versions:
            raise ReconciliationError("transition journal contains multiple object versions")
        if version.get("IsLatest") is not True:
            raise ReconciliationError("transition journal contains a non-latest object version")
        versions[key] = _version_id(
            version.get("VersionId"),
            f"S3 object version {key}",
        )
    return versions


def _journal_base_from_versions(
    versions: Mapping[str, str],
    *,
    journal_root: str,
) -> tuple[str, str, str] | None:
    latest = set(versions)
    intent_keys = sorted(key for key in latest if key.endswith("/promotion.json"))
    unresolved: list[str] = []
    for intent_key in intent_keys:
        base = intent_key.removesuffix("/promotion.json")
        terminal = f"{base}/transition-terminal.json"
        terminal_bundle = f"{base}/transition-terminal-kms-signature.json"
        if terminal_bundle in latest and terminal not in latest:
            raise ReconciliationError("transition terminal signature exists without its record")
        if terminal_bundle not in latest:
            unresolved.append(base)
    if len(unresolved) > 1:
        raise ReconciliationError("multiple nonterminal production transitions require incident review")
    if not unresolved:
        return None
    base = unresolved[0]
    relative = base.removeprefix(f"{journal_root}/")
    parts = relative.split("/")
    if (
        len(parts) != 2
        or deployment_transition.RUN_ID.fullmatch(parts[0]) is None
        or deployment_transition.RUN_ID.fullmatch(parts[1]) is None
    ):
        raise ReconciliationError("transition journal path has an invalid run identity")
    return base, parts[0], parts[1]


def _journal_base(
    listing: Mapping[str, Any],
    *,
    journal_root: str,
) -> tuple[str, str, str] | None:
    return _journal_base_from_versions(
        _journal_versions(listing, journal_root=journal_root),
        journal_root=journal_root,
    )


def _terminal_bases_from_versions(
    versions: Mapping[str, str],
    *,
    journal_root: str,
) -> tuple[tuple[str, str, str], ...]:
    latest = set(versions)
    result: list[tuple[str, str, str]] = []
    for intent_key in sorted(key for key in latest if key.endswith("/promotion.json")):
        base = intent_key.removesuffix("/promotion.json")
        terminal = f"{base}/transition-terminal.json"
        signature = f"{base}/transition-terminal-kms-signature.json"
        if terminal not in latest or signature not in latest:
            continue
        relative = base.removeprefix(f"{journal_root}/")
        parts = relative.split("/")
        if (
            len(parts) != 2
            or deployment_transition.RUN_ID.fullmatch(parts[0]) is None
            or deployment_transition.RUN_ID.fullmatch(parts[1]) is None
        ):
            raise ReconciliationError("transition journal path has an invalid run identity")
        result.append((base, parts[0], parts[1]))
    return tuple(result)


def _terminal_bases(
    listing: Mapping[str, Any],
    *,
    journal_root: str,
) -> tuple[tuple[str, str, str], ...]:
    return _terminal_bases_from_versions(
        _journal_versions(listing, journal_root=journal_root),
        journal_root=journal_root,
    )


def _verify_terminal_pairs(
    s3_client: Any,
    *,
    versions: Mapping[str, str],
    bucket: str,
    journal_root: str,
    repository: str,
    intent_signing_key_arn: str,
    terminal_signing_key_arn: str,
) -> None:
    allowed_terminal_keys = frozenset(
        {
            intent_signing_key_arn,
            terminal_signing_key_arn,
        }
    )
    with tempfile.TemporaryDirectory(prefix="axonllm-transition-terminal-audit-") as temporary_name:
        directory = Path(temporary_name)
        for index, (base, run_id, run_attempt) in enumerate(
            _terminal_bases_from_versions(
                versions,
                journal_root=journal_root,
            )
        ):
            pair_directory = directory / str(index)
            pair_directory.mkdir(mode=0o700)
            intent, _ = _require_pair(
                s3_client,
                bucket=bucket,
                base=base,
                artifact_name="promotion.json",
                signature_name="promotion-kms-signature.json",
                artifact_version=_required_version(
                    versions,
                    f"{base}/promotion.json",
                ),
                signature_version=_required_version(
                    versions,
                    f"{base}/promotion-kms-signature.json",
                ),
                directory=pair_directory,
                signing_key_arn=intent_signing_key_arn,
            )
            terminal, terminal_bundle = _fetch_pair(
                s3_client,
                bucket=bucket,
                base=base,
                artifact_name="transition-terminal.json",
                signature_name=("transition-terminal-kms-signature.json"),
                artifact_version=_required_version(
                    versions,
                    f"{base}/transition-terminal.json",
                ),
                signature_version=_required_version(
                    versions,
                    f"{base}/transition-terminal-kms-signature.json",
                ),
                directory=pair_directory,
            )
            terminal_key_arn = _declared_bundle_key(
                terminal_bundle,
                allowed_keys=allowed_terminal_keys,
            )
            try:
                kms_evidence.verify_artifact(
                    terminal,
                    terminal_bundle,
                    terminal_key_arn,
                )
            except kms_evidence.KmsEvidenceError as exc:
                raise ReconciliationError("transition signature is invalid: transition-terminal.json") from exc
            try:
                deployment_transition.verify_record(
                    argparse.Namespace(
                        intent=intent,
                        record=terminal,
                        outcome=None,
                        repository=repository,
                        run_id=run_id,
                        run_attempt=run_attempt,
                    )
                )
            except deployment_transition.TransitionRecordError as exc:
                raise ReconciliationError(
                    "terminal transition record is not bound to its signed promotion intent"
                ) from exc


def _list_journal_versions(
    s3_client: Any,
    *,
    bucket: str,
    journal_root: str,
) -> dict[str, list[Any]]:
    listing: dict[str, list[Any]] = {
        "Versions": [],
        "DeleteMarkers": [],
    }
    key_marker: str | None = None
    version_marker: str | None = None
    seen_markers: set[tuple[str, str | None]] = set()
    for _ in range(100):
        request: dict[str, str] = {
            "Bucket": bucket,
            "Prefix": f"{journal_root}/",
        }
        if key_marker is not None:
            request["KeyMarker"] = key_marker
        if version_marker is not None:
            request["VersionIdMarker"] = version_marker
        try:
            page = s3_client.list_object_versions(**request)
        except Exception as exc:
            raise ReconciliationError("cannot list the production transition journal") from exc
        if not isinstance(page, dict):
            raise ReconciliationError("S3 version listing is malformed")
        for field in ("Versions", "DeleteMarkers"):
            values = page.get(field, [])
            if not isinstance(values, list):
                raise ReconciliationError("S3 version listing is malformed")
            listing[field].extend(values)
        if page.get("IsTruncated") is not True:
            return listing
        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if (
            not isinstance(next_key, str)
            or not next_key
            or next_version is not None
            and (not isinstance(next_version, str) or not next_version)
        ):
            raise ReconciliationError("S3 version listing pagination is malformed")
        marker = (next_key, next_version)
        if marker in seen_markers:
            raise ReconciliationError("S3 version listing pagination repeated a marker")
        seen_markers.add(marker)
        key_marker, version_marker = marker
    raise ReconciliationError("production transition journal exceeds the reviewable page limit")


def _fetch_state(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    output: Path,
) -> str:
    state = deployment_transition.fetch_s3_object(
        argparse.Namespace(bucket=bucket, key=key, output=output),
        s3_client=s3_client,
    )
    if state not in {
        deployment_transition.S3_PRESENT,
        deployment_transition.S3_ABSENT,
        deployment_transition.S3_INDETERMINATE,
    }:
        raise ReconciliationError(f"unexpected S3 state for {key}: {state}")
    return state


def _retention_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fetch_exact_version(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
    output: Path,
) -> None:
    try:
        output.unlink(missing_ok=True)
        response = s3_client.get_object(
            Bucket=bucket,
            Key=key,
            VersionId=version_id,
            ChecksumMode="ENABLED",
        )
    except Exception as exc:
        raise ReconciliationError(f"cannot fetch exact transition object version: {key}") from exc
    if not isinstance(response, dict):
        raise ReconciliationError(f"S3 returned malformed transition object metadata: {key}")
    retain_until = _retention_time(response.get("ObjectLockRetainUntilDate"))
    response_key = response.get("Key")
    if (
        response.get("DeleteMarker") is True
        or response.get("VersionId") != version_id
        or response_key is not None
        and response_key != key
        or response.get("ObjectLockMode") != "COMPLIANCE"
        or retain_until is None
        or retain_until <= datetime.now(timezone.utc)
    ):
        raise ReconciliationError(f"transition object version is not immutable or exact: {key}")
    length = response.get("ContentLength")
    checksum = response.get("ChecksumSHA256")
    body = response.get("Body")
    if (
        not isinstance(length, int)
        or isinstance(length, bool)
        or length < 0
        or length > MAX_VERSIONED_OBJECT_BYTES
        or not isinstance(checksum, str)
        or not checksum
        or body is None
        or not callable(getattr(body, "read", None))
    ):
        raise ReconciliationError(f"transition object metadata is malformed: {key}")
    try:
        expected_checksum = base64.b64decode(checksum, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReconciliationError(f"transition object checksum is malformed: {key}") from exc
    if not expected_checksum or base64.b64encode(expected_checksum).decode("ascii") != checksum:
        raise ReconciliationError(f"transition object checksum is malformed: {key}")
    try:
        raw = body.read(MAX_VERSIONED_OBJECT_BYTES + 1)
    except Exception as exc:
        raise ReconciliationError(f"cannot read exact transition object version: {key}") from exc
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if (
        not isinstance(raw, bytes)
        or len(raw) != length
        or len(raw) > MAX_VERSIONED_OBJECT_BYTES
        or not hashlib.sha256(raw).digest() == expected_checksum
    ):
        raise ReconciliationError(f"transition object checksum or length is invalid: {key}")
    try:
        deployment_transition._atomic_write_bytes(output, raw)
    except deployment_transition.TransitionRecordError as exc:
        raise ReconciliationError(f"cannot persist exact transition object version: {key}") from exc


def _fetch_pair(
    s3_client: Any,
    *,
    bucket: str,
    base: str,
    artifact_name: str,
    signature_name: str,
    artifact_version: str,
    signature_version: str,
    directory: Path,
) -> tuple[Path, Path]:
    artifact = directory / artifact_name
    signature = directory / signature_name
    _fetch_exact_version(
        s3_client,
        bucket=bucket,
        key=f"{base}/{artifact_name}",
        version_id=artifact_version,
        output=artifact,
    )
    _fetch_exact_version(
        s3_client,
        bucket=bucket,
        key=f"{base}/{signature_name}",
        version_id=signature_version,
        output=signature,
    )
    return artifact, signature


def _require_pair(
    s3_client: Any,
    *,
    bucket: str,
    base: str,
    artifact_name: str,
    signature_name: str,
    artifact_version: str,
    signature_version: str,
    directory: Path,
    signing_key_arn: str,
) -> tuple[Path, Path]:
    artifact, signature = _fetch_pair(
        s3_client,
        bucket=bucket,
        base=base,
        artifact_name=artifact_name,
        signature_name=signature_name,
        artifact_version=artifact_version,
        signature_version=signature_version,
        directory=directory,
    )
    try:
        kms_evidence.verify_artifact(
            artifact,
            signature,
            signing_key_arn,
        )
    except kms_evidence.KmsEvidenceError as exc:
        raise ReconciliationError(f"transition signature is invalid: {artifact_name}") from exc
    return artifact, signature


def _declared_bundle_key(
    bundle: Path,
    *,
    allowed_keys: frozenset[str],
) -> str:
    try:
        raw = bundle.read_bytes()
    except OSError as exc:
        raise ReconciliationError("cannot read terminal signature bundle") from exc
    if len(raw) > kms_evidence.MAX_BUNDLE_BYTES:
        raise ReconciliationError("terminal signature bundle is too large")
    value = _strict_json(raw, "terminal signature bundle")
    signature = value.get("signature")
    key_arn = signature.get("keyArn") if isinstance(signature, dict) else None
    if not isinstance(key_arn, str) or key_arn not in allowed_keys:
        raise ReconciliationError("terminal signature bundle declares an unapproved key")
    return key_arn


def _put_locked(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    path: Path,
    retain_until: datetime,
) -> None:
    try:
        with path.open("rb") as body:
            s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ChecksumAlgorithm="SHA256",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
                IfNoneMatch="*",
            )
    except Exception as exc:
        raise ReconciliationError(f"cannot append locked transition object: {key}") from exc
    readback = path.with_name(f".readback-{path.name}")
    state = _fetch_state(
        s3_client,
        bucket=bucket,
        key=key,
        output=readback,
    )
    if state != deployment_transition.S3_PRESENT or readback.read_bytes() != path.read_bytes():
        raise ReconciliationError(f"locked transition object did not read back exactly: {key}")


def _verify_deployment_commit(
    evidence: Path,
    evidence_signature: Path,
    commit_record: Path,
    *,
    repository: str,
    run_id: str,
    run_attempt: str,
) -> None:
    value = _read_json(evidence)
    deployment = _object(value.get("deployment"), "deployment identity")
    release = _object(value.get("release"), "release identity")
    images = _object(value.get("images"), "deployment images")
    agentcore = _object(images.get("agentcore"), "AgentCore image")
    control = _object(images.get("controlPlane"), "control-plane image")
    deployment_evidence.verify_commit_record(
        argparse.Namespace(
            evidence=evidence,
            evidence_signature=evidence_signature,
            commit_record=commit_record,
            repository=repository,
            deployment_commit=deployment.get("commit"),
            release_commit=release.get("commit"),
            run_id=run_id,
            run_attempt=run_attempt,
            agentcore_image=agentcore.get("reference"),
            fargate_image=control.get("reference"),
        )
    )


def _required_version(
    versions: Mapping[str, str],
    key: str,
) -> str:
    try:
        return versions[key]
    except KeyError as exc:
        raise ReconciliationError(f"required transition object is absent: {key}") from exc


def _build_broker_event(
    versions: Mapping[str, str],
    *,
    base: str,
    repository: str,
    run_id: str,
    run_attempt: str,
) -> dict[str, str]:
    event = {
        "repository": repository,
        "runId": run_id,
        "runAttempt": run_attempt,
    }
    for field, name in _BASE_EVENT_OBJECTS:
        event[field] = _required_version(versions, f"{base}/{name}")

    commit_keys = {field: f"{base}/{name}" for field, name in _COMMIT_EVENT_OBJECTS}
    present = {field for field, key in commit_keys.items() if key in versions}
    signal_field = "deploymentCommitSignatureVersionId"
    if signal_field in present:
        if present != set(commit_keys):
            raise ReconciliationError("committed deployment evidence has partial or ambiguous versions")
        event.update({field: _required_version(versions, key) for field, key in commit_keys.items()})
    elif present:
        raise ReconciliationError("deployment evidence exists without its commit signature")
    return event


def _mutation_broker_version(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or value != value.strip():
        raise ReconciliationError("mutation broker must be an exact Lambda numeric version ARN")
    match = LAMBDA_VERSION_ARN.fullmatch(value)
    if match is None:
        raise ReconciliationError("mutation broker must be an exact Lambda numeric version ARN")
    return value, match.group("version")


def _lambda_payload(response: Mapping[str, Any]) -> bytes:
    payload = response.get("Payload")
    if payload is None or not callable(getattr(payload, "read", None)):
        raise ReconciliationError("mutation broker response has no readable payload")
    try:
        raw = payload.read(MAX_LAMBDA_RESPONSE_BYTES + 1)
    except Exception as exc:
        raise ReconciliationError("cannot read mutation broker response") from exc
    finally:
        close = getattr(payload, "close", None)
        if callable(close):
            close()
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_LAMBDA_RESPONSE_BYTES:
        raise ReconciliationError("mutation broker response is empty or too large")
    return raw


def _invoke_mutation_broker(
    lambda_client: Any,
    *,
    version_arn: str,
    event: Mapping[str, str],
    transition_id: str,
    expected_operation: str,
) -> dict[str, str]:
    exact_arn, expected_version = _mutation_broker_version(version_arn)
    encoded = json.dumps(
        dict(event),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        response = lambda_client.invoke(
            FunctionName=exact_arn,
            InvocationType="RequestResponse",
            Payload=encoded,
        )
    except Exception as exc:
        raise ReconciliationError("cannot invoke production mutation broker") from exc
    if not isinstance(response, dict):
        raise ReconciliationError("mutation broker returned malformed invocation metadata")
    if (
        response.get("StatusCode") != 200
        or isinstance(response.get("StatusCode"), bool)
        or response.get("ExecutedVersion") != expected_version
        or "FunctionError" in response
    ):
        raise ReconciliationError("mutation broker invocation did not execute the exact requested version")
    result = _strict_json(
        _lambda_payload(response),
        "mutation broker response",
    )
    expected_fields = {
        "status",
        "operation",
        "phase",
        "transitionId",
    }
    if set(result) != expected_fields or any(not isinstance(result.get(name), str) for name in expected_fields):
        raise ReconciliationError("mutation broker response fields are invalid")
    if (
        result["transitionId"] != transition_id
        or deployment_transition.TRANSITION_ID.fullmatch(result["transitionId"]) is None
        or result["operation"] != expected_operation
    ):
        raise ReconciliationError("mutation broker response identity is invalid")
    status = result["status"]
    phase = result["phase"]
    if status == "PENDING":
        if phase not in _PENDING_PHASES[expected_operation]:
            raise ReconciliationError("mutation broker pending phase is invalid")
    elif status == "COMPLETE":
        if phase != "COMPLETE":
            raise ReconciliationError("mutation broker completion phase is invalid")
    else:
        raise ReconciliationError("mutation broker status is invalid")
    return {
        "status": status,
        "operation": result["operation"],
        "phase": phase,
        "transitionId": result["transitionId"],
    }


def reconcile(
    args: argparse.Namespace,
    *,
    s3_client: Any,
    lambda_client: Any,
) -> str:
    if args.intent_signing_key_arn == args.terminal_signing_key_arn:
        raise ReconciliationError("intent and terminal signing keys must be distinct")
    _mutation_broker_version(args.mutation_broker_version_arn)
    journal_root = f"{args.evidence_prefix}/{args.repository}"
    listing = _list_journal_versions(
        s3_client,
        bucket=args.evidence_bucket,
        journal_root=journal_root,
    )
    versions = _journal_versions(
        listing,
        journal_root=journal_root,
    )
    selected = _journal_base_from_versions(
        versions,
        journal_root=journal_root,
    )
    if selected is None:
        _verify_terminal_pairs(
            s3_client,
            versions=versions,
            bucket=args.evidence_bucket,
            journal_root=journal_root,
            repository=args.repository,
            intent_signing_key_arn=args.intent_signing_key_arn,
            terminal_signing_key_arn=args.terminal_signing_key_arn,
        )
        return "no-op"
    base, run_id, run_attempt = selected
    event = _build_broker_event(
        versions,
        base=base,
        repository=args.repository,
        run_id=run_id,
        run_attempt=run_attempt,
    )

    with tempfile.TemporaryDirectory(prefix="axonllm-transition-watchdog-") as temporary_name:
        directory = Path(temporary_name)
        intent, _ = _require_pair(
            s3_client,
            bucket=args.evidence_bucket,
            base=base,
            artifact_name="promotion.json",
            signature_name="promotion-kms-signature.json",
            artifact_version=event["intentVersionId"],
            signature_version=event["intentSignatureVersionId"],
            directory=directory,
            signing_key_arn=args.intent_signing_key_arn,
        )
        setup, _ = _require_pair(
            s3_client,
            bucket=args.evidence_bucket,
            base=base,
            artifact_name="transition-recovery-setup.json",
            signature_name=("transition-recovery-setup-kms-signature.json"),
            artifact_version=event["recoverySetupVersionId"],
            signature_version=event["recoverySetupSignatureVersionId"],
            directory=directory,
            signing_key_arn=args.intent_signing_key_arn,
        )
        binding, _ = _require_pair(
            s3_client,
            bucket=args.evidence_bucket,
            base=base,
            artifact_name="transition-recovery-binding.json",
            signature_name=("transition-recovery-binding-kms-signature.json"),
            artifact_version=event["recoveryBindingVersionId"],
            signature_version=event["recoveryBindingSignatureVersionId"],
            directory=directory,
            signing_key_arn=args.intent_signing_key_arn,
        )
        try:
            deployment_transition.verify_recovery_binding(
                argparse.Namespace(
                    intent=intent,
                    setup_config=setup,
                    binding=binding,
                    repository=args.repository,
                    run_id=run_id,
                    run_attempt=run_attempt,
                )
            )
        except deployment_transition.TransitionRecordError as exc:
            raise ReconciliationError("transition recovery binding is invalid") from exc

        intent_value = _read_json(intent)
        transition = _object(
            intent_value.get("transition"),
            "promotion transition identity",
        )
        transition_id = transition.get("transitionId")
        if not isinstance(transition_id, str) or deployment_transition.TRANSITION_ID.fullmatch(transition_id) is None:
            raise ReconciliationError("promotion transition ID is invalid")

        has_commit = "deploymentCommitSignatureVersionId" in event
        if has_commit:
            evidence, evidence_signature = _require_pair(
                s3_client,
                bucket=args.evidence_bucket,
                base=base,
                artifact_name="agentcore-deployment.json",
                signature_name="agentcore-deployment-kms-signature.json",
                artifact_version=event["deploymentEvidenceVersionId"],
                signature_version=event["deploymentEvidenceSignatureVersionId"],
                directory=directory,
                signing_key_arn=args.intent_signing_key_arn,
            )
            commit_record, _ = _require_pair(
                s3_client,
                bucket=args.evidence_bucket,
                base=base,
                artifact_name="agentcore-deployment-commit.json",
                signature_name=("agentcore-deployment-commit-kms-signature.json"),
                artifact_version=event["deploymentCommitVersionId"],
                signature_version=event["deploymentCommitSignatureVersionId"],
                directory=directory,
                signing_key_arn=args.intent_signing_key_arn,
            )
            try:
                _verify_deployment_commit(
                    evidence,
                    evidence_signature,
                    commit_record,
                    repository=args.repository,
                    run_id=run_id,
                    run_attempt=run_attempt,
                )
            except deployment_evidence.DeploymentEvidenceError as exc:
                raise ReconciliationError("committed deployment evidence is invalid") from exc
            outcome = "committed"
            operation = "FINALIZE"
        else:
            outcome = "rolled-back"
            operation = "ROLLBACK"

        result = _invoke_mutation_broker(
            lambda_client,
            version_arn=args.mutation_broker_version_arn,
            event=event,
            transition_id=transition_id,
            expected_operation=operation,
        )
        if result["status"] == "PENDING":
            return "deferred" if result["phase"] == "ROLLBACK_NOT_BEFORE" else "pending"

        terminal = directory / "transition-terminal.json"
        terminal_signature = directory / "transition-terminal-kms-signature.json"
        terminal_state = _fetch_state(
            s3_client,
            bucket=args.evidence_bucket,
            key=f"{base}/{terminal.name}",
            output=terminal,
        )
        signature_state = _fetch_state(
            s3_client,
            bucket=args.evidence_bucket,
            key=f"{base}/{terminal_signature.name}",
            output=terminal_signature,
        )
        if signature_state == deployment_transition.S3_PRESENT:
            raise ReconciliationError("terminal signature appeared during reconciliation")
        if signature_state == deployment_transition.S3_INDETERMINATE:
            raise ReconciliationError("terminal signature state is indeterminate")
        record_args = argparse.Namespace(
            intent=intent,
            output=terminal,
            record=terminal,
            outcome=outcome,
            repository=args.repository,
            run_id=run_id,
            run_attempt=run_attempt,
        )
        if terminal_state == deployment_transition.S3_ABSENT:
            deployment_transition.materialize_record(record_args)
            _put_locked(
                s3_client,
                bucket=args.evidence_bucket,
                key=f"{base}/{terminal.name}",
                path=terminal,
                retain_until=args.retain_until,
            )
        elif terminal_state == deployment_transition.S3_PRESENT:
            deployment_transition.verify_record(record_args)
        else:
            raise ReconciliationError("terminal record state is indeterminate")
        try:
            kms_evidence.sign_artifact(
                terminal,
                terminal_signature,
                args.terminal_signing_key_arn,
            )
            kms_evidence.verify_artifact(
                terminal,
                terminal_signature,
                args.terminal_signing_key_arn,
            )
        except kms_evidence.KmsEvidenceError as exc:
            raise ReconciliationError("cannot sign the terminal transition record") from exc
        _put_locked(
            s3_client,
            bucket=args.evidence_bucket,
            key=f"{base}/{terminal_signature.name}",
            path=terminal_signature,
            retain_until=args.retain_until,
        )
        return outcome


def _retention(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("retention must be an ISO-8601 timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)
    ):
        raise argparse.ArgumentTypeError("retention must be a future timestamp")
    return parsed.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile a signed AgentCore production transition",
    )
    parser.add_argument("--evidence-bucket", required=True)
    parser.add_argument("--evidence-prefix", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--intent-signing-key-arn", required=True)
    parser.add_argument("--terminal-signing-key-arn", required=True)
    parser.add_argument("--mutation-broker-version-arn", required=True)
    parser.add_argument("--retain-until", required=True, type=_retention)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import boto3

        result = reconcile(
            args,
            s3_client=boto3.client("s3"),
            lambda_client=boto3.client("lambda"),
        )
    except (ImportError, ReconciliationError) as exc:
        print(f"transition reconciliation failed: {exc}", file=sys.stderr)
        return 1
    print(f"transition reconciliation result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
