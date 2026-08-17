#!/usr/bin/env python3
"""Produce immutable, release-bound AgentCore launch-rehearsal evidence.

The producer consumes a KMS-signed, Object-Locked gate-set manifest. Every gate
points to a separately signed and Object-Locked command receipt, and every
command points to immutable stdout and stderr objects. The final stdout object
is the sole source of gate observations; receipts never carry a caller-supplied
PASS value or independent observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit


SOURCE_SCHEMA = "axonllm.agentcore-launch-gate-set/v2"
GATE_SCHEMA = "axonllm.agentcore-launch-gate-command/v1"
TERMINAL_SCHEMA = "axonllm.agentcore-launch-gate-terminal/v1"
COMMAND_OUTPUT_SCHEMA = "axonllm.agentcore-launch-command-output/v1"
REPORT_SCHEMA = "axonllm.agentcore-launch-rehearsal-evidence/v2"
COMPATIBILITY_SCHEMA = "axonllm.agentcore-launch-rehearsal/v1"
PRODUCER_WORKFLOW = ".github/workflows/agentcore-launch-rehearsal-evidence.yml"
GATE_WORKFLOW = ".github/workflows/agentcore-launch-gates.yml"
LAUNCH_WORKFLOW = ".github/workflows/launch-agentcore-production.yml"

CORE_GATES = (
    "initializationTimeoutReplacement",
    "recoveryCutoverAndRollback",
    "securityEventDeliveryAndDlq",
)
ADDITIONAL_GATES = (
    "providerRoutingStrategies",
    "providerFallbackRecovery",
    "controlPlaneFaultRecovery",
)
ALL_GATES = CORE_GATES + ADDITIONAL_GATES

EXPECTED_COMMANDS: Mapping[str, tuple[str, ...]] = {
    "initializationTimeoutReplacement": (
        "induce-initialization-timeout",
        "observe-exit-124",
        "observe-runtime-replacement",
        "verify-replacement-ready",
    ),
    "recoveryCutoverAndRollback": (
        "restore-state",
        "cutover-restored-state",
        "verify-restored-state",
        "rollback-primary-state",
        "verify-primary-state",
    ),
    "securityEventDeliveryAndDlq": (
        "deliver-security-events",
        "verify-outbox-drained",
        "force-dead-letter",
        "verify-dead-letter-alarm",
        "redrive-dead-letter",
        "verify-redelivery",
    ),
    "providerRoutingStrategies": (
        "exercise-routing-strategies",
        "verify-routing-decisions",
    ),
    "providerFallbackRecovery": (
        "inject-primary-provider-fault",
        "verify-provider-fallback",
        "clear-primary-provider-fault",
        "verify-primary-provider-recovery",
    ),
    "controlPlaneFaultRecovery": (
        "inject-control-plane-fault",
        "verify-control-plane-fail-closed",
        "clear-control-plane-fault",
        "verify-control-plane-recovery",
    ),
}

ROUTING_STRATEGIES = (
    "cost-optimized",
    "ensemble",
    "least-latency",
    "round-robin",
    "smart",
    "weighted",
)

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
MAX_AWS_RESPONSE_BYTES = 256 * 1024
AWS_TIMEOUT_SECONDS = 120
MAX_S3_URI_LENGTH = 384
MAX_GATE_AGE = timedelta(days=30)

SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z0-9-]+-[1-9][0-9]*$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_ID = re.compile(r"^[1-9][0-9]*$")
VERSION_ID = re.compile(r"^[A-Za-z0-9._~+/=-]{1,1024}$")
BUCKET = re.compile(
    r"^(?![0-9]+(?:\.[0-9]+){3}$)"
    r"(?!.*\.\.)(?!.*\.-)(?!.*-\.)"
    r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)
S3_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,382}$")
SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]+$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{1,255}$")
PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SENSITIVE_ARGUMENT = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9]|"
    r"(?:api[_-]?key|password|access[_-]?token|refresh[_-]?token|"
    r"authorization|cookie)=\S)"
)
SENSITIVE_FLAGS = frozenset(
    {
        "-H",
        "--header",
        "-u",
        "--user",
        "--password",
        "--token",
        "--access-token",
        "--refresh-token",
        "--api-key",
        "--authorization",
        "--cookie",
        "--secret-value",
    }
)
TOOL = re.compile(
    r"^(?:aws|curl|python(?:3)?:scripts/"
    r"(?:operations|release)/[A-Za-z0-9_.-]+\.py|"
    r"uv:python:scripts/(?:operations|release)/[A-Za-z0-9_.-]+\.py)$"
)
WORKFLOW_REF = re.compile(
    r"^(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/"
    r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml@refs/heads/main$"
)
KMS_KEY_ARN = re.compile(
    r"^arn:(?:aws|aws-[a-z0-9-]+):kms:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"key/[A-Za-z0-9-]{1,256}$"
)
ECR_IMAGE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@"
    r"sha256:[0-9a-f]{64}$"
)
TABLE_ARN = re.compile(
    r"^arn:(?:aws|aws-[a-z0-9-]+):dynamodb:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"table/(?P<table>[A-Za-z0-9_.-]{3,255})$"
)


class LaunchRehearsalError(RuntimeError):
    """Raised when launch-rehearsal evidence cannot be trusted."""


@dataclass(frozen=True)
class S3Reference:
    uri: str
    bucket: str
    key: str
    version_id: str
    sha256: str

    def report_value(self) -> dict[str, str]:
        return {
            "s3Uri": self.uri,
            "versionId": self.version_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ArtifactPair:
    artifact: S3Reference
    signature: S3Reference

    def report_value(self) -> dict[str, dict[str, str]]:
        return {
            "artifact": self.artifact.report_value(),
            "signature": self.signature.report_value(),
        }


@dataclass(frozen=True)
class ValidatedSource:
    release: dict[str, str]
    execution: dict[str, str]
    gates: Mapping[str, ArtifactPair]
    terminal: ArtifactPair
    normalized: dict[str, Any]


Fetcher = Callable[[S3Reference, Path, str, datetime], None]
SignatureVerifier = Callable[[Path, Path, str], None]
ScriptChecker = Callable[[str, str], None]
CommandOutputs = Mapping[tuple[str, int, str], bytes]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LaunchRehearsalError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LaunchRehearsalError(f"invalid JSON constant: {value}")


def _read_regular(path: Path, *, maximum: int = MAX_INPUT_BYTES) -> bytes:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise LaunchRehearsalError(f"cannot inspect input: {path}") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise LaunchRehearsalError(f"input must be a regular non-symlink file: {path}")
    if initial.st_size > maximum:
        raise LaunchRehearsalError(f"input is too large: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            value = stream.read(maximum + 1)
            final = os.fstat(stream.fileno())
    except OSError as exc:
        raise LaunchRehearsalError(f"cannot read input: {path}") from exc
    if len(value) > maximum:
        raise LaunchRehearsalError(f"input is too large: {path}")
    if (
        opened.st_dev != initial.st_dev
        or opened.st_ino != initial.st_ino
        or final.st_dev != initial.st_dev
        or final.st_ino != initial.st_ino
        or final.st_size != initial.st_size
        or len(value) != final.st_size
        or final.st_mtime_ns != initial.st_mtime_ns
    ):
        raise LaunchRehearsalError(f"input changed while reading: {path}")
    return value


def _read_json(path: Path) -> Any:
    raw = _read_regular(path)
    return _strict_json_bytes(raw, location=str(path))


def _strict_json_bytes(raw: bytes, *, location: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, LaunchRehearsalError) as exc:
        raise LaunchRehearsalError(f"input is not strict JSON: {location}") from exc


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise LaunchRehearsalError(f"cannot inspect output: {path}") from exc
    if current is not None and (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
        raise LaunchRehearsalError(f"output must be a regular non-symlink file: {path}")
    encoded = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise LaunchRehearsalError(f"cannot write output: {path}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(_read_regular(path)).hexdigest()


def _canonical_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LaunchRehearsalError(f"{location} must be a JSON object")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    location: str,
) -> None:
    if set(value) != expected:
        raise LaunchRehearsalError(f"{location} fields do not match schema")


def _string(
    value: Any,
    location: str,
    *,
    maximum: int = 512,
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or SAFE_TEXT.fullmatch(value) is None:
        raise LaunchRehearsalError(f"{location} is malformed")
    return value


def _integer(
    value: Any,
    location: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise LaunchRehearsalError(f"{location} is malformed")
    return value


def _timestamp(value: Any, location: str) -> tuple[str, datetime]:
    text = _string(value, location, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LaunchRehearsalError(f"{location} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LaunchRehearsalError(f"{location} must include a timezone")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    return normalized, parsed.astimezone(timezone.utc)


def _validate_repository(value: Any, location: str) -> str:
    repository = _string(value, location, maximum=128)
    if REPOSITORY.fullmatch(repository) is None:
        raise LaunchRehearsalError(f"{location} must be owner/name")
    return repository


def _validate_workflow_ref(
    value: Any,
    *,
    repository: str,
    location: str,
) -> str:
    workflow_ref = _string(value, location, maximum=256)
    match = WORKFLOW_REF.fullmatch(workflow_ref)
    if match is None or match.group("repository") != repository:
        raise LaunchRehearsalError(f"{location} must identify this repository's main-branch workflow")
    return workflow_ref


def _validate_execution(
    value: Any,
    *,
    release_commit: str,
) -> dict[str, str]:
    execution = _object(value, "gate execution")
    _exact_fields(
        execution,
        {
            "repository",
            "workflowRef",
            "workflowCommit",
            "parentWorkflowRef",
            "parentWorkflowCommit",
            "checkedOutCommit",
            "runId",
            "runAttempt",
            "reviewedConfigS3Uri",
            "reviewedConfigVersionId",
            "reviewedConfigSha256",
        },
        "gate execution",
    )
    repository = _validate_repository(
        execution.get("repository"),
        "gate execution repository",
    )
    workflow_ref = _validate_workflow_ref(
        execution.get("workflowRef"),
        repository=repository,
        location="gate execution workflowRef",
    )
    expected_workflow_ref = f"{repository}/{GATE_WORKFLOW}@refs/heads/main"
    parent_workflow_ref = _validate_workflow_ref(
        execution.get("parentWorkflowRef"),
        repository=repository,
        location="gate execution parentWorkflowRef",
    )
    allowed_parent_refs = {
        expected_workflow_ref,
        f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main",
    }
    workflow_commit = _string(
        execution.get("workflowCommit"),
        "gate execution workflowCommit",
        maximum=40,
    )
    parent_workflow_commit = _string(
        execution.get("parentWorkflowCommit"),
        "gate execution parentWorkflowCommit",
        maximum=40,
    )
    checked_out = _string(
        execution.get("checkedOutCommit"),
        "gate execution checkedOutCommit",
        maximum=40,
    )
    if workflow_ref != expected_workflow_ref:
        raise LaunchRehearsalError("gate execution is not the allowlisted launch-gate workflow")
    if parent_workflow_ref not in allowed_parent_refs:
        raise LaunchRehearsalError("gate execution parent is not an allowlisted protected workflow")
    if SHA.fullmatch(workflow_commit) is None or SHA.fullmatch(parent_workflow_commit) is None:
        raise LaunchRehearsalError("gate execution workflow commits must be full commit SHAs")
    if workflow_commit != release_commit or parent_workflow_commit != release_commit or checked_out != release_commit:
        raise LaunchRehearsalError("gate execution workflow, parent, and checkout must equal the release commit")
    run_id = _string(execution.get("runId"), "gate execution runId")
    run_attempt = _string(
        execution.get("runAttempt"),
        "gate execution runAttempt",
    )
    if RUN_ID.fullmatch(run_id) is None or RUN_ID.fullmatch(run_attempt) is None:
        raise LaunchRehearsalError("gate execution run identity is malformed")
    reviewed_config_uri, _, _ = _parse_s3_uri(
        execution.get("reviewedConfigS3Uri"),
        "gate execution reviewed config URI",
    )
    reviewed_config_version = _string(
        execution.get("reviewedConfigVersionId"),
        "gate execution reviewed config VersionId",
        maximum=1024,
    )
    reviewed_config_sha256 = _string(
        execution.get("reviewedConfigSha256"),
        "gate execution reviewed config SHA-256",
        maximum=64,
    )
    if (
        VERSION_ID.fullmatch(reviewed_config_version) is None
        or reviewed_config_version == "null"
        or SHA256.fullmatch(reviewed_config_sha256) is None
    ):
        raise LaunchRehearsalError("gate execution reviewed config binding is malformed")
    return {
        "repository": repository,
        "workflowRef": workflow_ref,
        "workflowCommit": workflow_commit,
        "parentWorkflowRef": parent_workflow_ref,
        "parentWorkflowCommit": parent_workflow_commit,
        "checkedOutCommit": checked_out,
        "runId": run_id,
        "runAttempt": run_attempt,
        "reviewedConfigS3Uri": reviewed_config_uri,
        "reviewedConfigVersionId": reviewed_config_version,
        "reviewedConfigSha256": reviewed_config_sha256,
    }


def _validate_producer(
    value: Any,
    *,
    release_commit: str | None = None,
) -> dict[str, str]:
    producer = _object(value, "evidence producer")
    _exact_fields(
        producer,
        {
            "repository",
            "workflowRef",
            "workflowCommit",
            "parentWorkflowRef",
            "parentWorkflowCommit",
            "runId",
            "runAttempt",
        },
        "evidence producer",
    )
    repository = _validate_repository(
        producer.get("repository"),
        "evidence producer repository",
    )
    workflow_ref = _validate_workflow_ref(
        producer.get("workflowRef"),
        repository=repository,
        location="evidence producer workflowRef",
    )
    workflow_commit = _string(
        producer.get("workflowCommit"),
        "evidence producer workflowCommit",
        maximum=40,
    )
    parent_workflow_ref = _validate_workflow_ref(
        producer.get("parentWorkflowRef"),
        repository=repository,
        location="evidence producer parentWorkflowRef",
    )
    parent_workflow_commit = _string(
        producer.get("parentWorkflowCommit"),
        "evidence producer parentWorkflowCommit",
        maximum=40,
    )
    if (
        workflow_ref != f"{repository}/{PRODUCER_WORKFLOW}@refs/heads/main"
        or parent_workflow_ref != f"{repository}/{LAUNCH_WORKFLOW}@refs/heads/main"
    ):
        raise LaunchRehearsalError(
            "evidence producer is not the protected rehearsal workflow called by the production launch workflow"
        )
    run_id = _string(producer.get("runId"), "evidence producer runId")
    run_attempt = _string(
        producer.get("runAttempt"),
        "evidence producer runAttempt",
    )
    if SHA.fullmatch(workflow_commit) is None or SHA.fullmatch(parent_workflow_commit) is None:
        raise LaunchRehearsalError("evidence producer workflow commits must be full commit SHAs")
    if release_commit is not None and (workflow_commit != release_commit or parent_workflow_commit != release_commit):
        raise LaunchRehearsalError("evidence producer workflow and parent commits must equal the release commit")
    if RUN_ID.fullmatch(run_id) is None or RUN_ID.fullmatch(run_attempt) is None:
        raise LaunchRehearsalError("evidence producer run identity is malformed")
    return {
        "repository": repository,
        "workflowRef": workflow_ref,
        "workflowCommit": workflow_commit,
        "parentWorkflowRef": parent_workflow_ref,
        "parentWorkflowCommit": parent_workflow_commit,
        "runId": run_id,
        "runAttempt": run_attempt,
    }


def _validate_image(
    value: Any,
    *,
    expected_region: str,
    expected_repository: str,
    location: str,
) -> tuple[str, str]:
    image = _string(value, location, maximum=512)
    match = ECR_IMAGE.fullmatch(image)
    if match is None or match.group("region") != expected_region or match.group("repository") != expected_repository:
        raise LaunchRehearsalError(f"{location} must be an immutable ECR digest URI in {expected_region}")
    return image, match.group("account")


def _validate_release(value: Any) -> dict[str, str]:
    release = _object(value, "release binding")
    _exact_fields(
        release,
        {"commit", "region", "agentcoreImage", "controlPlaneImage"},
        "release binding",
    )
    commit = _string(release.get("commit"), "release commit", maximum=40)
    region = _string(release.get("region"), "release region", maximum=32)
    if SHA.fullmatch(commit) is None:
        raise LaunchRehearsalError("release commit must be a full commit SHA")
    if REGION.fullmatch(region) is None:
        raise LaunchRehearsalError("release region is malformed")
    agentcore_image, account = _validate_image(
        release.get("agentcoreImage"),
        expected_region=region,
        expected_repository="axonllm/agentcore",
        location="AgentCore image",
    )
    control_image, control_account = _validate_image(
        release.get("controlPlaneImage"),
        expected_region=region,
        expected_repository="axonllm/fargate",
        location="control-plane image",
    )
    if account != control_account:
        raise LaunchRehearsalError("release images use different AWS accounts")
    return {
        "commit": commit,
        "region": region,
        "agentcoreImage": agentcore_image,
        "controlPlaneImage": control_image,
    }


def _validate_kms_key(
    value: str,
    *,
    release: Mapping[str, str],
    location: str,
) -> str:
    match = KMS_KEY_ARN.fullmatch(value)
    image_match = ECR_IMAGE.fullmatch(release["agentcoreImage"])
    if (
        match is None
        or image_match is None
        or match.group("region") != release["region"]
        or match.group("account") != image_match.group("account")
    ):
        raise LaunchRehearsalError(f"{location} must be a full key ARN in the release account and region")
    return value


def _validate_prefix(value: str) -> str:
    prefix = _string(value, "evidence prefix", maximum=256).strip("/")
    if not prefix or S3_KEY.fullmatch(prefix) is None or any(part in {"", ".", ".."} for part in prefix.split("/")):
        raise LaunchRehearsalError("evidence prefix is malformed")
    return prefix


def _parse_s3_uri(value: Any, location: str) -> tuple[str, str, str]:
    uri = _string(value, location, maximum=MAX_S3_URI_LENGTH)
    parsed = urlsplit(uri)
    key = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or BUCKET.fullmatch(parsed.netloc) is None
        or S3_KEY.fullmatch(key) is None
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or uri != f"s3://{parsed.netloc}/{key}"
    ):
        raise LaunchRehearsalError(f"{location} must be a canonical S3 URI")
    return uri, parsed.netloc, key


def _validate_reference(
    value: Any,
    *,
    expected_bucket: str,
    expected_prefix: str,
    location: str,
) -> S3Reference:
    reference = _object(value, location)
    _exact_fields(
        reference,
        {"s3Uri", "versionId", "sha256"},
        location,
    )
    uri, bucket, key = _parse_s3_uri(reference.get("s3Uri"), f"{location} URI")
    if bucket != expected_bucket or not key.startswith(f"{expected_prefix}/"):
        raise LaunchRehearsalError(f"{location} is outside the approved evidence prefix")
    version_id = _string(
        reference.get("versionId"),
        f"{location} versionId",
        maximum=1024,
    )
    digest = _string(
        reference.get("sha256"),
        f"{location} sha256",
        maximum=64,
    )
    if VERSION_ID.fullmatch(version_id) is None or version_id == "null" or SHA256.fullmatch(digest) is None:
        raise LaunchRehearsalError(f"{location} immutable binding is malformed")
    return S3Reference(
        uri=uri,
        bucket=bucket,
        key=key,
        version_id=version_id,
        sha256=digest,
    )


def _reference_from_arguments(
    *,
    uri: str,
    version_id: str,
    sha256: str,
    expected_bucket: str,
    expected_prefix: str,
    location: str,
) -> S3Reference:
    return _validate_reference(
        {
            "s3Uri": uri,
            "versionId": version_id,
            "sha256": sha256,
        },
        expected_bucket=expected_bucket,
        expected_prefix=expected_prefix,
        location=location,
    )


def _validate_source_manifest(
    value: Any,
    *,
    expected_release: Mapping[str, str],
    evidence_bucket: str,
    evidence_prefix: str,
) -> ValidatedSource:
    source = _object(value, "gate-set manifest")
    _exact_fields(
        source,
        {"schema", "release", "execution", "terminal", "gates"},
        "gate-set manifest",
    )
    if source.get("schema") != SOURCE_SCHEMA:
        raise LaunchRehearsalError("gate-set manifest schema is unsupported")
    release = _validate_release(source.get("release"))
    if release != dict(expected_release):
        raise LaunchRehearsalError("gate-set manifest is not bound to the requested release")
    execution = _validate_execution(
        source.get("execution"),
        release_commit=release["commit"],
    )
    _, config_bucket, config_key = _parse_s3_uri(
        execution["reviewedConfigS3Uri"],
        "gate-set reviewed config URI",
    )
    if config_bucket != evidence_bucket or not config_key.startswith(f"{evidence_prefix}/"):
        raise LaunchRehearsalError("gate-set reviewed config is outside the approved evidence prefix")
    if execution["repository"] == "":
        raise LaunchRehearsalError("gate-set execution repository is missing")
    raw_gates = _object(source.get("gates"), "gate-set gates")
    if set(raw_gates) != set(ALL_GATES):
        raise LaunchRehearsalError("gate-set manifest must contain every required launch gate")

    gates: dict[str, ArtifactPair] = {}
    normalized_gates: dict[str, Any] = {}
    identities: set[tuple[str, str]] = set()
    raw_terminal = _object(
        source.get("terminal"),
        "gate-set terminal",
    )
    _exact_fields(
        raw_terminal,
        {"artifact", "signature"},
        "gate-set terminal",
    )
    terminal = ArtifactPair(
        artifact=_validate_reference(
            raw_terminal.get("artifact"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location="gate-set terminal artifact",
        ),
        signature=_validate_reference(
            raw_terminal.get("signature"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location="gate-set terminal signature",
        ),
    )
    for reference in (terminal.artifact, terminal.signature):
        identity = (reference.uri, reference.version_id)
        if identity in identities:
            raise LaunchRehearsalError("gate-set manifest reuses an immutable object version")
        identities.add(identity)
    for gate_name in sorted(ALL_GATES):
        raw_pair = _object(
            raw_gates.get(gate_name),
            f"gate-set {gate_name}",
        )
        _exact_fields(
            raw_pair,
            {"artifact", "signature"},
            f"gate-set {gate_name}",
        )
        artifact = _validate_reference(
            raw_pair.get("artifact"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location=f"{gate_name} artifact",
        )
        signature = _validate_reference(
            raw_pair.get("signature"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location=f"{gate_name} signature",
        )
        for reference in (artifact, signature):
            identity = (reference.uri, reference.version_id)
            if identity in identities:
                raise LaunchRehearsalError("gate-set manifest reuses an immutable object version")
            identities.add(identity)
        pair = ArtifactPair(artifact=artifact, signature=signature)
        gates[gate_name] = pair
        normalized_gates[gate_name] = pair.report_value()

    normalized = {
        "schema": SOURCE_SCHEMA,
        "release": release,
        "execution": execution,
        "terminal": terminal.report_value(),
        "gates": normalized_gates,
    }
    if normalized != source:
        raise LaunchRehearsalError("gate-set manifest is not normalized")
    return ValidatedSource(
        release=release,
        execution=execution,
        gates=gates,
        terminal=terminal,
        normalized=normalized,
    )


def _validate_cleanup_observations(value: Any) -> dict[str, Any]:
    observations = _object(value, "launch-gate terminal cleanup observations")
    inventory_fields = (
        "restoredSnapshotRefs",
        "clearedFaultIds",
        "clearedFixtureIds",
        "redrivenDlqCorrelationIds",
        "removedDlqCorrelationIds",
    )
    _exact_fields(
        observations,
        {
            *inventory_fields,
            "primaryStateSelected",
            "productionEndpointStatus",
            "faultsRemaining",
            "fixturesRemaining",
            "correlatedDlqMessagesRemaining",
        },
        "launch-gate terminal cleanup observations",
    )
    normalized: dict[str, Any] = {}
    for name in inventory_fields:
        raw = observations.get(name)
        if (
            type(raw) is not list
            or len(raw) > 4096
            or any(not isinstance(item, str) for item in raw)
            or raw != sorted(raw)
            or len(raw) != len(set(raw))
        ):
            raise LaunchRehearsalError(f"launch-gate terminal cleanup {name} is malformed")
        normalized[name] = [
            _string(
                item,
                f"launch-gate terminal cleanup {name} item",
                maximum=512,
            )
            for item in raw
        ]
    if set(normalized["redrivenDlqCorrelationIds"]).intersection(normalized["removedDlqCorrelationIds"]):
        raise LaunchRehearsalError("launch-gate terminal cleanup DLQ inventories overlap")
    if (
        observations.get("primaryStateSelected") is not True
        or observations.get("productionEndpointStatus") != "READY"
        or _integer(
            observations.get("faultsRemaining"),
            "launch-gate terminal cleanup faultsRemaining",
            minimum=0,
            maximum=0,
        )
        != 0
        or _integer(
            observations.get("fixturesRemaining"),
            "launch-gate terminal cleanup fixturesRemaining",
            minimum=0,
            maximum=0,
        )
        != 0
        or _integer(
            observations.get("correlatedDlqMessagesRemaining"),
            "launch-gate terminal cleanup correlatedDlqMessagesRemaining",
            minimum=0,
            maximum=0,
        )
        != 0
    ):
        raise LaunchRehearsalError("launch-gate terminal does not prove complete cleanup")
    return {
        **normalized,
        "primaryStateSelected": True,
        "productionEndpointStatus": "READY",
        "faultsRemaining": 0,
        "fixturesRemaining": 0,
        "correlatedDlqMessagesRemaining": 0,
    }


def _validate_terminal(
    value: Any,
    *,
    source: ValidatedSource,
    now: datetime,
) -> dict[str, Any]:
    terminal = _object(value, "launch-gate terminal")
    _exact_fields(
        terminal,
        {
            "schema",
            "release",
            "execution",
            "status",
            "failureStage",
            "cleanupStatus",
            "cleanupObservations",
            "startedAt",
            "completedAt",
        },
        "launch-gate terminal",
    )
    release = _validate_release(terminal.get("release"))
    execution = _validate_execution(
        terminal.get("execution"),
        release_commit=release["commit"],
    )
    started_text, started = _timestamp(
        terminal.get("startedAt"),
        "launch-gate terminal startedAt",
    )
    completed_text, completed = _timestamp(
        terminal.get("completedAt"),
        "launch-gate terminal completedAt",
    )
    if now.tzinfo is None or now.utcoffset() is None:
        raise LaunchRehearsalError("launch-gate terminal validation time must include timezone")
    current = now.astimezone(timezone.utc)
    if (
        terminal.get("schema") != TERMINAL_SCHEMA
        or release != source.release
        or execution != source.execution
        or terminal.get("status") != "PASSED"
        or terminal.get("failureStage") is not None
        or terminal.get("cleanupStatus") != "SUCCEEDED"
        or started > completed
        or completed > current
        or current - completed > MAX_GATE_AGE
    ):
        raise LaunchRehearsalError("launch-gate terminal does not prove a successful current run")
    cleanup = _validate_cleanup_observations(terminal.get("cleanupObservations"))
    normalized = {
        "schema": TERMINAL_SCHEMA,
        "release": release,
        "execution": execution,
        "status": "PASSED",
        "failureStage": None,
        "cleanupStatus": "SUCCEEDED",
        "cleanupObservations": cleanup,
        "startedAt": started_text,
        "completedAt": completed_text,
    }
    if normalized != terminal:
        raise LaunchRehearsalError("launch-gate terminal is not normalized")
    return normalized


def _validate_commands(
    value: Any,
    *,
    gate_name: str,
    release: Mapping[str, str],
    execution: Mapping[str, str],
    evidence_bucket: str,
    evidence_prefix: str,
    command_outputs: CommandOutputs,
    script_checker: ScriptChecker,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    str,
    str,
    datetime,
    datetime,
]:
    if not isinstance(value, list):
        raise LaunchRehearsalError(f"{gate_name} commands must be an array")
    expected_names = EXPECTED_COMMANDS[gate_name]
    if len(value) != len(expected_names):
        raise LaunchRehearsalError(f"{gate_name} command receipt sequence is incomplete")
    normalized: list[dict[str, Any]] = []
    previous_completed: datetime | None = None
    first_started: datetime | None = None
    last_completed: datetime | None = None
    for index, (raw_command, expected_name) in enumerate(zip(value, expected_names, strict=True)):
        location = f"{gate_name} command {index + 1}"
        command = _object(raw_command, location)
        _exact_fields(
            command,
            {
                "name",
                "tool",
                "argv",
                "commandSha256",
                "stdout",
                "stderr",
                "startedAt",
                "completedAt",
                "exitCode",
            },
            location,
        )
        name = _string(command.get("name"), f"{location} name", maximum=64)
        tool = _string(command.get("tool"), f"{location} tool", maximum=256)
        argv = command.get("argv")
        if not isinstance(argv, list) or not 2 <= len(argv) <= 64:
            raise LaunchRehearsalError(f"{location} argv is malformed")
        normalized_argv: list[str] = []
        argv_size = 0
        for argument_index, raw_argument in enumerate(argv):
            argument = _string(
                raw_argument,
                f"{location} argv item {argument_index + 1}",
                maximum=512,
            )
            argv_size += len(argument.encode("utf-8"))
            normalized_argv.append(argument)
        expected_prefix: list[str]
        if tool == "aws":
            expected_prefix = ["aws"]
        elif tool.startswith("python:"):
            expected_prefix = ["python", tool.removeprefix("python:")]
        elif tool.startswith("python3:"):
            expected_prefix = ["python3", tool.removeprefix("python3:")]
        elif tool.startswith("uv:python:"):
            expected_prefix = [
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                tool.removeprefix("uv:python:"),
            ]
        else:
            expected_prefix = ["curl"]
        if (
            argv_size > 8192
            or normalized_argv[: len(expected_prefix)] != expected_prefix
            or any(argument in SENSITIVE_FLAGS for argument in normalized_argv)
            or any(SENSITIVE_ARGUMENT.search(argument) is not None for argument in normalized_argv)
        ):
            raise LaunchRehearsalError(f"{location} argv is unsafe or not bound to its tool")
        command_sha = _string(
            command.get("commandSha256"),
            f"{location} commandSha256",
            maximum=64,
        )
        stdout = _validate_reference(
            command.get("stdout"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location=f"{location} stdout",
        )
        stderr = _validate_reference(
            command.get("stderr"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location=f"{location} stderr",
        )
        exit_code = _integer(
            command.get("exitCode"),
            f"{location} exitCode",
            minimum=0,
            maximum=255,
        )
        started_text, started = _timestamp(
            command.get("startedAt"),
            f"{location} startedAt",
        )
        completed_text, completed = _timestamp(
            command.get("completedAt"),
            f"{location} completedAt",
        )
        if (
            name != expected_name
            or TOOL.fullmatch(tool) is None
            or command_sha != _canonical_sha(normalized_argv)
            or exit_code != 0
            or completed < started
            or completed - started > timedelta(days=7)
            or (previous_completed is not None and started < previous_completed)
        ):
            raise LaunchRehearsalError(f"{location} does not prove the required successful command")
        script = _script_from_tool(tool)
        if script is not None:
            script_checker(execution["workflowCommit"], script)
        stdout_bytes = command_outputs.get((gate_name, index, "stdout"))
        stderr_bytes = command_outputs.get((gate_name, index, "stderr"))
        if (
            not isinstance(stdout_bytes, bytes)
            or not isinstance(stderr_bytes, bytes)
            or len(stdout_bytes) > MAX_COMMAND_OUTPUT_BYTES
            or len(stderr_bytes) > MAX_COMMAND_OUTPUT_BYTES
            or hashlib.sha256(stdout_bytes).hexdigest() != stdout.sha256
            or hashlib.sha256(stderr_bytes).hexdigest() != stderr.sha256
        ):
            raise LaunchRehearsalError(f"{location} output bytes do not match immutable references")
        if stderr_bytes != b"":
            raise LaunchRehearsalError(f"{location} successful command has non-empty stderr")
        output = _object(
            _strict_json_bytes(
                stdout_bytes,
                location=f"{location} stdout",
            ),
            f"{location} stdout",
        )
        _exact_fields(
            output,
            {
                "schema",
                "gate",
                "action",
                "release",
                "execution",
                "observations",
            },
            f"{location} stdout",
        )
        final_command = index == len(expected_names) - 1
        if (
            output.get("schema") != COMMAND_OUTPUT_SCHEMA
            or output.get("gate") != gate_name
            or output.get("action") != expected_name
            or output.get("release") != dict(release)
            or output.get("execution") != dict(execution)
            or (not final_command and output.get("observations") is not None)
            or (final_command and not isinstance(output.get("observations"), dict))
        ):
            raise LaunchRehearsalError(f"{location} stdout is not bound to this command execution")
        if first_started is None:
            first_started = started
        previous_completed = completed
        last_completed = completed
        normalized.append(
            {
                "name": name,
                "tool": tool,
                "argv": normalized_argv,
                "commandSha256": command_sha,
                "stdout": stdout.report_value(),
                "stderr": stderr.report_value(),
                "startedAt": started_text,
                "completedAt": completed_text,
                "exitCode": 0,
            }
        )
    if first_started is None or last_completed is None:
        raise LaunchRehearsalError(f"{gate_name} has no command receipts")
    return (
        normalized,
        _object(
            _strict_json_bytes(
                command_outputs[(gate_name, len(expected_names) - 1, "stdout")],
                location=f"{gate_name} final stdout",
            ),
            f"{gate_name} final stdout",
        )["observations"],
        normalized[0]["startedAt"],
        normalized[-1]["completedAt"],
        first_started,
        last_completed,
    )


def _script_from_tool(tool: str) -> str | None:
    for prefix in ("python:", "python3:", "uv:python:"):
        if tool.startswith(prefix):
            return tool.removeprefix(prefix)
    return None


def _verify_script_at_commit(commit: str, script: str) -> None:
    if SHA.fullmatch(commit) is None or TOOL.fullmatch(f"python:{script}") is None:
        raise LaunchRehearsalError("command script binding is malformed")
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{script}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise LaunchRehearsalError(f"command script does not exist at the release commit: {script}") from exc


def _command_output_references(
    receipt: Any,
    *,
    gate_name: str,
    evidence_bucket: str,
    evidence_prefix: str,
) -> list[tuple[int, str, S3Reference]]:
    value = _object(receipt, f"{gate_name} receipt")
    commands = value.get("commands")
    expected = EXPECTED_COMMANDS[gate_name]
    if not isinstance(commands, list) or len(commands) != len(expected):
        raise LaunchRehearsalError(f"{gate_name} command receipt sequence is incomplete")
    references: list[tuple[int, str, S3Reference]] = []
    for index, command_value in enumerate(commands):
        command = _object(
            command_value,
            f"{gate_name} command {index + 1}",
        )
        for stream in ("stdout", "stderr"):
            references.append(
                (
                    index,
                    stream,
                    _validate_reference(
                        command.get(stream),
                        expected_bucket=evidence_bucket,
                        expected_prefix=evidence_prefix,
                        location=(f"{gate_name} command {index + 1} {stream}"),
                    ),
                )
            )
    return references


def _safe_id(value: Any, location: str) -> str:
    identifier = _string(value, location, maximum=256)
    if SAFE_ID.fullmatch(identifier) is None:
        raise LaunchRehearsalError(f"{location} is malformed")
    return identifier


def _client_error(value: Any, location: str) -> int:
    return _integer(value, location, minimum=400, maximum=499)


def _provider_list(
    value: Any,
    location: str,
    *,
    minimum: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum or len(value) > 32:
        raise LaunchRehearsalError(f"{location} is malformed")
    providers: list[str] = []
    for index, item in enumerate(value):
        provider = _string(item, f"{location} item {index + 1}", maximum=64)
        if PROVIDER.fullmatch(provider) is None:
            raise LaunchRehearsalError(f"{location} is malformed")
        providers.append(provider)
    if len(set(providers)) != len(providers) or providers != sorted(providers):
        raise LaunchRehearsalError(f"{location} must be unique and lexically sorted")
    return providers


def _table_arn(
    value: Any,
    *,
    release: Mapping[str, str],
    location: str,
) -> tuple[str, str]:
    arn = _string(value, location, maximum=512)
    match = TABLE_ARN.fullmatch(arn)
    image_match = ECR_IMAGE.fullmatch(release["agentcoreImage"])
    if (
        match is None
        or image_match is None
        or match.group("region") != release["region"]
        or match.group("account") != image_match.group("account")
    ):
        raise LaunchRehearsalError(f"{location} is outside the release account and region")
    return arn, match.group("table")


def _initialization_observations(value: Any) -> dict[str, Any]:
    observations = _object(value, "initialization observations")
    _exact_fields(
        observations,
        {
            "timeoutExitCode",
            "startupDeadlineSeconds",
            "timedOutRuntimeId",
            "replacementRuntimeId",
            "replacementReadyStatusCode",
        },
        "initialization observations",
    )
    timed_out = _safe_id(
        observations.get("timedOutRuntimeId"),
        "timed-out runtime ID",
    )
    replacement = _safe_id(
        observations.get("replacementRuntimeId"),
        "replacement runtime ID",
    )
    deadline = _integer(
        observations.get("startupDeadlineSeconds"),
        "startup deadline",
        minimum=1,
        maximum=3600,
    )
    if (
        observations.get("timeoutExitCode") != 124
        or observations.get("replacementReadyStatusCode") != 200
        or timed_out == replacement
    ):
        raise LaunchRehearsalError("initialization timeout replacement was not observed")
    return {
        "timeoutExitCode": 124,
        "startupDeadlineSeconds": deadline,
        "timedOutRuntimeId": timed_out,
        "replacementRuntimeId": replacement,
        "replacementReadyStatusCode": 200,
    }


def _phase_list(value: Any, location: str) -> list[str]:
    expected = ["quiesced", "selected", "validation", "normal"]
    if value != expected:
        raise LaunchRehearsalError(f"{location} did not traverse every phase")
    return expected


def _recovery_observations(
    value: Any,
    *,
    release: Mapping[str, str],
) -> dict[str, Any]:
    observations = _object(value, "recovery observations")
    _exact_fields(
        observations,
        {
            "primaryTableArn",
            "restoredTableArn",
            "cutoverPhases",
            "rollbackPhases",
            "cutoverSelectedTableArn",
            "rollbackSelectedTableArn",
            "finalSelectedTableArn",
            "productionEndpointStatusAfter",
            "controlPlaneDesiredCountAfter",
            "controlPlaneRunningCountAfter",
        },
        "recovery observations",
    )
    primary_arn, primary_table = _table_arn(
        observations.get("primaryTableArn"),
        release=release,
        location="primary table ARN",
    )
    restored_arn, restored_table = _table_arn(
        observations.get("restoredTableArn"),
        release=release,
        location="restored table ARN",
    )
    desired = _integer(
        observations.get("controlPlaneDesiredCountAfter"),
        "control-plane desired count",
        minimum=2,
        maximum=1000,
    )
    running = _integer(
        observations.get("controlPlaneRunningCountAfter"),
        "control-plane running count",
        minimum=0,
        maximum=1000,
    )
    cutover_phases = _phase_list(
        observations.get("cutoverPhases"),
        "recovery cutover phases",
    )
    rollback_phases = _phase_list(
        observations.get("rollbackPhases"),
        "recovery rollback phases",
    )
    if (
        primary_arn == restored_arn
        or not restored_table.startswith(f"{primary_table}-restore-validation-")
        or observations.get("cutoverSelectedTableArn") != restored_arn
        or observations.get("rollbackSelectedTableArn") != primary_arn
        or observations.get("finalSelectedTableArn") != primary_arn
        or observations.get("productionEndpointStatusAfter") != "READY"
        or running != desired
    ):
        raise LaunchRehearsalError("recovery cutover and rollback did not return to healthy primary state")
    return {
        "primaryTableArn": primary_arn,
        "restoredTableArn": restored_arn,
        "cutoverPhases": cutover_phases,
        "rollbackPhases": rollback_phases,
        "cutoverSelectedTableArn": restored_arn,
        "rollbackSelectedTableArn": primary_arn,
        "finalSelectedTableArn": primary_arn,
        "productionEndpointStatusAfter": "READY",
        "controlPlaneDesiredCountAfter": desired,
        "controlPlaneRunningCountAfter": running,
    }


def _security_event_observations(value: Any) -> dict[str, Any]:
    observations = _object(value, "security-event observations")
    _exact_fields(
        observations,
        {
            "configuredDestinationCount",
            "deliveredDestinationCount",
            "outboxMessagesAfterDelivery",
            "dlqMessagesAfterFailure",
            "dlqAlarmState",
            "redrivenMessageCount",
            "dlqMessagesAfterRedrive",
            "outboxMessagesAfterRedrive",
        },
        "security-event observations",
    )
    configured = _integer(
        observations.get("configuredDestinationCount"),
        "configured security-event destination count",
        minimum=1,
        maximum=1000,
    )
    delivered = _integer(
        observations.get("deliveredDestinationCount"),
        "delivered security-event destination count",
        minimum=0,
        maximum=1000,
    )
    dead_letters = _integer(
        observations.get("dlqMessagesAfterFailure"),
        "security-event DLQ message count",
        minimum=1,
        maximum=100_000,
    )
    redriven = _integer(
        observations.get("redrivenMessageCount"),
        "redriven security-event message count",
        minimum=1,
        maximum=100_000,
    )
    outbox_after_delivery = _integer(
        observations.get("outboxMessagesAfterDelivery"),
        "security-event outbox count after delivery",
        minimum=0,
        maximum=100_000,
    )
    dlq_after_redrive = _integer(
        observations.get("dlqMessagesAfterRedrive"),
        "security-event DLQ count after redrive",
        minimum=0,
        maximum=100_000,
    )
    outbox_after_redrive = _integer(
        observations.get("outboxMessagesAfterRedrive"),
        "security-event outbox count after redrive",
        minimum=0,
        maximum=100_000,
    )
    if (
        delivered != configured
        or outbox_after_delivery != 0
        or observations.get("dlqAlarmState") != "ALARM"
        or redriven < dead_letters
        or dlq_after_redrive != 0
        or outbox_after_redrive != 0
    ):
        raise LaunchRehearsalError("security-event delivery, DLQ alarm, or redrive did not pass")
    return {
        "configuredDestinationCount": configured,
        "deliveredDestinationCount": delivered,
        "outboxMessagesAfterDelivery": 0,
        "dlqMessagesAfterFailure": dead_letters,
        "dlqAlarmState": "ALARM",
        "redrivenMessageCount": redriven,
        "dlqMessagesAfterRedrive": 0,
        "outboxMessagesAfterRedrive": 0,
    }


def _routing_observations(value: Any) -> dict[str, Any]:
    observations = _object(value, "provider-routing observations")
    _exact_fields(
        observations,
        {
            "strategiesExercised",
            "candidateProviders",
            "observedProviders",
            "requestCount",
            "successfulRequestCount",
        },
        "provider-routing observations",
    )
    if observations.get("strategiesExercised") != list(ROUTING_STRATEGIES):
        raise LaunchRehearsalError("provider-routing evidence does not cover every launch strategy")
    candidates = _provider_list(
        observations.get("candidateProviders"),
        "candidate providers",
        minimum=2,
    )
    observed = _provider_list(
        observations.get("observedProviders"),
        "observed providers",
        minimum=2,
    )
    request_count = _integer(
        observations.get("requestCount"),
        "provider-routing request count",
        minimum=len(ROUTING_STRATEGIES),
        maximum=1_000_000,
    )
    successful = _integer(
        observations.get("successfulRequestCount"),
        "successful provider-routing request count",
        minimum=0,
        maximum=1_000_000,
    )
    if not set(observed).issubset(candidates) or successful != request_count:
        raise LaunchRehearsalError("provider routing did not pass")
    return {
        "strategiesExercised": list(ROUTING_STRATEGIES),
        "candidateProviders": candidates,
        "observedProviders": observed,
        "requestCount": request_count,
        "successfulRequestCount": successful,
    }


def _provider_name(value: Any, location: str) -> str:
    provider = _string(value, location, maximum=64)
    if PROVIDER.fullmatch(provider) is None:
        raise LaunchRehearsalError(f"{location} is malformed")
    return provider


def _fallback_observations(value: Any) -> dict[str, Any]:
    observations = _object(value, "provider-fallback observations")
    _exact_fields(
        observations,
        {
            "primaryProvider",
            "fallbackProvider",
            "observedProvider",
            "injectedFailureStatusCode",
            "fallbackResponseStatusCode",
            "postRecoveryStatusCode",
            "primaryAttemptCount",
            "fallbackAttemptCount",
        },
        "provider-fallback observations",
    )
    primary = _provider_name(
        observations.get("primaryProvider"),
        "primary provider",
    )
    fallback = _provider_name(
        observations.get("fallbackProvider"),
        "fallback provider",
    )
    observed = _provider_name(
        observations.get("observedProvider"),
        "observed fallback provider",
    )
    failure_status = _integer(
        observations.get("injectedFailureStatusCode"),
        "injected provider failure status",
        minimum=400,
        maximum=599,
    )
    primary_attempts = _integer(
        observations.get("primaryAttemptCount"),
        "primary provider attempt count",
        minimum=1,
        maximum=100,
    )
    fallback_attempts = _integer(
        observations.get("fallbackAttemptCount"),
        "fallback provider attempt count",
        minimum=1,
        maximum=100,
    )
    if (
        primary == fallback
        or observed != fallback
        or failure_status not in {429, 500, 502, 503, 504}
        or observations.get("fallbackResponseStatusCode") != 200
        or observations.get("postRecoveryStatusCode") != 200
    ):
        raise LaunchRehearsalError("provider fallback or post-fault recovery did not pass")
    return {
        "primaryProvider": primary,
        "fallbackProvider": fallback,
        "observedProvider": observed,
        "injectedFailureStatusCode": failure_status,
        "fallbackResponseStatusCode": 200,
        "postRecoveryStatusCode": 200,
        "primaryAttemptCount": primary_attempts,
        "fallbackAttemptCount": fallback_attempts,
    }


def _control_fault_observations(value: Any) -> dict[str, Any]:
    observations = _object(value, "control-plane fault observations")
    _exact_fields(
        observations,
        {
            "faultedDependency",
            "readyDuringFaultStatusCode",
            "readDuringFaultStatusCode",
            "mutationDuringFaultStatusCode",
            "readyAfterRecoveryStatusCode",
            "readAfterRecoveryStatusCode",
        },
        "control-plane fault observations",
    )
    dependency = observations.get("faultedDependency")
    if dependency not in {
        "dynamodb",
        "secrets-manager",
        "security-event-outbox",
    }:
        raise LaunchRehearsalError("control-plane faulted dependency is unsupported")
    if (
        observations.get("readyDuringFaultStatusCode") != 503
        or observations.get("readDuringFaultStatusCode") != 503
        or observations.get("mutationDuringFaultStatusCode") != 503
        or observations.get("readyAfterRecoveryStatusCode") != 200
        or observations.get("readAfterRecoveryStatusCode") != 200
    ):
        raise LaunchRehearsalError("control-plane dependency fault did not fail closed and recover")
    return {
        "faultedDependency": dependency,
        "readyDuringFaultStatusCode": 503,
        "readDuringFaultStatusCode": 503,
        "mutationDuringFaultStatusCode": 503,
        "readyAfterRecoveryStatusCode": 200,
        "readAfterRecoveryStatusCode": 200,
    }


def _validate_observations(
    gate_name: str,
    value: Any,
    *,
    release: Mapping[str, str],
) -> dict[str, Any]:
    if gate_name == "initializationTimeoutReplacement":
        return _initialization_observations(value)
    if gate_name == "recoveryCutoverAndRollback":
        return _recovery_observations(value, release=release)
    if gate_name == "securityEventDeliveryAndDlq":
        return _security_event_observations(value)
    if gate_name == "providerRoutingStrategies":
        return _routing_observations(value)
    if gate_name == "providerFallbackRecovery":
        return _fallback_observations(value)
    if gate_name == "controlPlaneFaultRecovery":
        return _control_fault_observations(value)
    raise LaunchRehearsalError(f"unsupported launch gate: {gate_name}")


def _validate_gate_receipt(
    value: Any,
    *,
    gate_name: str,
    source: ValidatedSource,
    now: datetime,
    evidence_bucket: str,
    evidence_prefix: str,
    command_outputs: CommandOutputs,
    script_checker: ScriptChecker,
) -> dict[str, Any]:
    receipt = _object(value, f"{gate_name} receipt")
    _exact_fields(
        receipt,
        {
            "schema",
            "gate",
            "release",
            "execution",
            "environment",
            "commands",
        },
        f"{gate_name} receipt",
    )
    if receipt.get("schema") != GATE_SCHEMA or receipt.get("gate") != gate_name:
        raise LaunchRehearsalError(f"{gate_name} receipt identity is invalid")
    release = _validate_release(receipt.get("release"))
    execution = _validate_execution(
        receipt.get("execution"),
        release_commit=release["commit"],
    )
    if release != source.release or execution != source.execution or receipt.get("environment") != "production":
        raise LaunchRehearsalError(f"{gate_name} receipt is not bound to this production gate run")
    (
        commands,
        raw_observations,
        started_text,
        completed_text,
        started,
        completed,
    ) = _validate_commands(
        receipt.get("commands"),
        gate_name=gate_name,
        release=release,
        execution=execution,
        evidence_bucket=evidence_bucket,
        evidence_prefix=evidence_prefix,
        command_outputs=command_outputs,
        script_checker=script_checker,
    )
    if completed > now or now - completed > MAX_GATE_AGE or started > completed:
        raise LaunchRehearsalError(f"{gate_name} command receipt time is outside the accepted window")
    observations = _validate_observations(
        gate_name,
        raw_observations,
        release=release,
    )
    return {
        "startedAt": started_text,
        "completedAt": completed_text,
        "commandReceiptsSha256": _canonical_sha(commands),
        "observationsSha256": _canonical_sha(observations),
    }


def _evidence_id(reference: S3Reference) -> str:
    value = f"{reference.uri}?versionId={quote(reference.version_id, safe='')}&sha256={reference.sha256}"
    if len(value) > 512:
        raise LaunchRehearsalError("immutable gate evidence identifier is too long for compatibility")
    return value


def _expected_release(
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    control_plane_image: str,
) -> dict[str, str]:
    return _validate_release(
        {
            "commit": release_commit,
            "region": region,
            "agentcoreImage": agentcore_image,
            "controlPlaneImage": control_plane_image,
        }
    )


def _validate_detailed_report(
    value: Any,
    *,
    expected_release: Mapping[str, str],
    expected_producer: Mapping[str, str],
    evidence_bucket: str,
    evidence_prefix: str,
) -> dict[str, Any]:
    report = _object(value, "launch-rehearsal evidence report")
    _exact_fields(
        report,
        {
            "schema",
            "releaseCommit",
            "region",
            "agentcoreImage",
            "controlPlaneImage",
            "generatedAt",
            "producer",
            "gateExecution",
            "sourceManifest",
            "gates",
        },
        "launch-rehearsal evidence report",
    )
    if (
        report.get("schema") != REPORT_SCHEMA
        or report.get("releaseCommit") != expected_release["commit"]
        or report.get("region") != expected_release["region"]
        or report.get("agentcoreImage") != expected_release["agentcoreImage"]
        or report.get("controlPlaneImage") != expected_release["controlPlaneImage"]
    ):
        raise LaunchRehearsalError("launch-rehearsal report is not bound to the requested release")
    generated_text, generated = _timestamp(
        report.get("generatedAt"),
        "launch-rehearsal generatedAt",
    )
    producer = _validate_producer(report.get("producer"))
    if producer != dict(expected_producer):
        raise LaunchRehearsalError("launch-rehearsal producer identity does not match expectation")
    gate_execution = _validate_execution(
        report.get("gateExecution"),
        release_commit=expected_release["commit"],
    )
    source_raw = _object(
        report.get("sourceManifest"),
        "launch-rehearsal source manifest",
    )
    _exact_fields(
        source_raw,
        {"artifact", "signature"},
        "launch-rehearsal source manifest",
    )
    source_pair = ArtifactPair(
        artifact=_validate_reference(
            source_raw.get("artifact"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location="source manifest artifact",
        ),
        signature=_validate_reference(
            source_raw.get("signature"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location="source manifest signature",
        ),
    )
    raw_gates = _object(report.get("gates"), "launch-rehearsal gates")
    if set(raw_gates) != set(ALL_GATES):
        raise LaunchRehearsalError("launch-rehearsal report is missing required gates")
    normalized_gates: dict[str, Any] = {}
    identities = {
        (source_pair.artifact.uri, source_pair.artifact.version_id),
        (source_pair.signature.uri, source_pair.signature.version_id),
    }
    if len(identities) != 2:
        raise LaunchRehearsalError("launch-rehearsal source artifact and signature are not distinct")
    for gate_name in sorted(ALL_GATES):
        gate = _object(raw_gates.get(gate_name), f"report gate {gate_name}")
        _exact_fields(
            gate,
            {
                "status",
                "environment",
                "startedAt",
                "completedAt",
                "commandReceiptsSha256",
                "observationsSha256",
                "artifact",
                "signature",
            },
            f"report gate {gate_name}",
        )
        started_text, started = _timestamp(
            gate.get("startedAt"),
            f"{gate_name} startedAt",
        )
        completed_text, completed = _timestamp(
            gate.get("completedAt"),
            f"{gate_name} completedAt",
        )
        command_sha = _string(
            gate.get("commandReceiptsSha256"),
            f"{gate_name} commandReceiptsSha256",
            maximum=64,
        )
        observation_sha = _string(
            gate.get("observationsSha256"),
            f"{gate_name} observationsSha256",
            maximum=64,
        )
        artifact = _validate_reference(
            gate.get("artifact"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location=f"{gate_name} artifact",
        )
        signature = _validate_reference(
            gate.get("signature"),
            expected_bucket=evidence_bucket,
            expected_prefix=evidence_prefix,
            location=f"{gate_name} signature",
        )
        for reference in (artifact, signature):
            identity = (reference.uri, reference.version_id)
            if identity in identities:
                raise LaunchRehearsalError("launch-rehearsal report reuses an immutable object version")
            identities.add(identity)
        if (
            gate.get("status") != "PASS"
            or gate.get("environment") != "production"
            or SHA256.fullmatch(command_sha) is None
            or SHA256.fullmatch(observation_sha) is None
            or completed < started
            or completed > generated
        ):
            raise LaunchRehearsalError(f"launch-rehearsal gate {gate_name} is invalid")
        normalized_gates[gate_name] = {
            "status": "PASS",
            "environment": "production",
            "startedAt": started_text,
            "completedAt": completed_text,
            "commandReceiptsSha256": command_sha,
            "observationsSha256": observation_sha,
            "artifact": artifact.report_value(),
            "signature": signature.report_value(),
        }
    normalized = {
        "schema": REPORT_SCHEMA,
        "releaseCommit": expected_release["commit"],
        "region": expected_release["region"],
        "agentcoreImage": expected_release["agentcoreImage"],
        "controlPlaneImage": expected_release["controlPlaneImage"],
        "generatedAt": generated_text,
        "producer": producer,
        "gateExecution": gate_execution,
        "sourceManifest": source_pair.report_value(),
        "gates": normalized_gates,
    }
    if normalized != report:
        raise LaunchRehearsalError("launch-rehearsal evidence report is not normalized")
    return normalized


def validate_detailed_report(
    value: Any,
    *,
    release_commit: str,
    region: str,
    agentcore_image: str,
    control_plane_image: str,
    repository: str,
    evidence_bucket: str,
    evidence_prefix: str,
) -> dict[str, Any]:
    """Validate detailed evidence from the protected rehearsal producer."""

    normalized_repository = _validate_repository(
        repository,
        "expected repository",
    )
    producer = _validate_producer(
        _object(value, "launch-rehearsal evidence report").get("producer"),
        release_commit=release_commit,
    )
    expected_workflow_ref = f"{normalized_repository}/{PRODUCER_WORKFLOW}@refs/heads/main"
    expected_parent_workflow_ref = f"{normalized_repository}/{LAUNCH_WORKFLOW}@refs/heads/main"
    if (
        producer["repository"] != normalized_repository
        or producer["workflowRef"] != expected_workflow_ref
        or producer["parentWorkflowRef"] != expected_parent_workflow_ref
    ):
        raise LaunchRehearsalError("launch-rehearsal report was not created by the protected producer workflow")
    normalized = _validate_detailed_report(
        value,
        expected_release=_expected_release(
            release_commit=release_commit,
            region=region,
            agentcore_image=agentcore_image,
            control_plane_image=control_plane_image,
        ),
        expected_producer=producer,
        evidence_bucket=evidence_bucket,
        evidence_prefix=_validate_prefix(evidence_prefix),
    )
    gate_execution = normalized["gateExecution"]
    if (
        gate_execution["repository"] != normalized_repository
        or gate_execution["parentWorkflowRef"] != expected_parent_workflow_ref
        or gate_execution["runId"] != producer["runId"]
        or gate_execution["runAttempt"] != producer["runAttempt"]
    ):
        raise LaunchRehearsalError("launch-rehearsal gate execution is not bound to the protected parent run")
    return normalized


def _validate_compatibility_report(
    value: Any,
    *,
    detailed_report: Mapping[str, Any],
) -> dict[str, Any]:
    report = _object(value, "compatibility launch-rehearsal report")
    _exact_fields(
        report,
        {
            "schema",
            "releaseCommit",
            "region",
            "agentcoreImage",
            "controlPlaneImage",
            "generatedAt",
            "gates",
        },
        "compatibility launch-rehearsal report",
    )
    if (
        report.get("schema") != COMPATIBILITY_SCHEMA
        or report.get("releaseCommit") != detailed_report["releaseCommit"]
        or report.get("region") != detailed_report["region"]
        or report.get("agentcoreImage") != detailed_report["agentcoreImage"]
        or report.get("controlPlaneImage") != detailed_report["controlPlaneImage"]
        or report.get("generatedAt") != detailed_report["generatedAt"]
    ):
        raise LaunchRehearsalError("compatibility report is not bound to detailed evidence")
    raw_gates = _object(report.get("gates"), "compatibility gates")
    if set(raw_gates) != set(CORE_GATES):
        raise LaunchRehearsalError("compatibility report must contain the four deployment gates")
    normalized_gates: dict[str, Any] = {}
    for gate_name in sorted(CORE_GATES):
        gate = _object(
            raw_gates.get(gate_name),
            f"compatibility gate {gate_name}",
        )
        _exact_fields(
            gate,
            {"status", "environment", "completedAt", "evidenceId"},
            f"compatibility gate {gate_name}",
        )
        detailed_gate = detailed_report["gates"][gate_name]
        artifact = detailed_gate["artifact"]
        reference = S3Reference(
            uri=artifact["s3Uri"],
            bucket=urlsplit(artifact["s3Uri"]).netloc,
            key=urlsplit(artifact["s3Uri"]).path.removeprefix("/"),
            version_id=artifact["versionId"],
            sha256=artifact["sha256"],
        )
        expected_id = _evidence_id(reference)
        if (
            gate.get("status") != "PASS"
            or gate.get("environment") != "production"
            or gate.get("completedAt") != detailed_gate["completedAt"]
            or gate.get("evidenceId") != expected_id
        ):
            raise LaunchRehearsalError(f"compatibility gate {gate_name} is not immutable evidence")
        normalized_gates[gate_name] = {
            "status": "PASS",
            "environment": "production",
            "completedAt": detailed_gate["completedAt"],
            "evidenceId": expected_id,
        }
    normalized = {
        "schema": COMPATIBILITY_SCHEMA,
        "releaseCommit": detailed_report["releaseCommit"],
        "region": detailed_report["region"],
        "agentcoreImage": detailed_report["agentcoreImage"],
        "controlPlaneImage": detailed_report["controlPlaneImage"],
        "generatedAt": detailed_report["generatedAt"],
        "gates": normalized_gates,
    }
    if normalized != report:
        raise LaunchRehearsalError("compatibility launch-rehearsal report is not normalized")
    return normalized


def verify_reports(
    detailed: Any,
    compatibility: Any,
    *,
    expected_release: Mapping[str, str],
    expected_producer: Mapping[str, str],
    evidence_bucket: str,
    evidence_prefix: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Strictly verify both the detailed report and deployment projection."""

    prefix = _validate_prefix(evidence_prefix)
    if BUCKET.fullmatch(evidence_bucket) is None:
        raise LaunchRehearsalError("evidence bucket is malformed")
    normalized_detailed = _validate_detailed_report(
        detailed,
        expected_release=expected_release,
        expected_producer=expected_producer,
        evidence_bucket=evidence_bucket,
        evidence_prefix=prefix,
    )
    normalized_compatibility = _validate_compatibility_report(
        compatibility,
        detailed_report=normalized_detailed,
    )
    return normalized_detailed, normalized_compatibility


def build_reports(
    source: ValidatedSource,
    receipts: Mapping[str, Any],
    *,
    terminal: Any,
    command_outputs: CommandOutputs,
    script_checker: ScriptChecker,
    source_pair: ArtifactPair,
    producer: Mapping[str, str],
    evidence_bucket: str,
    evidence_prefix: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive detailed and deployment-compatible reports from signed receipts."""

    if set(receipts) != set(ALL_GATES):
        raise LaunchRehearsalError("signed receipt set must contain every required launch gate")
    if now.tzinfo is None or now.utcoffset() is None:
        raise LaunchRehearsalError("report generation time must include timezone")
    now = now.astimezone(timezone.utc)
    normalized_terminal = _validate_terminal(
        terminal,
        source=source,
        now=now,
    )
    _, terminal_started = _timestamp(
        normalized_terminal["startedAt"],
        "launch-gate terminal startedAt",
    )
    _, terminal_completed = _timestamp(
        normalized_terminal["completedAt"],
        "launch-gate terminal completedAt",
    )
    normalized_producer = _validate_producer(
        producer,
        release_commit=source.release["commit"],
    )
    if source.execution["repository"] != normalized_producer["repository"]:
        raise LaunchRehearsalError("gate execution and evidence producer repositories differ")
    report_gates: dict[str, Any] = {}
    for gate_name in sorted(ALL_GATES):
        result = _validate_gate_receipt(
            receipts[gate_name],
            gate_name=gate_name,
            source=source,
            now=now,
            evidence_bucket=evidence_bucket,
            evidence_prefix=evidence_prefix,
            command_outputs=command_outputs,
            script_checker=script_checker,
        )
        _, gate_started = _timestamp(
            result["startedAt"],
            f"{gate_name} startedAt",
        )
        _, gate_completed = _timestamp(
            result["completedAt"],
            f"{gate_name} completedAt",
        )
        if gate_started < terminal_started or gate_completed > terminal_completed:
            raise LaunchRehearsalError(f"{gate_name} command receipt is outside the terminal run")
        pair = source.gates[gate_name]
        report_gates[gate_name] = {
            "status": "PASS",
            "environment": "production",
            **result,
            "artifact": pair.artifact.report_value(),
            "signature": pair.signature.report_value(),
        }
    generated_at = now.isoformat(timespec="seconds")
    detailed = {
        "schema": REPORT_SCHEMA,
        "releaseCommit": source.release["commit"],
        "region": source.release["region"],
        "agentcoreImage": source.release["agentcoreImage"],
        "controlPlaneImage": source.release["controlPlaneImage"],
        "generatedAt": generated_at,
        "producer": normalized_producer,
        "gateExecution": source.execution,
        "sourceManifest": source_pair.report_value(),
        "gates": report_gates,
    }
    compatibility = {
        "schema": COMPATIBILITY_SCHEMA,
        "releaseCommit": source.release["commit"],
        "region": source.release["region"],
        "agentcoreImage": source.release["agentcoreImage"],
        "controlPlaneImage": source.release["controlPlaneImage"],
        "generatedAt": generated_at,
        "gates": {
            gate_name: {
                "status": "PASS",
                "environment": "production",
                "completedAt": report_gates[gate_name]["completedAt"],
                "evidenceId": _evidence_id(source.gates[gate_name].artifact),
            }
            for gate_name in sorted(CORE_GATES)
        },
    }
    return verify_reports(
        detailed,
        compatibility,
        expected_release=source.release,
        expected_producer=normalized_producer,
        evidence_bucket=evidence_bucket,
        evidence_prefix=evidence_prefix,
    )


def _aws_json(arguments: Sequence[str]) -> dict[str, Any]:
    command = [
        "aws",
        *arguments,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=AWS_TIMEOUT_SECONDS,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        UnicodeError,
    ) as exc:
        raise LaunchRehearsalError("AWS evidence request failed") from exc
    if len(completed.stdout.encode("utf-8")) > MAX_AWS_RESPONSE_BYTES:
        raise LaunchRehearsalError("AWS evidence response is too large")
    try:
        value = json.loads(
            completed.stdout,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, LaunchRehearsalError) as exc:
        raise LaunchRehearsalError("AWS evidence response is not strict JSON") from exc
    if not isinstance(value, dict):
        raise LaunchRehearsalError("AWS evidence response must be a JSON object")
    return value


def _validate_download_metadata(
    metadata: Mapping[str, Any],
    *,
    reference: S3Reference,
    storage_key_arn: str,
    downloaded_size: int,
    now: datetime,
) -> None:
    retained_text, retained_until = _timestamp(
        metadata.get("ObjectLockRetainUntilDate"),
        "S3 Object Lock retain-until date",
    )
    content_length = metadata.get("ContentLength")
    if (
        metadata.get("VersionId") != reference.version_id
        or not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length != downloaded_size
        or metadata.get("ServerSideEncryption") != "aws:kms"
        or metadata.get("SSEKMSKeyId") != storage_key_arn
        or metadata.get("ObjectLockMode") != "COMPLIANCE"
        or retained_until <= now
        or not retained_text
    ):
        raise LaunchRehearsalError("S3 object is not the required immutable COMPLIANCE version")


def _fetch_immutable_reference(
    reference: S3Reference,
    destination: Path,
    storage_key_arn: str,
    now: datetime,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = _aws_json(
        (
            "s3api",
            "get-object",
            "--bucket",
            reference.bucket,
            "--key",
            reference.key,
            "--version-id",
            reference.version_id,
            "--checksum-mode",
            "ENABLED",
            str(destination),
        )
    )
    size = len(_read_regular(destination))
    _validate_download_metadata(
        metadata,
        reference=reference,
        storage_key_arn=storage_key_arn,
        downloaded_size=size,
        now=now,
    )
    if _hash_file(destination) != reference.sha256:
        raise LaunchRehearsalError("downloaded S3 object does not match its SHA-256 binding")


def _verify_kms_signature(
    artifact: Path,
    signature: Path,
    signing_key_arn: str,
) -> None:
    try:
        from kms_evidence import KmsEvidenceError, verify_artifact

        verify_artifact(artifact, signature, signing_key_arn)
    except (ImportError, KmsEvidenceError) as exc:
        raise LaunchRehearsalError("KMS signature verification failed") from exc


def produce_reports(
    *,
    source_pair: ArtifactPair,
    expected_release: Mapping[str, str],
    producer: Mapping[str, str],
    evidence_bucket: str,
    evidence_prefix: str,
    storage_key_arn: str,
    signing_key_arn: str,
    output: Path,
    compatibility_output: Path,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    signature_verifier: SignatureVerifier | None = None,
    script_checker: ScriptChecker | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch, verify, normalize, and write the signed launch evidence."""

    prefix = _validate_prefix(evidence_prefix)
    if BUCKET.fullmatch(evidence_bucket) is None:
        raise LaunchRehearsalError("evidence bucket is malformed")
    release = _validate_release(expected_release)
    storage_key = _validate_kms_key(
        storage_key_arn,
        release=release,
        location="evidence storage KMS key",
    )
    signing_key = _validate_kms_key(
        signing_key_arn,
        release=release,
        location="evidence signing KMS key",
    )
    if storage_key == signing_key:
        raise LaunchRehearsalError("storage and asymmetric signing KMS keys must be distinct")
    normalized_producer = _validate_producer(producer)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fetch = fetcher or _fetch_immutable_reference
    verify_signature = signature_verifier or _verify_kms_signature
    check_script = script_checker or _verify_script_at_commit

    with tempfile.TemporaryDirectory(prefix="axonllm-launch-rehearsal-") as directory:
        temporary = Path(directory)
        source_path = temporary / "gate-set.json"
        source_signature_path = temporary / "gate-set-kms-signature.json"
        fetch(source_pair.artifact, source_path, storage_key, current_time)
        fetch(
            source_pair.signature,
            source_signature_path,
            storage_key,
            current_time,
        )
        verify_signature(source_path, source_signature_path, signing_key)
        source = _validate_source_manifest(
            _read_json(source_path),
            expected_release=release,
            evidence_bucket=evidence_bucket,
            evidence_prefix=prefix,
        )

        receipts: dict[str, Any] = {}
        command_outputs: dict[tuple[str, int, str], bytes] = {}
        identities = {
            (source_pair.artifact.uri, source_pair.artifact.version_id),
            (source_pair.signature.uri, source_pair.signature.version_id),
        }
        for pair in (source.terminal, *source.gates.values()):
            for reference in (pair.artifact, pair.signature):
                identity = (reference.uri, reference.version_id)
                if identity in identities:
                    raise LaunchRehearsalError("launch evidence reuses an immutable object version")
                identities.add(identity)

        terminal_path = temporary / "attempt-terminal.json"
        terminal_signature_path = temporary / "attempt-terminal-kms-signature.json"
        fetch(
            source.terminal.artifact,
            terminal_path,
            storage_key,
            current_time,
        )
        fetch(
            source.terminal.signature,
            terminal_signature_path,
            storage_key,
            current_time,
        )
        verify_signature(
            terminal_path,
            terminal_signature_path,
            signing_key,
        )
        terminal = _read_json(terminal_path)
        _validate_terminal(
            terminal,
            source=source,
            now=current_time,
        )
        for gate_name in sorted(ALL_GATES):
            pair = source.gates[gate_name]
            artifact_path = temporary / f"{gate_name}.json"
            signature_path = temporary / f"{gate_name}-kms-signature.json"
            fetch(pair.artifact, artifact_path, storage_key, current_time)
            fetch(pair.signature, signature_path, storage_key, current_time)
            verify_signature(artifact_path, signature_path, signing_key)
            receipts[gate_name] = _read_json(artifact_path)
            for index, stream, reference in _command_output_references(
                receipts[gate_name],
                gate_name=gate_name,
                evidence_bucket=evidence_bucket,
                evidence_prefix=prefix,
            ):
                identity = (reference.uri, reference.version_id)
                if identity in identities:
                    raise LaunchRehearsalError("launch command outputs reuse an immutable object version")
                identities.add(identity)
                output_path = temporary / f"{gate_name}-{index + 1}-{stream}.bin"
                fetch(reference, output_path, storage_key, current_time)
                command_outputs[(gate_name, index, stream)] = _read_regular(
                    output_path,
                    maximum=MAX_COMMAND_OUTPUT_BYTES,
                )

        detailed, compatibility = build_reports(
            source,
            receipts,
            terminal=terminal,
            command_outputs=command_outputs,
            script_checker=check_script,
            source_pair=source_pair,
            producer=normalized_producer,
            evidence_bucket=evidence_bucket,
            evidence_prefix=prefix,
            now=current_time,
        )
    _atomic_write(output, detailed)
    _atomic_write(compatibility_output, compatibility)
    return detailed, compatibility


def _producer_from_args(args: argparse.Namespace) -> dict[str, str]:
    return _validate_producer(
        {
            "repository": args.repository,
            "workflowRef": args.workflow_ref,
            "workflowCommit": args.workflow_commit,
            "parentWorkflowRef": args.parent_workflow_ref,
            "parentWorkflowCommit": args.parent_workflow_commit,
            "runId": args.run_id,
            "runAttempt": args.run_attempt,
        },
        release_commit=args.release_commit,
    )


def _release_from_args(args: argparse.Namespace) -> dict[str, str]:
    return _expected_release(
        release_commit=args.release_commit,
        region=args.region,
        agentcore_image=args.agentcore_image,
        control_plane_image=args.control_plane_image,
    )


def _common_expected(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--agentcore-image", required=True)
    parser.add_argument("--control-plane-image", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--parent-workflow-ref", required=True)
    parser.add_argument("--parent-workflow-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--evidence-bucket", required=True)
    parser.add_argument("--evidence-prefix", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Produce or verify signed, immutable AgentCore launch-rehearsal evidence")
    )
    commands = parser.add_subparsers(dest="command", required=True)

    produce = commands.add_parser("produce")
    _common_expected(produce)
    produce.add_argument("--source-manifest-uri", required=True)
    produce.add_argument("--source-manifest-version-id", required=True)
    produce.add_argument("--source-manifest-sha256", required=True)
    produce.add_argument("--source-signature-uri", required=True)
    produce.add_argument("--source-signature-version-id", required=True)
    produce.add_argument("--source-signature-sha256", required=True)
    produce.add_argument("--storage-kms-key-arn", required=True)
    produce.add_argument("--signing-key-arn", required=True)
    produce.add_argument("--output", required=True, type=Path)
    produce.add_argument(
        "--compatibility-output",
        required=True,
        type=Path,
    )

    verify = commands.add_parser("verify")
    _common_expected(verify)
    verify.add_argument("--report", required=True, type=Path)
    verify.add_argument(
        "--compatibility-report",
        required=True,
        type=Path,
    )

    verify_detailed = commands.add_parser("verify-detailed")
    verify_detailed.add_argument("--release-commit", required=True)
    verify_detailed.add_argument("--region", required=True)
    verify_detailed.add_argument("--agentcore-image", required=True)
    verify_detailed.add_argument("--control-plane-image", required=True)
    verify_detailed.add_argument("--repository", required=True)
    verify_detailed.add_argument("--evidence-bucket", required=True)
    verify_detailed.add_argument("--evidence-prefix", required=True)
    verify_detailed.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        release = _release_from_args(args)
        prefix = _validate_prefix(args.evidence_prefix)
        if args.command == "verify-detailed":
            validate_detailed_report(
                _read_json(args.report),
                release_commit=release["commit"],
                region=release["region"],
                agentcore_image=release["agentcoreImage"],
                control_plane_image=release["controlPlaneImage"],
                repository=args.repository,
                evidence_bucket=args.evidence_bucket,
                evidence_prefix=prefix,
            )
            print(f"detailed launch-rehearsal evidence verified: {args.report}")
        elif args.command == "produce":
            producer = _producer_from_args(args)
            source_pair = ArtifactPair(
                artifact=_reference_from_arguments(
                    uri=args.source_manifest_uri,
                    version_id=args.source_manifest_version_id,
                    sha256=args.source_manifest_sha256,
                    expected_bucket=args.evidence_bucket,
                    expected_prefix=prefix,
                    location="source manifest artifact",
                ),
                signature=_reference_from_arguments(
                    uri=args.source_signature_uri,
                    version_id=args.source_signature_version_id,
                    sha256=args.source_signature_sha256,
                    expected_bucket=args.evidence_bucket,
                    expected_prefix=prefix,
                    location="source manifest signature",
                ),
            )
            produce_reports(
                source_pair=source_pair,
                expected_release=release,
                producer=producer,
                evidence_bucket=args.evidence_bucket,
                evidence_prefix=prefix,
                storage_key_arn=args.storage_kms_key_arn,
                signing_key_arn=args.signing_key_arn,
                output=args.output,
                compatibility_output=args.compatibility_output,
            )
            print(f"launch-rehearsal evidence written: {args.output}")
        else:
            producer = _producer_from_args(args)
            verify_reports(
                _read_json(args.report),
                _read_json(args.compatibility_report),
                expected_release=release,
                expected_producer=producer,
                evidence_bucket=args.evidence_bucket,
                evidence_prefix=prefix,
            )
            print(f"launch-rehearsal evidence verified: {args.report}")
    except LaunchRehearsalError as exc:
        print(f"launch-rehearsal evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
