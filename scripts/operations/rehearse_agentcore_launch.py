#!/usr/bin/env python3
"""Run release-bound, resumable AgentCore production launch rehearsals."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent
RELEASE_DIR = SCRIPT_DIR.parent / "release"
for import_path in (SCRIPT_DIR, RELEASE_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import launch_rehearsal_evidence as launch_evidence  # noqa: E402


CONFIG_SCHEMA = "axonllm.agentcore-launch-rehearsal-operation-config/v2"
STATE_SCHEMA = "axonllm.agentcore-launch-rehearsal-operation-state/v1"
ACTION_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-action/v1"
ACTION_RESULT_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-result/v1"
ACTION_RETRY_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-retry/v1"
COORDINATOR_NOT_DRAINED_EXIT_CODE = 4
COORDINATOR_DRAIN_TIMEOUT_SECONDS = 120.0
COMMAND_OUTPUT_SCHEMA = launch_evidence.COMMAND_OUTPUT_SCHEMA
EXPECTED_COMMANDS = launch_evidence.EXPECTED_COMMANDS
ALL_GATES = tuple(EXPECTED_COMMANDS)
ALL_ACTIONS = frozenset(action for commands in EXPECTED_COMMANDS.values() for action in commands)
ROUTING_STRATEGIES = tuple(launch_evidence.ROUTING_STRATEGIES)

MAX_CONFIG_BYTES = 256 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_REVIEW_LIFETIME = timedelta(hours=48)
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_CLEANUP_AFTER_EXPIRY = timedelta(days=7)
MAX_COORDINATOR_OUTPUT_BYTES = 256 * 1024
STATE_FILE = "journal.json"
LOCK_FILE = ".launch-rehearsal.lock"

SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT = re.compile(r"^[0-9]{12}$")
REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z0-9-]+-[1-9][0-9]*$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_NUMBER = re.compile(r"^[1-9][0-9]*$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}$")
RETRY_CODE = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
BUCKET = re.compile(
    r"^(?![0-9]+(?:\.[0-9]+){3}$)"
    r"(?!.*\.\.)(?!.*\.-)(?!.*-\.)"
    r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)
S3_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
VERSION_ID = re.compile(r"^[A-Za-z0-9._~+/=-]{1,1024}$")
ECR_IMAGE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@"
    r"sha256:(?P<digest>[0-9a-f]{64})$"
)
RUNTIME_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"runtime/(?P<runtime>[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10})$"
)
ENDPOINT_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"runtime/(?P<runtime>[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10})/"
    r"runtime-endpoint/(?P<endpoint>[A-Za-z0-9_-]{1,128})$"
)
STACK_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):cloudformation:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"stack/(?P<name>[A-Za-z][A-Za-z0-9-]{0,127})/"
    r"(?P<id>[A-Za-z0-9-]{8,64})$"
)
TABLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):dynamodb:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"table/(?P<name>[A-Za-z0-9_.-]{3,255})$"
)
QUEUE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):sqs:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"(?P<name>[A-Za-z0-9_-]{1,80}(?:\.fifo)?)$"
)
ALARM_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):cloudwatch:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"alarm:(?P<name>[^\x00-\x1f\x7f:*?]{1,255})$"
)
STATE_MACHINE_VERSION_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):states:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"stateMachine:(?P<name>[A-Za-z0-9_-]{1,80}):"
    r"(?P<version>[1-9][0-9]{0,9})$"
)
IAM_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):iam::"
    r"(?P<account>[0-9]{12}):role/"
    r"(?P<name>[A-Za-z0-9+=,.@_/-]{1,512})$"
)
KMS_KEY_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z]+)?):kms:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"key/[A-Za-z0-9-]{1,256}$"
)
SECRET_FIELD = re.compile(
    r"(?i)(?:password|secret|api.?key|access.?token|refresh.?token|"
    r"authorization|private.?key|client.?secret|session.?cookie|bearer.?token)"
)
CONTROL_PLANE_DEPENDENCIES = frozenset({"dynamodb", "secrets-manager", "security-event-outbox"})


ACTION_EVIDENCE_FIELDS: Mapping[str, frozenset[str]] = {
    "induce-initialization-timeout": frozenset({"startupDeadlineSeconds", "timedOutRuntimeId"}),
    "observe-exit-124": frozenset({"timeoutExitCode"}),
    "observe-runtime-replacement": frozenset({"replacementRuntimeId"}),
    "verify-replacement-ready": frozenset({"replacementReadyStatusCode"}),
    "restore-state": frozenset({"primaryTableArn", "restoredTableArn"}),
    "cutover-restored-state": frozenset({"cutoverPhases", "cutoverSelectedTableArn"}),
    "verify-restored-state": frozenset(),
    "rollback-primary-state": frozenset({"rollbackPhases", "rollbackSelectedTableArn"}),
    "verify-primary-state": frozenset(
        {
            "finalSelectedTableArn",
            "productionEndpointStatusAfter",
            "controlPlaneDesiredCountAfter",
            "controlPlaneRunningCountAfter",
        }
    ),
    "deliver-security-events": frozenset({"configuredDestinationCount", "deliveredDestinationCount"}),
    "verify-outbox-drained": frozenset({"outboxMessagesAfterDelivery"}),
    "force-dead-letter": frozenset({"dlqMessagesAfterFailure"}),
    "verify-dead-letter-alarm": frozenset({"dlqAlarmState"}),
    "redrive-dead-letter": frozenset({"redrivenMessageCount"}),
    "verify-redelivery": frozenset({"dlqMessagesAfterRedrive", "outboxMessagesAfterRedrive"}),
    "exercise-routing-strategies": frozenset({"strategiesExercised", "candidateProviders", "requestCount"}),
    "verify-routing-decisions": frozenset({"observedProviders", "successfulRequestCount"}),
    "inject-primary-provider-fault": frozenset(
        {
            "primaryProvider",
            "fallbackProvider",
            "injectedFailureStatusCode",
            "primaryAttemptCount",
        }
    ),
    "verify-provider-fallback": frozenset(
        {
            "observedProvider",
            "fallbackResponseStatusCode",
            "fallbackAttemptCount",
        }
    ),
    "clear-primary-provider-fault": frozenset(),
    "verify-primary-provider-recovery": frozenset({"postRecoveryStatusCode"}),
    "inject-control-plane-fault": frozenset({"faultedDependency"}),
    "verify-control-plane-fail-closed": frozenset(
        {
            "readyDuringFaultStatusCode",
            "readDuringFaultStatusCode",
            "mutationDuringFaultStatusCode",
        }
    ),
    "clear-control-plane-fault": frozenset(),
    "verify-control-plane-recovery": frozenset({"readyAfterRecoveryStatusCode", "readAfterRecoveryStatusCode"}),
}

FAULT_ADD_ACTIONS = frozenset(
    {
        "induce-initialization-timeout",
        "inject-primary-provider-fault",
        "inject-control-plane-fault",
    }
)
FAULT_REMOVE_ACTIONS = frozenset(
    {
        "observe-runtime-replacement",
        "clear-primary-provider-fault",
        "clear-control-plane-fault",
    }
)
FIXTURE_ADD_ACTIONS = frozenset(
    {
        "restore-state",
        "deliver-security-events",
        "exercise-routing-strategies",
    }
)
DLQ_ADD_ACTIONS = frozenset({"force-dead-letter"})
DLQ_REMOVE_ACTIONS = frozenset({"redrive-dead-letter", "verify-redelivery"})


class LaunchOperationError(RuntimeError):
    """A credential-safe launch rehearsal failure."""


class ArgumentError(LaunchOperationError):
    """Raised for command-line errors without writing to stderr."""


class CoordinatorNotDrainedError(LaunchOperationError):
    """Raised when a possibly running coordinator execution cannot be drained."""


class AwsCallError(LaunchOperationError):
    """Sanitized AWS API failure retaining only its machine-readable code."""

    def __init__(self, service: str, code: str) -> None:
        self.service = service
        self.code = code
        super().__init__(f"AWS {service} operation failed")


class SilentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArgumentError(message)


@dataclass(frozen=True)
class Review:
    review_id: str
    reviewer: str
    reviewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Limits:
    request_timeout_seconds: float
    operation_timeout_seconds: float
    poll_interval_seconds: float
    max_attempts: int
    max_response_bytes: int


@dataclass(frozen=True)
class Resources:
    runtime_arn: str
    runtime_endpoint_arn: str
    runtime_endpoint_name: str
    agentcore_stack_arn: str
    control_plane_stack_arn: str
    state_table_arn: str
    restored_state_table_arn: str
    outbox_queue_arn: str
    outbox_queue_url: str
    dead_letter_queue_arn: str
    dead_letter_queue_url: str
    dead_letter_alarm_arn: str
    security_event_log_group_arn: str

    @property
    def agentcore_stack_name(self) -> str:
        match = STACK_ARN.fullmatch(self.agentcore_stack_arn)
        assert match is not None
        return match.group("name")

    @property
    def control_plane_stack_name(self) -> str:
        match = STACK_ARN.fullmatch(self.control_plane_stack_arn)
        assert match is not None
        return match.group("name")

    @property
    def state_table_name(self) -> str:
        match = TABLE_ARN.fullmatch(self.state_table_arn)
        assert match is not None
        return match.group("name")

    @property
    def restored_state_table_name(self) -> str:
        match = TABLE_ARN.fullmatch(self.restored_state_table_arn)
        assert match is not None
        return match.group("name")


@dataclass(frozen=True)
class Coordinator:
    state_machine_version_arn: str
    execution_role_arn: str
    launch_role_arn: str
    lease_table_arn: str
    watchdog_alarm_arn: str
    kms_key_arn: str
    cleanup_deadline_seconds: int

    @property
    def state_machine_base_arn(self) -> str:
        return self.state_machine_version_arn.rsplit(":", 1)[0]

    @property
    def state_machine_name(self) -> str:
        match = STATE_MACHINE_VERSION_ARN.fullmatch(self.state_machine_version_arn)
        assert match is not None
        return match.group("name")

    @property
    def lease_table_name(self) -> str:
        match = TABLE_ARN.fullmatch(self.lease_table_arn)
        assert match is not None
        return match.group("name")


@dataclass(frozen=True)
class Scenario:
    tenant_id: str
    project_id: str
    model: str
    primary_provider: str
    fallback_provider: str
    control_plane_fault: str
    startup_deadline_seconds: int
    fault_ttl_seconds: int


@dataclass(frozen=True)
class OperationConfig:
    account_id: str
    review: Review
    limits: Limits
    coordinator: Coordinator
    resources: Resources
    scenario: Scenario
    sha256: str


@dataclass(frozen=True)
class ReleaseBinding:
    release: dict[str, str]
    execution: dict[str, str]
    account_id: str
    owner_id: str


class AwsTransport(Protocol):
    """Bounded AWS API transport used only by fixed rehearsal operations."""

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class CoordinatorTransport(Protocol):
    """Durable out-of-band launch-rehearsal controller."""

    def invoke(
        self,
        payload: Mapping[str, Any],
        *,
        config: OperationConfig,
        binding: ReleaseBinding,
    ) -> Mapping[str, Any]: ...


class BotoAwsTransport:
    """Boto3 transport with SDK retries and finite connection/read timeouts."""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str, int], Any] = {}

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if not 0.1 <= timeout_seconds <= 1800:
            raise LaunchOperationError("AWS request timeout is outside policy")
        timeout = max(1, min(int(math.ceil(timeout_seconds)), 60))
        key = (service, region, timeout)
        client = self._clients.get(key)
        if client is None:
            try:
                import boto3
                from botocore.config import Config

                client = boto3.client(
                    service,
                    region_name=region,
                    config=Config(
                        connect_timeout=timeout,
                        read_timeout=timeout,
                        retries={
                            "max_attempts": 3,
                            "mode": "standard",
                        },
                    ),
                )
            except Exception as exc:
                raise LaunchOperationError("AWS client initialization failed") from exc
            self._clients[key] = client
        method = getattr(client, operation, None)
        if not callable(method):
            raise LaunchOperationError("unsupported AWS operation")
        try:
            response = method(**dict(parameters))
        except Exception as exc:
            raw_response = getattr(exc, "response", None)
            raw_error = raw_response.get("Error") if isinstance(raw_response, Mapping) else None
            code = raw_error.get("Code") if isinstance(raw_error, Mapping) else None
            raise AwsCallError(
                service,
                code if isinstance(code, str) and code else "Unknown",
            ) from exc
        if not isinstance(response, Mapping):
            raise LaunchOperationError(f"AWS {service} returned an invalid response")
        return response


def _object(value: Any, location: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise LaunchOperationError(f"{location} must be a JSON object")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str] | frozenset[str],
    location: str,
) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected).difference(actual))
        extra = sorted(actual.difference(expected))
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unsupported " + ", ".join(extra))
        raise LaunchOperationError(f"{location} fields are invalid: {'; '.join(details)}")


def _safe_string(
    value: Any,
    location: str,
    *,
    maximum: int = 2048,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise LaunchOperationError(f"{location} is not a safe non-empty string")
    return value


def _safe_id(value: Any, location: str) -> str:
    result = _safe_string(value, location, maximum=256)
    if SAFE_ID.fullmatch(result) is None:
        raise LaunchOperationError(f"{location} contains unsupported characters")
    return result


def _integer(
    value: Any,
    location: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LaunchOperationError(f"{location} must be an integer between {minimum} and {maximum}")
    return value


def _number(
    value: Any,
    location: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise LaunchOperationError(f"{location} must be between {minimum} and {maximum}")
    return float(value)


def _timestamp(value: Any, location: str) -> datetime:
    text = _safe_string(value, location, maximum=64)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LaunchOperationError(f"{location} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LaunchOperationError(f"{location} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LaunchOperationError("value is not canonical JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LaunchOperationError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise LaunchOperationError(f"invalid JSON constant: {value}")


def _read_regular(path: Path, *, maximum: int) -> bytes:
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    _assert_no_symlink_components(absolute, location="input")
    try:
        before = absolute.lstat()
    except OSError as exc:
        raise LaunchOperationError(f"cannot inspect input: {absolute}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
    ):
        raise LaunchOperationError(f"input must be an owner-only regular non-symlink file: {absolute}")
    if before.st_size > maximum:
        raise LaunchOperationError(f"input is too large: {absolute}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise LaunchOperationError(f"cannot read input: {absolute}") from exc
    if len(raw) > maximum:
        raise LaunchOperationError(f"input is too large: {absolute}")
    if (
        opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or len(raw) != after.st_size
    ):
        raise LaunchOperationError(f"input changed while being read: {absolute}")
    return raw


def _strict_json(raw: bytes, location: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except LaunchOperationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaunchOperationError(f"{location} is not strict UTF-8 JSON") from exc


def _reject_secret_fields(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            current = (*path, key)
            if SECRET_FIELD.search(key) is not None:
                raise LaunchOperationError("configuration contains a secret-like field")
            _reject_secret_fields(item, current)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item, path)


def _resource_arn(
    value: Any,
    pattern: re.Pattern[str],
    location: str,
    *,
    account_id: str,
    region: str,
    partition: str | None = None,
) -> tuple[str, re.Match[str]]:
    arn = _safe_string(value, location, maximum=1024)
    if "*" in arn or "?" in arn:
        raise LaunchOperationError(f"{location} must be an exact ARN")
    match = pattern.fullmatch(arn)
    if (
        match is None
        or match.group("account") != account_id
        or match.group("region") != region
        or (partition is not None and match.group("partition") != partition)
    ):
        raise LaunchOperationError(f"{location} is outside the exact release account and region")
    return arn, match


def _queue_url(
    value: Any,
    location: str,
    *,
    account_id: str,
    region: str,
    queue_name: str,
) -> str:
    url = _safe_string(value, location, maximum=1024)
    parsed = urlsplit(url)
    expected_hosts = {
        f"sqs.{region}.amazonaws.com",
        f"sqs.{region}.amazonaws.com.cn",
    }
    if (
        parsed.scheme != "https"
        or parsed.hostname not in expected_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/{account_id}/{queue_name}"
    ):
        raise LaunchOperationError(f"{location} is not the exact queue URL for its ARN")
    return url


def _parse_review(
    value: Any,
    *,
    now: datetime,
    allow_expired: bool,
) -> Review:
    review = _object(value, "configuration.review")
    _exact_fields(
        review,
        {"reviewId", "reviewer", "reviewedAt", "expiresAt"},
        "configuration.review",
    )
    reviewed_at = _timestamp(review["reviewedAt"], "review.reviewedAt")
    expires_at = _timestamp(review["expiresAt"], "review.expiresAt")
    if (
        reviewed_at > now + MAX_CLOCK_SKEW
        or expires_at <= reviewed_at
        or expires_at - reviewed_at > MAX_REVIEW_LIFETIME
        or (expires_at <= now and (not allow_expired or now - expires_at > MAX_CLEANUP_AFTER_EXPIRY))
    ):
        raise LaunchOperationError("configuration review is stale or outside its approval window")
    return Review(
        review_id=_safe_id(review["reviewId"], "review.reviewId"),
        reviewer=_safe_id(review["reviewer"], "review.reviewer"),
        reviewed_at=reviewed_at,
        expires_at=expires_at,
    )


def _parse_resources(
    value: Any,
    *,
    account_id: str,
    region: str,
) -> Resources:
    raw = _object(value, "configuration.resources")
    _exact_fields(
        raw,
        {
            "runtimeArn",
            "runtimeEndpointArn",
            "runtimeEndpointName",
            "agentcoreStackArn",
            "controlPlaneStackArn",
            "stateTableArn",
            "restoredStateTableArn",
            "outboxQueueArn",
            "outboxQueueUrl",
            "deadLetterQueueArn",
            "deadLetterQueueUrl",
            "deadLetterAlarmArn",
            "securityEventLogGroupArn",
        },
        "configuration.resources",
    )
    runtime_arn, runtime_match = _resource_arn(
        raw["runtimeArn"],
        RUNTIME_ARN,
        "resources.runtimeArn",
        account_id=account_id,
        region=region,
    )
    partition = runtime_match.group("partition")
    endpoint_arn, endpoint_match = _resource_arn(
        raw["runtimeEndpointArn"],
        ENDPOINT_ARN,
        "resources.runtimeEndpointArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    endpoint_name = _safe_id(
        raw["runtimeEndpointName"],
        "resources.runtimeEndpointName",
    )
    if endpoint_match.group("runtime") != runtime_match.group("runtime") or endpoint_name != "production":
        raise LaunchOperationError("runtime endpoint is not the exact production endpoint")
    agentcore_stack_arn, _ = _resource_arn(
        raw["agentcoreStackArn"],
        STACK_ARN,
        "resources.agentcoreStackArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    control_plane_stack_arn, _ = _resource_arn(
        raw["controlPlaneStackArn"],
        STACK_ARN,
        "resources.controlPlaneStackArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    state_table_arn, state_match = _resource_arn(
        raw["stateTableArn"],
        TABLE_ARN,
        "resources.stateTableArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    restored_table_arn, restored_match = _resource_arn(
        raw["restoredStateTableArn"],
        TABLE_ARN,
        "resources.restoredStateTableArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    if not restored_match.group("name").startswith(f"{state_match.group('name')}-restore-validation-"):
        raise LaunchOperationError("restored state table is outside the reviewed restore namespace")
    outbox_arn, outbox_match = _resource_arn(
        raw["outboxQueueArn"],
        QUEUE_ARN,
        "resources.outboxQueueArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    dlq_arn, dlq_match = _resource_arn(
        raw["deadLetterQueueArn"],
        QUEUE_ARN,
        "resources.deadLetterQueueArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    if outbox_arn == dlq_arn:
        raise LaunchOperationError("outbox and dead-letter queues must differ")
    outbox_url = _queue_url(
        raw["outboxQueueUrl"],
        "resources.outboxQueueUrl",
        account_id=account_id,
        region=region,
        queue_name=outbox_match.group("name"),
    )
    dlq_url = _queue_url(
        raw["deadLetterQueueUrl"],
        "resources.deadLetterQueueUrl",
        account_id=account_id,
        region=region,
        queue_name=dlq_match.group("name"),
    )
    alarm_arn, _ = _resource_arn(
        raw["deadLetterAlarmArn"],
        ALARM_ARN,
        "resources.deadLetterAlarmArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    log_group_arn, _ = _resource_arn(
        raw["securityEventLogGroupArn"],
        re.compile(
            r"^arn:(?P<partition>aws(?:-[a-z]+)?):logs:"
            r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
            r"log-group:(?P<name>[.\-_/#A-Za-z0-9]{1,512})$"
        ),
        "resources.securityEventLogGroupArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    return Resources(
        runtime_arn=runtime_arn,
        runtime_endpoint_arn=endpoint_arn,
        runtime_endpoint_name=endpoint_name,
        agentcore_stack_arn=agentcore_stack_arn,
        control_plane_stack_arn=control_plane_stack_arn,
        state_table_arn=state_table_arn,
        restored_state_table_arn=restored_table_arn,
        outbox_queue_arn=outbox_arn,
        outbox_queue_url=outbox_url,
        dead_letter_queue_arn=dlq_arn,
        dead_letter_queue_url=dlq_url,
        dead_letter_alarm_arn=alarm_arn,
        security_event_log_group_arn=log_group_arn,
    )


def _parse_coordinator(
    value: Any,
    *,
    account_id: str,
    region: str,
    partition: str,
    resources: Resources,
) -> Coordinator:
    raw = _object(value, "configuration.coordinator")
    _exact_fields(
        raw,
        {
            "stateMachineVersionArn",
            "executionRoleArn",
            "launchRoleArn",
            "leaseTableArn",
            "watchdogAlarmArn",
            "kmsKeyArn",
            "cleanupDeadlineSeconds",
        },
        "configuration.coordinator",
    )

    state_machine = _safe_string(
        raw["stateMachineVersionArn"],
        "coordinator.stateMachineVersionArn",
        maximum=512,
    )
    state_match = STATE_MACHINE_VERSION_ARN.fullmatch(state_machine)
    if (
        state_match is None
        or state_match.group("partition") != partition
        or state_match.group("region") != region
        or state_match.group("account") != account_id
    ):
        raise LaunchOperationError(
            "coordinator state machine must be an exact version ARN in the release account and region"
        )

    roles: list[str] = []
    for field in ("executionRoleArn", "launchRoleArn"):
        role = _safe_string(
            raw[field],
            f"coordinator.{field}",
            maximum=1024,
        )
        match = IAM_ROLE_ARN.fullmatch(role)
        if (
            match is None
            or match.group("partition") != partition
            or match.group("account") != account_id
            or role.endswith("/")
        ):
            raise LaunchOperationError(f"coordinator.{field} must be an exact release-account role ARN")
        roles.append(role)
    if roles[0] == roles[1]:
        raise LaunchOperationError("coordinator execution and launch roles must be distinct")

    lease_table, _ = _resource_arn(
        raw["leaseTableArn"],
        TABLE_ARN,
        "coordinator.leaseTableArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    if lease_table in {
        resources.state_table_arn,
        resources.restored_state_table_arn,
    }:
        raise LaunchOperationError("coordinator lease table must be separate from runtime state")
    watchdog_alarm, _ = _resource_arn(
        raw["watchdogAlarmArn"],
        ALARM_ARN,
        "coordinator.watchdogAlarmArn",
        account_id=account_id,
        region=region,
        partition=partition,
    )
    if watchdog_alarm == resources.dead_letter_alarm_arn:
        raise LaunchOperationError("coordinator watchdog and event DLQ alarms must be distinct")
    kms_key = _safe_string(
        raw["kmsKeyArn"],
        "coordinator.kmsKeyArn",
        maximum=1024,
    )
    kms_match = KMS_KEY_ARN.fullmatch(kms_key)
    if (
        kms_match is None
        or kms_match.group("partition") != partition
        or kms_match.group("region") != region
        or kms_match.group("account") != account_id
    ):
        raise LaunchOperationError("coordinator KMS key must be an exact key ARN in the release account and region")
    return Coordinator(
        state_machine_version_arn=state_machine,
        execution_role_arn=roles[0],
        launch_role_arn=roles[1],
        lease_table_arn=lease_table,
        watchdog_alarm_arn=watchdog_alarm,
        kms_key_arn=kms_key,
        cleanup_deadline_seconds=_integer(
            raw["cleanupDeadlineSeconds"],
            "coordinator.cleanupDeadlineSeconds",
            minimum=60,
            maximum=1800,
        ),
    )


def _parse_scenario(value: Any) -> Scenario:
    raw = _object(value, "configuration.scenario")
    _exact_fields(
        raw,
        {
            "tenantId",
            "projectId",
            "model",
            "primaryProvider",
            "fallbackProvider",
            "controlPlaneFault",
            "startupDeadlineSeconds",
            "faultTtlSeconds",
        },
        "configuration.scenario",
    )
    primary = _safe_string(
        raw["primaryProvider"],
        "scenario.primaryProvider",
        maximum=64,
    )
    fallback = _safe_string(
        raw["fallbackProvider"],
        "scenario.fallbackProvider",
        maximum=64,
    )
    if PROVIDER.fullmatch(primary) is None or PROVIDER.fullmatch(fallback) is None or primary == fallback:
        raise LaunchOperationError("scenario providers must be distinct provider identifiers")
    model = _safe_string(raw["model"], "scenario.model", maximum=256)
    if MODEL.fullmatch(model) is None:
        raise LaunchOperationError("scenario.model is invalid")
    dependency = raw["controlPlaneFault"]
    if dependency not in CONTROL_PLANE_DEPENDENCIES:
        raise LaunchOperationError("scenario.controlPlaneFault is not an approved dependency")
    return Scenario(
        tenant_id=_safe_id(raw["tenantId"], "scenario.tenantId"),
        project_id=_safe_id(raw["projectId"], "scenario.projectId"),
        model=model,
        primary_provider=primary,
        fallback_provider=fallback,
        control_plane_fault=dependency,
        startup_deadline_seconds=_integer(
            raw["startupDeadlineSeconds"],
            "scenario.startupDeadlineSeconds",
            minimum=1,
            maximum=300,
        ),
        fault_ttl_seconds=_integer(
            raw["faultTtlSeconds"],
            "scenario.faultTtlSeconds",
            minimum=30,
            maximum=1800,
        ),
    )


def parse_config(
    value: Any,
    *,
    region: str,
    now: datetime,
    sha256: str,
    allow_expired: bool = False,
) -> OperationConfig:
    """Parse one reviewed, secret-free rehearsal configuration."""
    raw = _object(value, "configuration")
    _reject_secret_fields(raw)
    _exact_fields(
        raw,
        {
            "schema",
            "accountId",
            "review",
            "limits",
            "coordinator",
            "resources",
            "scenario",
        },
        "configuration",
    )
    if raw["schema"] != CONFIG_SCHEMA:
        raise LaunchOperationError("configuration schema is unsupported")
    account_id = _safe_string(
        raw["accountId"],
        "configuration.accountId",
        maximum=12,
    )
    if ACCOUNT.fullmatch(account_id) is None:
        raise LaunchOperationError("configuration.accountId is invalid")
    review = _parse_review(
        raw["review"],
        now=now,
        allow_expired=allow_expired,
    )
    limits_raw = _object(raw["limits"], "configuration.limits")
    _exact_fields(
        limits_raw,
        {
            "requestTimeoutSeconds",
            "operationTimeoutSeconds",
            "pollIntervalSeconds",
            "maxAttempts",
            "maxResponseBytes",
        },
        "configuration.limits",
    )
    limits = Limits(
        request_timeout_seconds=_number(
            limits_raw["requestTimeoutSeconds"],
            "limits.requestTimeoutSeconds",
            minimum=0.5,
            maximum=60,
        ),
        operation_timeout_seconds=_number(
            limits_raw["operationTimeoutSeconds"],
            "limits.operationTimeoutSeconds",
            minimum=10,
            maximum=1800,
        ),
        poll_interval_seconds=_number(
            limits_raw["pollIntervalSeconds"],
            "limits.pollIntervalSeconds",
            minimum=0.05,
            maximum=30,
        ),
        max_attempts=_integer(
            limits_raw["maxAttempts"],
            "limits.maxAttempts",
            minimum=1,
            maximum=5,
        ),
        max_response_bytes=_integer(
            limits_raw["maxResponseBytes"],
            "limits.maxResponseBytes",
            minimum=1024,
            maximum=MAX_COORDINATOR_OUTPUT_BYTES,
        ),
    )
    if limits.request_timeout_seconds > limits.operation_timeout_seconds:
        raise LaunchOperationError("request timeout exceeds the operation timeout")
    resources = _parse_resources(
        raw["resources"],
        account_id=account_id,
        region=region,
    )
    runtime_match = RUNTIME_ARN.fullmatch(resources.runtime_arn)
    assert runtime_match is not None
    coordinator = _parse_coordinator(
        raw["coordinator"],
        account_id=account_id,
        region=region,
        partition=runtime_match.group("partition"),
        resources=resources,
    )
    scenario = _parse_scenario(raw["scenario"])
    if not allow_expired and now + timedelta(seconds=scenario.fault_ttl_seconds) > review.expires_at:
        raise LaunchOperationError("fault TTL extends beyond the reviewed configuration window")
    return OperationConfig(
        account_id=account_id,
        review=review,
        limits=limits,
        coordinator=coordinator,
        resources=resources,
        scenario=scenario,
        sha256=sha256,
    )


def load_config(
    path: Path,
    *,
    region: str,
    now: datetime,
    allow_expired: bool = False,
) -> OperationConfig:
    raw = _read_regular(path, maximum=MAX_CONFIG_BYTES)
    return parse_config(
        _strict_json(raw, str(path)),
        region=region,
        now=now,
        sha256=hashlib.sha256(raw).hexdigest(),
        allow_expired=allow_expired,
    )


def _reviewed_config_reference(
    *,
    uri: str,
    version_id: str,
    sha256: str,
    expected_sha256: str,
) -> dict[str, str]:
    parsed = urlsplit(uri)
    key = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "s3"
        or BUCKET.fullmatch(parsed.netloc or "") is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or S3_KEY.fullmatch(key) is None
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or uri != f"s3://{parsed.netloc}/{key}"
        or VERSION_ID.fullmatch(version_id) is None
        or version_id == "null"
        or SHA256.fullmatch(sha256) is None
        or sha256 != expected_sha256
    ):
        raise LaunchOperationError("reviewed configuration reference is not an exact immutable S3 version")
    return {
        "reviewedConfigS3Uri": uri,
        "reviewedConfigVersionId": version_id,
        "reviewedConfigSha256": sha256,
    }


def build_release_binding(
    *,
    release_commit: str,
    agentcore_image: str,
    control_plane_image: str,
    region: str,
    repository: str,
    workflow_ref: str,
    workflow_commit: str,
    run_id: str,
    run_attempt: str,
    account_id: str,
    config_sha256: str,
    reviewed_config_uri: str,
    reviewed_config_version_id: str,
    reviewed_config_sha256: str,
) -> ReleaseBinding:
    if SHA.fullmatch(release_commit) is None:
        raise LaunchOperationError("release commit must be a full lowercase SHA")
    if SHA.fullmatch(workflow_commit) is None:
        raise LaunchOperationError("workflow commit must be a full lowercase SHA")
    if REGION.fullmatch(region) is None:
        raise LaunchOperationError("region is invalid")
    if REPOSITORY.fullmatch(repository) is None:
        raise LaunchOperationError("repository is invalid")
    if RUN_NUMBER.fullmatch(run_id) is None or RUN_NUMBER.fullmatch(run_attempt) is None:
        raise LaunchOperationError("workflow run identity is invalid")
    expected_ref = f"{repository}/{launch_evidence.GATE_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise LaunchOperationError("workflow ref is not the protected launch-gates workflow")
    image_matches: list[re.Match[str]] = []
    for image, location in (
        (agentcore_image, "AgentCore image"),
        (control_plane_image, "control-plane image"),
    ):
        match = ECR_IMAGE.fullmatch(image)
        if match is None or match.group("account") != account_id or match.group("region") != region:
            raise LaunchOperationError(f"{location} must be an exact release-account ECR digest URI")
        image_matches.append(match)
    if agentcore_image == control_plane_image:
        raise LaunchOperationError("AgentCore and Fargate images must be distinct digest URIs")
    config_reference = _reviewed_config_reference(
        uri=reviewed_config_uri,
        version_id=reviewed_config_version_id,
        sha256=reviewed_config_sha256,
        expected_sha256=config_sha256,
    )
    release = {
        "commit": release_commit,
        "region": region,
        "agentcoreImage": agentcore_image,
        "controlPlaneImage": control_plane_image,
    }
    execution = {
        "repository": repository,
        "workflowRef": workflow_ref,
        "workflowCommit": workflow_commit,
        "checkedOutCommit": release_commit,
        "runId": run_id,
        "runAttempt": run_attempt,
        **config_reference,
    }
    owner_material = {
        "release": release,
        "execution": execution,
        "configSha256": config_sha256,
    }
    owner_id = hashlib.sha256(_canonical_bytes(owner_material)).hexdigest()
    return ReleaseBinding(
        release=release,
        execution=execution,
        account_id=account_id,
        owner_id=owner_id,
    )


def _assert_no_symlink_components(
    path: Path,
    *,
    location: str,
) -> None:
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LaunchOperationError(f"cannot inspect {location} path component: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LaunchOperationError(f"{location} path contains a symlink: {current}")


class StateDirectory:
    """Owner-only state directory with atomic files and a process lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        self._directory_fd: int | None = None
        self._lock_fd: int | None = None

    def __enter__(self) -> StateDirectory:
        _assert_no_symlink_components(self.path, location="state")
        try:
            self.path.mkdir(mode=0o700, parents=False, exist_ok=True)
            metadata = self.path.lstat()
        except OSError as exc:
            raise LaunchOperationError("cannot create or inspect the state directory") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise LaunchOperationError("state directory must be an owner-only non-symlink directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self._directory_fd = os.open(self.path, flags)
            lock_flags = os.O_RDWR | os.O_CREAT
            lock_flags |= getattr(os, "O_CLOEXEC", 0)
            lock_flags |= getattr(os, "O_NOFOLLOW", 0)
            self._lock_fd = os.open(
                LOCK_FILE,
                lock_flags,
                0o600,
                dir_fd=self._directory_fd,
            )
            lock_metadata = os.fstat(self._lock_fd)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != os.getuid()
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            ):
                raise LaunchOperationError("state lock must be an owner-only regular file")
            try:
                fcntl.flock(
                    self._lock_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise LaunchOperationError("state directory is owned by a concurrent rehearsal") from exc
        except LaunchOperationError:
            self.__exit__(None, None, None)
            raise
        except OSError as exc:
            self.__exit__(None, None, None)
            raise LaunchOperationError("cannot lock the state directory") from exc
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    @property
    def directory_fd(self) -> int:
        if self._directory_fd is None:
            raise LaunchOperationError("state directory is not open")
        return self._directory_fd

    def read(self) -> dict[str, Any] | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                STATE_FILE,
                flags,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LaunchOperationError("cannot open rehearsal state") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_STATE_BYTES
            ):
                raise LaunchOperationError("rehearsal state must be an owner-only bounded regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES or len(raw) != metadata.st_size:
                raise LaunchOperationError("rehearsal state is invalid")
            return _object(
                _strict_json(raw, "rehearsal state"),
                "rehearsal state",
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def write(self, state_value: Mapping[str, Any]) -> None:
        raw = _canonical_bytes(state_value)
        if len(raw) > MAX_STATE_BYTES:
            raise LaunchOperationError("rehearsal state exceeds its size limit")
        temporary = f".{STATE_FILE}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                flags,
                0o600,
                dir_fd=self.directory_fd,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                STATE_FILE,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            os.fsync(self.directory_fd)
        except OSError as exc:
            raise LaunchOperationError("cannot atomically persist rehearsal state") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _empty_ownership(owner_id: str, expires_at: str) -> dict[str, Any]:
    return {
        "ownerId": owner_id,
        "expiresAt": expires_at,
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {
            "model": None,
            "tenantConfig": None,
        },
    }


def _new_state(
    config: OperationConfig,
    binding: ReleaseBinding,
    *,
    now: datetime,
) -> dict[str, Any]:
    expires = _time_text(config.review.expires_at)
    return {
        "schema": STATE_SCHEMA,
        "owner": {
            "id": binding.owner_id,
            "reviewId": config.review.review_id,
            "reviewer": config.review.reviewer,
        },
        "release": dict(binding.release),
        "execution": dict(binding.execution),
        "configSha256": config.sha256,
        "reviewExpiresAt": expires,
        "createdAt": _time_text(now),
        "updatedAt": _time_text(now),
        "activeGate": None,
        "gates": {},
        "ownership": _empty_ownership(binding.owner_id, expires),
        "cleanup": None,
    }


def _validate_owned_id(value: Any, owner_id: str, location: str) -> str:
    identifier = _safe_id(value, location)
    if not identifier.startswith(f"{owner_id}:"):
        raise LaunchOperationError(f"{location} is not owned by this rehearsal")
    return identifier


def _validate_snapshot(
    value: Any,
    *,
    owner_id: str,
    kind: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    snapshot = _object(value, f"{kind} snapshot")
    _exact_fields(
        snapshot,
        {"ref", "sha256", "revision"},
        f"{kind} snapshot",
    )
    digest = _safe_string(
        snapshot["sha256"],
        f"{kind} snapshot sha256",
        maximum=64,
    )
    if SHA256.fullmatch(digest) is None:
        raise LaunchOperationError(f"{kind} snapshot digest is invalid")
    return {
        "ref": _validate_owned_id(
            snapshot["ref"],
            owner_id,
            f"{kind} snapshot ref",
        ),
        "sha256": digest,
        "revision": _integer(
            snapshot["revision"],
            f"{kind} snapshot revision",
            minimum=0,
            maximum=2**63 - 1,
        ),
    }


def _validate_ownership(
    value: Any,
    *,
    owner_id: str,
    expected_expiry: str,
) -> dict[str, Any]:
    ownership = _object(value, "action ownership")
    _exact_fields(
        ownership,
        {
            "ownerId",
            "expiresAt",
            "faultIds",
            "fixtureIds",
            "dlqCorrelationIds",
            "snapshots",
        },
        "action ownership",
    )
    if ownership["ownerId"] != owner_id or ownership["expiresAt"] != expected_expiry:
        raise LaunchOperationError("action ownership is foreign or has an unexpected lease")

    def owned_list(name: str) -> list[str]:
        raw = ownership[name]
        if type(raw) is not list or len(raw) > 256 or len(set(raw)) != len(raw) or raw != sorted(raw):
            raise LaunchOperationError(f"action ownership {name} must be sorted and unique")
        return [_validate_owned_id(item, owner_id, f"ownership {name}") for item in raw]

    snapshots = _object(ownership["snapshots"], "action snapshots")
    _exact_fields(
        snapshots,
        {"model", "tenantConfig"},
        "action snapshots",
    )
    return {
        "ownerId": owner_id,
        "expiresAt": expected_expiry,
        "faultIds": owned_list("faultIds"),
        "fixtureIds": owned_list("fixtureIds"),
        "dlqCorrelationIds": owned_list("dlqCorrelationIds"),
        "snapshots": {
            "model": _validate_snapshot(
                snapshots["model"],
                owner_id=owner_id,
                kind="model",
            ),
            "tenantConfig": _validate_snapshot(
                snapshots["tenantConfig"],
                owner_id=owner_id,
                kind="tenant config",
            ),
        },
    }


def _validate_state(
    state_value: dict[str, Any],
    *,
    config: OperationConfig,
    binding: ReleaseBinding,
    now: datetime,
    allow_expired: bool = False,
) -> dict[str, Any]:
    _exact_fields(
        state_value,
        {
            "schema",
            "owner",
            "release",
            "execution",
            "configSha256",
            "reviewExpiresAt",
            "createdAt",
            "updatedAt",
            "activeGate",
            "gates",
            "ownership",
            "cleanup",
        },
        "rehearsal state",
    )
    owner = _object(state_value["owner"], "rehearsal state owner")
    _exact_fields(
        owner,
        {"id", "reviewId", "reviewer"},
        "rehearsal state owner",
    )
    expected_expiry = _time_text(config.review.expires_at)
    if (
        state_value["schema"] != STATE_SCHEMA
        or owner
        != {
            "id": binding.owner_id,
            "reviewId": config.review.review_id,
            "reviewer": config.review.reviewer,
        }
        or state_value["release"] != binding.release
        or state_value["execution"] != binding.execution
        or state_value["configSha256"] != config.sha256
        or state_value["reviewExpiresAt"] != expected_expiry
    ):
        raise LaunchOperationError("rehearsal state does not match this reviewed release execution")
    created = _timestamp(state_value["createdAt"], "state.createdAt")
    updated = _timestamp(state_value["updatedAt"], "state.updatedAt")
    expires = _timestamp(
        state_value["reviewExpiresAt"],
        "state.reviewExpiresAt",
    )
    if (
        (expires <= now and (not allow_expired or now - expires > MAX_CLEANUP_AFTER_EXPIRY))
        or created > now + MAX_CLOCK_SKEW
        or updated > now + MAX_CLOCK_SKEW
        or updated < created
        or now - created > MAX_REVIEW_LIFETIME + MAX_CLOCK_SKEW
    ):
        raise LaunchOperationError("rehearsal state is stale")
    active_gate = state_value["activeGate"]
    if active_gate is not None and active_gate not in ALL_GATES:
        raise LaunchOperationError("rehearsal state active gate is invalid")
    gates = _object(state_value["gates"], "rehearsal state gates")
    if any(gate not in ALL_GATES for gate in gates):
        raise LaunchOperationError("rehearsal state contains an unknown gate")
    incomplete_gates: list[str] = []
    for gate, raw_gate_state in gates.items():
        gate_state = _object(raw_gate_state, f"state gate {gate}")
        _exact_fields(
            gate_state,
            {"nextIndex", "evidence", "actions"},
            f"state gate {gate}",
        )
        next_index = _integer(
            gate_state["nextIndex"],
            f"state gate {gate} nextIndex",
            minimum=0,
            maximum=len(EXPECTED_COMMANDS[gate]),
        )
        evidence = _object(
            gate_state["evidence"],
            f"state gate {gate} evidence",
        )
        actions = _object(
            gate_state["actions"],
            f"state gate {gate} actions",
        )
        expected_names = EXPECTED_COMMANDS[gate]
        if any(name not in expected_names for name in actions):
            raise LaunchOperationError(f"state gate {gate} contains an unknown action")
        complete_count = 0
        in_progress_count = 0
        for index, name in enumerate(expected_names):
            record = actions.get(name)
            if record is None:
                continue
            record = _object(record, f"state action {name}")
            status_value = record.get("status")
            expected_record_fields = (
                {
                    "status",
                    "correlationId",
                    "idempotencyKey",
                    "startedAt",
                }
                if status_value == "in_progress"
                else {
                    "status",
                    "correlationId",
                    "idempotencyKey",
                    "startedAt",
                    "completedAt",
                    "resultSha256",
                }
            )
            _exact_fields(record, expected_record_fields, f"state action {name}")
            if status_value not in {"in_progress", "complete"}:
                raise LaunchOperationError(f"state action {name} status is invalid")
            correlation_id = _safe_id(
                record["correlationId"],
                f"state action {name} correlation",
            )
            digest = _safe_string(
                record["idempotencyKey"],
                f"state action {name} idempotency key",
                maximum=64,
            )
            if (
                SHA256.fullmatch(digest) is None
                or correlation_id != _correlation_id(binding, gate, name)
                or digest != _idempotency_key(binding, gate, name)
            ):
                raise LaunchOperationError(f"state action {name} identity is invalid")
            started_at = _timestamp(
                record["startedAt"],
                f"state action {name} startedAt",
            )
            if started_at > now + MAX_CLOCK_SKEW:
                raise LaunchOperationError(f"state action {name} starts in the future")
            if status_value == "complete":
                complete_count += 1
                completed_at = _timestamp(
                    record["completedAt"],
                    f"state action {name} completedAt",
                )
                if completed_at < started_at or completed_at > now + MAX_CLOCK_SKEW:
                    raise LaunchOperationError(f"state action {name} completion time is invalid")
                result_digest = _safe_string(
                    record["resultSha256"],
                    f"state action {name} result digest",
                    maximum=64,
                )
                if SHA256.fullmatch(result_digest) is None:
                    raise LaunchOperationError(f"state action {name} result digest is invalid")
                if index >= next_index:
                    raise LaunchOperationError(f"state action {name} completion is out of order")
            else:
                in_progress_count += 1
                if index != next_index:
                    raise LaunchOperationError(f"state action {name} progress is out of order")
        if complete_count != next_index or in_progress_count > 1:
            raise LaunchOperationError(f"state gate {gate} command journal is inconsistent")
        allowed_evidence = set().union(*(ACTION_EVIDENCE_FIELDS[name] for name in expected_names[:next_index]))
        if set(evidence) != allowed_evidence:
            raise LaunchOperationError(f"state gate {gate} evidence is inconsistent")
        if next_index == len(expected_names):
            try:
                normalized = launch_evidence._validate_observations(
                    gate,
                    evidence,
                    release=binding.release,
                )
            except launch_evidence.LaunchRehearsalError as exc:
                raise LaunchOperationError(f"state gate {gate} observations are invalid") from exc
            if normalized != evidence:
                raise LaunchOperationError(f"state gate {gate} observations are not normalized")
        else:
            incomplete_gates.append(gate)
    cleanup = state_value["cleanup"]
    if (
        len(incomplete_gates) > 1
        or (
            cleanup is None
            and (
                (incomplete_gates and active_gate != incomplete_gates[0])
                or (not incomplete_gates and active_gate is not None)
            )
        )
        or (cleanup is not None and active_gate is not None and active_gate not in incomplete_gates)
    ):
        raise LaunchOperationError("rehearsal state gate ownership is inconsistent")
    normalized_ownership = _validate_ownership(
        state_value["ownership"],
        owner_id=binding.owner_id,
        expected_expiry=expected_expiry,
    )
    if cleanup is not None:
        cleanup_value = _object(cleanup, "state cleanup")
        cleanup_status = cleanup_value.get("status")
        expected_cleanup_fields = (
            {
                "status",
                "correlationId",
                "idempotencyKey",
                "startedAt",
                "priorOwnership",
            }
            if cleanup_status == "in_progress"
            else {
                "status",
                "correlationId",
                "idempotencyKey",
                "startedAt",
                "priorOwnership",
                "completedAt",
                "resultSha256",
                "evidenceSha256",
                "evidence",
            }
        )
        _exact_fields(
            cleanup_value,
            expected_cleanup_fields,
            "state cleanup",
        )
        if cleanup_status not in {"in_progress", "complete"}:
            raise LaunchOperationError("state cleanup status is invalid")
        correlation_id = _safe_id(
            cleanup_value["correlationId"],
            "state cleanup correlation ID",
        )
        idempotency_key = _safe_string(
            cleanup_value["idempotencyKey"],
            "state cleanup idempotency key",
            maximum=64,
        )
        if correlation_id != _correlation_id(binding, "cleanup", "cleanup") or idempotency_key != _idempotency_key(
            binding, "cleanup", "cleanup"
        ):
            raise LaunchOperationError("state cleanup identity does not match this execution")
        _timestamp(cleanup_value["startedAt"], "state cleanup startedAt")
        prior_ownership = _validate_ownership(
            cleanup_value["priorOwnership"],
            owner_id=binding.owner_id,
            expected_expiry=expected_expiry,
        )
        if cleanup_status == "in_progress":
            if prior_ownership != normalized_ownership:
                raise LaunchOperationError("in-progress cleanup ownership changed locally")
        else:
            _timestamp(
                cleanup_value["completedAt"],
                "state cleanup completedAt",
            )
            for name in ("resultSha256", "evidenceSha256"):
                digest = _safe_string(
                    cleanup_value[name],
                    f"state cleanup {name}",
                    maximum=64,
                )
                if SHA256.fullmatch(digest) is None:
                    raise LaunchOperationError(f"state cleanup {name} is invalid")
            cleanup_evidence = _cleanup_evidence(
                cleanup_value["evidence"],
                prior_ownership=prior_ownership,
            )
            if (
                hashlib.sha256(_canonical_bytes(cleanup_evidence)).hexdigest() != cleanup_value["evidenceSha256"]
                or normalized_ownership != _empty_ownership(binding.owner_id, expected_expiry)
                or active_gate is not None
            ):
                raise LaunchOperationError("completed cleanup state is inconsistent")
    return state_value


def _aws_call(
    aws: AwsTransport,
    config: OperationConfig,
    binding: ReleaseBinding,
    service: str,
    operation: str,
    parameters: Mapping[str, Any],
) -> Mapping[str, Any]:
    deadline = time.monotonic() + config.limits.operation_timeout_seconds
    last_error: Exception | None = None
    for attempt in range(config.limits.max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = aws.call(
                service,
                operation,
                region=binding.release["region"],
                parameters=parameters,
                timeout_seconds=min(
                    config.limits.request_timeout_seconds,
                    remaining,
                ),
            )
            if not isinstance(response, Mapping):
                raise LaunchOperationError(f"AWS {service} returned an invalid response")
            return response
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= config.limits.max_attempts:
                break
            time.sleep(
                min(
                    config.limits.poll_interval_seconds,
                    max(0.0, deadline - time.monotonic()),
                )
            )
    raise LaunchOperationError(f"AWS {service} operation did not complete within retry bounds") from last_error


class StepFunctionsCoordinator:
    """Invoke one immutable Standard-workflow execution idempotently."""

    def __init__(
        self,
        aws: AwsTransport,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._aws = aws
        self._sleep = sleep
        self._monotonic = monotonic

    def _call(
        self,
        operation: str,
        parameters: Mapping[str, Any],
        *,
        config: OperationConfig,
        binding: ReleaseBinding,
        deadline: float,
        allowed_errors: frozenset[str] = frozenset(),
    ) -> Mapping[str, Any] | None:
        last_error: BaseException | None = None
        for attempt in range(config.limits.max_attempts):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            try:
                response = self._aws.call(
                    "stepfunctions",
                    operation,
                    region=binding.release["region"],
                    parameters=parameters,
                    timeout_seconds=min(
                        config.limits.request_timeout_seconds,
                        remaining,
                    ),
                )
                if not isinstance(response, Mapping):
                    raise LaunchOperationError("Step Functions returned an invalid response")
                return response
            except AwsCallError as exc:
                if exc.code in allowed_errors:
                    return None
                last_error = exc
            except Exception as exc:
                last_error = exc
            if attempt + 1 < config.limits.max_attempts:
                self._sleep(
                    min(
                        config.limits.poll_interval_seconds,
                        max(0.0, deadline - self._monotonic()),
                    )
                )
        raise LaunchOperationError("launch coordinator AWS operation did not complete") from last_error

    @staticmethod
    def _execution_identity(
        payload: Mapping[str, Any],
        coordinator: Coordinator,
        *,
        retry_number: int = 0,
    ) -> tuple[str, str]:
        if type(retry_number) is not int or not 0 <= retry_number <= 99:
            raise LaunchOperationError("coordinator retry number is outside policy")
        owner = _object(payload.get("owner"), "coordinator owner")
        owner_id = _safe_string(
            owner.get("id"),
            "coordinator owner id",
            maximum=64,
        )
        correlation_id = _safe_string(
            payload.get("correlationId"),
            "coordinator correlation id",
            maximum=64,
        )
        if SHA256.fullmatch(owner_id) is None or re.fullmatch(r"[0-9a-f]{32}", correlation_id) is None:
            raise LaunchOperationError("coordinator execution identity is malformed")
        suffix = "" if retry_number == 0 else f"-r{retry_number:02d}"
        execution_name = f"axon-{owner_id[:16]}-{correlation_id[:32]}{suffix}"
        match = STATE_MACHINE_VERSION_ARN.fullmatch(coordinator.state_machine_version_arn)
        assert match is not None
        execution_arn = (
            f"arn:{match.group('partition')}:states:"
            f"{match.group('region')}:{match.group('account')}:execution:"
            f"{match.group('name')}:{execution_name}"
        )
        return execution_name, execution_arn

    @staticmethod
    def _validate_description(
        description: Mapping[str, Any],
        *,
        execution_name: str,
        execution_arn: str,
        input_text: str,
        coordinator: Coordinator,
    ) -> str:
        if (
            description.get("executionArn") != execution_arn
            or description.get("name") != execution_name
            or description.get("stateMachineArn") != coordinator.state_machine_base_arn
            or description.get("stateMachineVersionArn") != coordinator.state_machine_version_arn
            or description.get("input") != input_text
        ):
            raise CoordinatorNotDrainedError("launch coordinator execution binding is invalid")
        status = description.get("status")
        if status not in {
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "TIMED_OUT",
            "ABORTED",
        }:
            raise CoordinatorNotDrainedError("launch coordinator execution status is invalid")
        return status

    @staticmethod
    def _parse_output(
        description: Mapping[str, Any],
        *,
        config: OperationConfig,
    ) -> dict[str, Any]:
        output = description.get("output")
        if not isinstance(output, str):
            raise LaunchOperationError("launch coordinator output is missing")
        try:
            raw = output.encode("utf-8")
        except UnicodeError as exc:
            raise LaunchOperationError("launch coordinator output is not UTF-8") from exc
        if len(raw) > config.limits.max_response_bytes:
            raise LaunchOperationError("launch coordinator output exceeds its reviewed limit")
        return _object(
            _strict_json(raw, "launch coordinator output"),
            "launch coordinator output",
        )

    @staticmethod
    def _is_retry_output(
        output: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> bool:
        if output.get("schema") != ACTION_RETRY_SCHEMA:
            return False
        _exact_fields(
            output,
            {
                "schema",
                "status",
                "gate",
                "operation",
                "ownerId",
                "correlationId",
                "idempotencyKey",
                "code",
                "retryable",
            },
            "launch coordinator retry output",
        )
        owner = _object(payload.get("owner"), "coordinator owner")
        code = output.get("code")
        if (
            output.get("status") != "RETRY"
            or output.get("retryable") is not True
            or output.get("gate") != payload.get("gate")
            or output.get("operation") != payload.get("operation")
            or output.get("ownerId") != owner.get("id")
            or output.get("correlationId") != payload.get("correlationId")
            or output.get("idempotencyKey") != payload.get("idempotencyKey")
            or not isinstance(code, str)
            or RETRY_CODE.fullmatch(code) is None
        ):
            raise LaunchOperationError("launch coordinator retry output is not bound to this action")
        return True

    def _stop_and_drain(
        self,
        *,
        execution_name: str,
        execution_arn: str,
        input_text: str,
        config: OperationConfig,
        binding: ReleaseBinding,
    ) -> bool:
        drain_timeout = min(
            COORDINATOR_DRAIN_TIMEOUT_SECONDS,
            max(
                10.0,
                config.limits.request_timeout_seconds * config.limits.max_attempts
                + 2 * config.limits.poll_interval_seconds,
            ),
        )
        deadline = self._monotonic() + drain_timeout
        poll_budget = min(
            256,
            max(
                config.limits.max_attempts,
                math.ceil(drain_timeout / config.limits.poll_interval_seconds) + 1,
            ),
        )
        try:
            description = None
            for attempt in range(poll_budget):
                description = self._call(
                    "describe_execution",
                    {
                        "executionArn": execution_arn,
                        "includedData": "ALL_DATA",
                    },
                    config=config,
                    binding=binding,
                    deadline=deadline,
                    allowed_errors=frozenset({"ExecutionDoesNotExist"}),
                )
                if description is not None:
                    break
                if attempt + 1 >= poll_budget or self._monotonic() >= deadline:
                    return False
                self._sleep(
                    min(
                        config.limits.poll_interval_seconds,
                        max(0.0, deadline - self._monotonic()),
                    )
                )
            if description is None:
                return False
            status = self._validate_description(
                description,
                execution_name=execution_name,
                execution_arn=execution_arn,
                input_text=input_text,
                coordinator=config.coordinator,
            )
            if status != "RUNNING":
                return True
            self._call(
                "stop_execution",
                {
                    "executionArn": execution_arn,
                    "error": "LaunchCoordinatorDeadlineExceeded",
                    "cause": ("The reviewed launch operation deadline expired"),
                },
                config=config,
                binding=binding,
                deadline=deadline,
                allowed_errors=frozenset({"ExecutionDoesNotExist", "ExecutionNotRunning"}),
            )
            for _ in range(poll_budget):
                if self._monotonic() >= deadline:
                    return False
                description = self._call(
                    "describe_execution",
                    {
                        "executionArn": execution_arn,
                        "includedData": "ALL_DATA",
                    },
                    config=config,
                    binding=binding,
                    deadline=deadline,
                    allowed_errors=frozenset({"ExecutionDoesNotExist"}),
                )
                if description is None:
                    return False
                status = self._validate_description(
                    description,
                    execution_name=execution_name,
                    execution_arn=execution_arn,
                    input_text=input_text,
                    coordinator=config.coordinator,
                )
                if status != "RUNNING":
                    return True
                self._sleep(
                    min(
                        config.limits.poll_interval_seconds,
                        max(0.0, deadline - self._monotonic()),
                    )
                )
        except LaunchOperationError:
            return False
        return False

    def invoke(
        self,
        payload: Mapping[str, Any],
        *,
        config: OperationConfig,
        binding: ReleaseBinding,
    ) -> Mapping[str, Any]:
        input_text = _canonical_bytes(payload).decode("utf-8").removesuffix("\n")
        deadline = self._monotonic() + config.limits.operation_timeout_seconds
        for retry_number in range(config.limits.max_attempts):
            execution_name, execution_arn = self._execution_identity(
                payload,
                config.coordinator,
                retry_number=retry_number,
            )
            try:
                description = self._call(
                    "describe_execution",
                    {
                        "executionArn": execution_arn,
                        "includedData": "ALL_DATA",
                    },
                    config=config,
                    binding=binding,
                    deadline=deadline,
                    allowed_errors=frozenset({"ExecutionDoesNotExist"}),
                )
                if description is None:
                    started = self._call(
                        "start_execution",
                        {
                            "stateMachineArn": (config.coordinator.state_machine_version_arn),
                            "name": execution_name,
                            "input": input_text,
                        },
                        config=config,
                        binding=binding,
                        deadline=deadline,
                        allowed_errors=frozenset({"ExecutionAlreadyExists"}),
                    )
                    if started is not None and started.get("executionArn") != execution_arn:
                        raise CoordinatorNotDrainedError("launch coordinator started a foreign execution")
            except CoordinatorNotDrainedError:
                raise
            except LaunchOperationError as exc:
                if not self._stop_and_drain(
                    execution_name=execution_name,
                    execution_arn=execution_arn,
                    input_text=input_text,
                    config=config,
                    binding=binding,
                ):
                    raise CoordinatorNotDrainedError("launch coordinator execution could not be drained") from exc
                raise

            retry_requested = False
            while self._monotonic() < deadline:
                try:
                    description = self._call(
                        "describe_execution",
                        {
                            "executionArn": execution_arn,
                            "includedData": "ALL_DATA",
                        },
                        config=config,
                        binding=binding,
                        deadline=deadline,
                    )
                except LaunchOperationError as exc:
                    if not self._stop_and_drain(
                        execution_name=execution_name,
                        execution_arn=execution_arn,
                        input_text=input_text,
                        config=config,
                        binding=binding,
                    ):
                        raise CoordinatorNotDrainedError("launch coordinator execution could not be drained") from exc
                    raise
                assert description is not None
                status_value = self._validate_description(
                    description,
                    execution_name=execution_name,
                    execution_arn=execution_arn,
                    input_text=input_text,
                    coordinator=config.coordinator,
                )
                if status_value == "SUCCEEDED":
                    output = self._parse_output(
                        description,
                        config=config,
                    )
                    if self._is_retry_output(output, payload):
                        retry_requested = True
                        break
                    return output
                if status_value != "RUNNING":
                    raise LaunchOperationError("launch coordinator execution did not succeed")
                self._sleep(
                    min(
                        config.limits.poll_interval_seconds,
                        max(0.0, deadline - self._monotonic()),
                    )
                )
            if retry_requested:
                if retry_number + 1 >= config.limits.max_attempts:
                    raise LaunchOperationError("launch coordinator retry budget is exhausted")
                self._sleep(
                    min(
                        config.limits.poll_interval_seconds,
                        max(0.0, deadline - self._monotonic()),
                    )
                )
                continue
            if not self._stop_and_drain(
                execution_name=execution_name,
                execution_arn=execution_arn,
                input_text=input_text,
                config=config,
                binding=binding,
            ):
                raise CoordinatorNotDrainedError("launch coordinator execution could not be drained")
            raise LaunchOperationError("launch coordinator execution exceeded its reviewed deadline")
        raise LaunchOperationError("launch coordinator retry budget is exhausted")


def _stack(
    aws: AwsTransport,
    config: OperationConfig,
    binding: ReleaseBinding,
    stack_arn: str,
) -> tuple[Mapping[str, Any], dict[str, str]]:
    response = _aws_call(
        aws,
        config,
        binding,
        "cloudformation",
        "describe_stacks",
        {"StackName": stack_arn},
    )
    stacks = response.get("Stacks")
    if type(stacks) is not list or len(stacks) != 1:
        raise LaunchOperationError("CloudFormation stack binding is missing")
    stack = stacks[0]
    if not isinstance(stack, Mapping) or stack.get("StackId") != stack_arn:
        raise LaunchOperationError("CloudFormation returned a foreign stack")
    raw_outputs = stack.get("Outputs")
    if not isinstance(raw_outputs, list):
        raise LaunchOperationError("CloudFormation stack outputs are invalid")
    outputs: dict[str, str] = {}
    for output in raw_outputs:
        if not isinstance(output, Mapping):
            continue
        key = output.get("OutputKey")
        value = output.get("OutputValue")
        if isinstance(key, str) and isinstance(value, str):
            if key in outputs:
                raise LaunchOperationError("CloudFormation stack has duplicate outputs")
            outputs[key] = value
    return stack, outputs


def verify_launch_role_identity(
    aws: AwsTransport,
    config: OperationConfig,
    binding: ReleaseBinding,
) -> None:
    """Prove the caller is the exact protected launch-gates role session."""
    identity = _aws_call(
        aws,
        config,
        binding,
        "sts",
        "get_caller_identity",
        {},
    )
    launch_role_match = IAM_ROLE_ARN.fullmatch(config.coordinator.launch_role_arn)
    assert launch_role_match is not None
    session_name = f"AxonLLMLaunchGates-{binding.execution['runId']}-{binding.execution['runAttempt']}"
    expected_caller_arn = (
        f"arn:{launch_role_match.group('partition')}:sts::"
        f"{binding.account_id}:assumed-role/"
        f"{launch_role_match.group('name').rsplit('/', 1)[-1]}/"
        f"{session_name}"
    )
    if (
        identity.get("Account") != binding.account_id
        or identity.get("Arn") != expected_caller_arn
        or not isinstance(identity.get("UserId"), str)
        or not identity["UserId"].endswith(f":{session_name}")
    ):
        raise LaunchOperationError("AWS caller is not the reviewed launch-gates role session")


def verify_deployment_binding(
    aws: AwsTransport,
    config: OperationConfig,
    binding: ReleaseBinding,
    *,
    require_primary: bool,
    require_launch_identity: bool = True,
) -> None:
    """Bind deployed stacks, runtime, state, queues, alarms, and images."""
    if require_launch_identity:
        verify_launch_role_identity(aws, config, binding)

    _, agent_outputs = _stack(
        aws,
        config,
        binding,
        config.resources.agentcore_stack_arn,
    )
    expected_agent_outputs = {
        "RuntimeArn": config.resources.runtime_arn,
        "RuntimeEndpointName": config.resources.runtime_endpoint_name,
        "StateTableName": config.resources.state_table_name,
        "SecurityEventOutboxQueueArn": config.resources.outbox_queue_arn,
        "SecurityEventOutboxQueueUrl": config.resources.outbox_queue_url,
        "SecurityEventDeadLetterQueueUrl": (config.resources.dead_letter_queue_url),
        "SecurityEventLogGroupArn": (config.resources.security_event_log_group_arn),
        "RuntimeImageUri": binding.release["agentcoreImage"],
    }
    if any(agent_outputs.get(name) != expected for name, expected in expected_agent_outputs.items()):
        raise LaunchOperationError("AgentCore stack outputs do not match the reviewed resources")

    _, control_outputs = _stack(
        aws,
        config,
        binding,
        config.resources.control_plane_stack_arn,
    )
    expected_control_outputs = {
        "AgentCoreStackName": config.resources.agentcore_stack_name,
        "PrimaryStateTableName": config.resources.state_table_name,
        "ControlPlaneImageUri": binding.release["controlPlaneImage"],
    }
    if any(control_outputs.get(name) != expected for name, expected in expected_control_outputs.items()):
        raise LaunchOperationError("control-plane stack outputs do not match the reviewed resources")
    cluster_name = control_outputs.get("ClusterName")
    service_name = control_outputs.get("ServiceName")
    task_definition_arn = control_outputs.get("TaskDefinitionArn")
    if any(not isinstance(item, str) or not item for item in (cluster_name, service_name, task_definition_arn)):
        raise LaunchOperationError("control-plane stack is missing live service bindings")
    if require_primary and (
        agent_outputs.get("RecoveryCutoverMode") != "normal"
        or agent_outputs.get("SelectedRuntimeStateTableName") != config.resources.state_table_name
        or control_outputs.get("RecoveryCutoverMode") != "normal"
        or control_outputs.get("SelectedRuntimeStateTableName") != config.resources.state_table_name
    ):
        raise LaunchOperationError("AgentCore and control plane are not on healthy primary state")

    table_response = _aws_call(
        aws,
        config,
        binding,
        "dynamodb",
        "describe_table",
        {"TableName": config.resources.state_table_name},
    )
    table = table_response.get("Table")
    if (
        not isinstance(table, Mapping)
        or table.get("TableArn") != config.resources.state_table_arn
        or table.get("TableStatus") != "ACTIVE"
        or table.get("DeletionProtectionEnabled") is not True
    ):
        raise LaunchOperationError("primary state table is not the reviewed protected table")

    coordinator_table = _aws_call(
        aws,
        config,
        binding,
        "dynamodb",
        "describe_table",
        {"TableName": config.coordinator.lease_table_name},
    ).get("Table")
    coordinator_sse = coordinator_table.get("SSEDescription") if isinstance(coordinator_table, Mapping) else None
    if (
        not isinstance(coordinator_table, Mapping)
        or coordinator_table.get("TableArn") != config.coordinator.lease_table_arn
        or coordinator_table.get("TableStatus") != "ACTIVE"
        or coordinator_table.get("DeletionProtectionEnabled") is not True
        or not isinstance(coordinator_sse, Mapping)
        or coordinator_sse.get("Status") != "ENABLED"
        or coordinator_sse.get("SSEType") != "KMS"
        or coordinator_sse.get("KMSMasterKeyArn") != config.coordinator.kms_key_arn
    ):
        raise LaunchOperationError("launch coordinator lease table is not the reviewed protected KMS table")
    ttl = _aws_call(
        aws,
        config,
        binding,
        "dynamodb",
        "describe_time_to_live",
        {"TableName": config.coordinator.lease_table_name},
    ).get("TimeToLiveDescription")
    if (
        not isinstance(ttl, Mapping)
        or ttl.get("TimeToLiveStatus") != "ENABLED"
        or ttl.get("AttributeName") != "expiresAtEpoch"
    ):
        raise LaunchOperationError("launch coordinator lease expiry is not enabled")

    state_machine = _aws_call(
        aws,
        config,
        binding,
        "stepfunctions",
        "describe_state_machine",
        {
            "stateMachineArn": (config.coordinator.state_machine_version_arn),
            "includedData": "ALL_DATA",
        },
    )
    logging_configuration = state_machine.get("loggingConfiguration")
    tracing_configuration = state_machine.get("tracingConfiguration")
    encryption_configuration = state_machine.get("encryptionConfiguration")
    if (
        state_machine.get("stateMachineArn") != config.coordinator.state_machine_version_arn
        or state_machine.get("status") != "ACTIVE"
        or state_machine.get("type") != "STANDARD"
        or state_machine.get("roleArn") != config.coordinator.execution_role_arn
        or not isinstance(state_machine.get("revisionId"), str)
        or not state_machine["revisionId"]
        or not isinstance(logging_configuration, Mapping)
        or logging_configuration.get("level") != "ALL"
        or logging_configuration.get("includeExecutionData") is not False
        or not logging_configuration.get("destinations")
        or not isinstance(tracing_configuration, Mapping)
        or tracing_configuration.get("enabled") is not True
        or not isinstance(encryption_configuration, Mapping)
        or encryption_configuration.get("type") != "CUSTOMER_MANAGED_KMS_KEY"
        or encryption_configuration.get("kmsKeyId") != config.coordinator.kms_key_arn
    ):
        raise LaunchOperationError("launch coordinator state-machine version is not production hardened")
    raw_tags = _aws_call(
        aws,
        config,
        binding,
        "stepfunctions",
        "list_tags_for_resource",
        {"resourceArn": config.coordinator.state_machine_base_arn},
    ).get("tags")
    if not isinstance(raw_tags, list):
        raise LaunchOperationError("launch coordinator tags are unavailable")
    tags = {
        item.get("key"): item.get("value")
        for item in raw_tags
        if isinstance(item, Mapping) and isinstance(item.get("key"), str) and isinstance(item.get("value"), str)
    }
    if any(
        tags.get(name) != expected
        for name, expected in {
            "Application": "AxonLLM",
            "Environment": "production",
            "Purpose": "agentcore-launch-rehearsal",
        }.items()
    ):
        raise LaunchOperationError("launch coordinator is missing required ownership tags")

    for queue_url, queue_arn in (
        (
            config.resources.outbox_queue_url,
            config.resources.outbox_queue_arn,
        ),
        (
            config.resources.dead_letter_queue_url,
            config.resources.dead_letter_queue_arn,
        ),
    ):
        attributes = _aws_call(
            aws,
            config,
            binding,
            "sqs",
            "get_queue_attributes",
            {
                "QueueUrl": queue_url,
                "AttributeNames": ["QueueArn"],
            },
        ).get("Attributes")
        if not isinstance(attributes, Mapping) or attributes.get("QueueArn") != queue_arn:
            raise LaunchOperationError("SQS queue binding is foreign")

    alarm_match = ALARM_ARN.fullmatch(config.resources.dead_letter_alarm_arn)
    assert alarm_match is not None
    alarms = _aws_call(
        aws,
        config,
        binding,
        "cloudwatch",
        "describe_alarms",
        {"AlarmNames": [alarm_match.group("name")]},
    ).get("MetricAlarms")
    if (
        type(alarms) is not list
        or len(alarms) != 1
        or not isinstance(alarms[0], Mapping)
        or alarms[0].get("AlarmArn") != config.resources.dead_letter_alarm_arn
    ):
        raise LaunchOperationError("dead-letter alarm does not match the reviewed ARN")

    watchdog_match = ALARM_ARN.fullmatch(config.coordinator.watchdog_alarm_arn)
    assert watchdog_match is not None
    watchdogs = _aws_call(
        aws,
        config,
        binding,
        "cloudwatch",
        "describe_alarms",
        {"AlarmNames": [watchdog_match.group("name")]},
    ).get("MetricAlarms")
    if (
        type(watchdogs) is not list
        or len(watchdogs) != 1
        or not isinstance(watchdogs[0], Mapping)
        or watchdogs[0].get("AlarmArn") != config.coordinator.watchdog_alarm_arn
        or watchdogs[0].get("ActionsEnabled") is not True
        or not watchdogs[0].get("AlarmActions")
        or watchdogs[0].get("TreatMissingData") != "breaching"
        or watchdogs[0].get("StateValue") != "OK"
    ):
        raise LaunchOperationError("launch coordinator watchdog alarm is not healthy")

    runtime_id = config.resources.runtime_arn.rsplit("/", 1)[-1]
    endpoint = _aws_call(
        aws,
        config,
        binding,
        "bedrock-agentcore-control",
        "get_agent_runtime_endpoint",
        {
            "agentRuntimeId": runtime_id,
            "endpointName": config.resources.runtime_endpoint_name,
        },
    )
    if (
        endpoint.get("agentRuntimeArn") != config.resources.runtime_arn
        or endpoint.get("agentRuntimeEndpointArn") != config.resources.runtime_endpoint_arn
        or endpoint.get("name") != config.resources.runtime_endpoint_name
        or endpoint.get("status") != "READY"
        or not isinstance(endpoint.get("liveVersion"), str)
        or endpoint.get("liveVersion") != endpoint.get("targetVersion")
    ):
        raise LaunchOperationError("AgentCore endpoint is not the reviewed stable endpoint")
    live_version = endpoint["liveVersion"]
    runtime = _aws_call(
        aws,
        config,
        binding,
        "bedrock-agentcore-control",
        "get_agent_runtime",
        {
            "agentRuntimeId": runtime_id,
            "agentRuntimeVersion": live_version,
        },
    )
    artifact = runtime.get("agentRuntimeArtifact")
    container = artifact.get("containerConfiguration") if isinstance(artifact, Mapping) else None
    if (
        runtime.get("agentRuntimeArn") != config.resources.runtime_arn
        or runtime.get("agentRuntimeVersion") != live_version
        or runtime.get("status") != "READY"
        or not isinstance(container, Mapping)
        or container.get("containerUri") != binding.release["agentcoreImage"]
    ):
        raise LaunchOperationError("live AgentCore version does not run the reviewed image")

    service_response = _aws_call(
        aws,
        config,
        binding,
        "ecs",
        "describe_services",
        {
            "cluster": cluster_name,
            "services": [service_name],
        },
    )
    services = service_response.get("services")
    if (
        service_response.get("failures")
        or type(services) is not list
        or len(services) != 1
        or not isinstance(services[0], Mapping)
    ):
        raise LaunchOperationError("control-plane service binding is unavailable")
    service = services[0]
    desired_count = service.get("desiredCount")
    running_count = service.get("runningCount")
    if (
        service.get("taskDefinition") != task_definition_arn
        or isinstance(desired_count, bool)
        or not isinstance(desired_count, int)
        or desired_count < 1
        or running_count != desired_count
    ):
        raise LaunchOperationError("control-plane service is not stably running its reviewed task")
    task_definition = _aws_call(
        aws,
        config,
        binding,
        "ecs",
        "describe_task_definition",
        {"taskDefinition": task_definition_arn},
    ).get("taskDefinition")
    containers = task_definition.get("containerDefinitions") if isinstance(task_definition, Mapping) else None
    if (
        not isinstance(task_definition, Mapping)
        or task_definition.get("taskDefinitionArn") != task_definition_arn
        or type(containers) is not list
        or len(containers) != 1
        or not isinstance(containers[0], Mapping)
        or containers[0].get("image") != binding.release["controlPlaneImage"]
    ):
        raise LaunchOperationError("control-plane task definition does not use the reviewed image")
    listed_tasks = _aws_call(
        aws,
        config,
        binding,
        "ecs",
        "list_tasks",
        {
            "cluster": cluster_name,
            "serviceName": service_name,
            "desiredStatus": "RUNNING",
            "maxResults": 100,
        },
    )
    task_arns = listed_tasks.get("taskArns")
    if (
        listed_tasks.get("nextToken") is not None
        or type(task_arns) is not list
        or len(task_arns) != running_count
        or any(not isinstance(arn, str) or not arn for arn in task_arns)
    ):
        raise LaunchOperationError("control-plane running task inventory is incomplete")
    running_tasks = _aws_call(
        aws,
        config,
        binding,
        "ecs",
        "describe_tasks",
        {"cluster": cluster_name, "tasks": task_arns},
    )
    tasks = running_tasks.get("tasks")
    if (
        running_tasks.get("failures")
        or type(tasks) is not list
        or len(tasks) != running_count
        or any(
            not isinstance(task, Mapping)
            or task.get("lastStatus") != "RUNNING"
            or task.get("taskDefinitionArn") != task_definition_arn
            for task in tasks
        )
    ):
        raise LaunchOperationError("control-plane running tasks do not match the reviewed image")


def _binding_payload(
    config: OperationConfig,
    binding: ReleaseBinding,
) -> dict[str, Any]:
    return {
        "accountId": binding.account_id,
        "region": binding.release["region"],
        "reviewId": config.review.review_id,
        "reviewExpiresAt": _time_text(config.review.expires_at),
        "reviewedConfigS3Uri": binding.execution["reviewedConfigS3Uri"],
        "reviewedConfigVersionId": binding.execution["reviewedConfigVersionId"],
        "reviewedConfigSha256": binding.execution["reviewedConfigSha256"],
        "coordinatorStateMachineVersionArn": (config.coordinator.state_machine_version_arn),
        "coordinatorLeaseTableArn": config.coordinator.lease_table_arn,
        "coordinatorWatchdogAlarmArn": (config.coordinator.watchdog_alarm_arn),
        "coordinatorCleanupDeadlineSeconds": (config.coordinator.cleanup_deadline_seconds),
        "tenantId": config.scenario.tenant_id,
        "projectId": config.scenario.project_id,
        "runtimeArn": config.resources.runtime_arn,
        "runtimeEndpointArn": config.resources.runtime_endpoint_arn,
        "agentcoreStackArn": config.resources.agentcore_stack_arn,
        "controlPlaneStackArn": config.resources.control_plane_stack_arn,
        "stateTableArn": config.resources.state_table_arn,
        "restoredStateTableArn": config.resources.restored_state_table_arn,
        "outboxQueueArn": config.resources.outbox_queue_arn,
        "outboxQueueUrl": config.resources.outbox_queue_url,
        "deadLetterQueueArn": config.resources.dead_letter_queue_arn,
        "deadLetterQueueUrl": config.resources.dead_letter_queue_url,
        "deadLetterAlarmArn": config.resources.dead_letter_alarm_arn,
        "securityEventLogGroupArn": (config.resources.security_event_log_group_arn),
        "agentcoreImage": binding.release["agentcoreImage"],
        "controlPlaneImage": binding.release["controlPlaneImage"],
    }


def _action_parameters(
    action: str,
    config: OperationConfig,
    *,
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    scenario = config.scenario
    common = {
        "tenantId": scenario.tenant_id,
        "projectId": scenario.project_id,
    }
    if action in {
        "restore-state",
        "cutover-restored-state",
        "verify-restored-state",
        "rollback-primary-state",
        "verify-primary-state",
    }:
        return {
            "primaryTableArn": config.resources.state_table_arn,
            "primaryTableName": config.resources.state_table_arn.rsplit("/", 1)[-1],
            "restoredTableArn": config.resources.restored_state_table_arn,
            "restoredTableName": (config.resources.restored_state_table_arn.rsplit("/", 1)[-1]),
        }
    if action in {
        "deliver-security-events",
        "verify-outbox-drained",
        "force-dead-letter",
        "verify-dead-letter-alarm",
        "redrive-dead-letter",
        "verify-redelivery",
    }:
        return {
            **common,
            "outboxQueueArn": config.resources.outbox_queue_arn,
            "deadLetterQueueArn": config.resources.dead_letter_queue_arn,
            "deadLetterAlarmArn": config.resources.dead_letter_alarm_arn,
        }
    if action in {
        "exercise-routing-strategies",
        "verify-routing-decisions",
    }:
        return {
            **common,
            "model": scenario.model,
            "strategies": list(ROUTING_STRATEGIES),
            "candidateProviders": sorted({scenario.primary_provider, scenario.fallback_provider}),
        }
    if action in {
        "inject-primary-provider-fault",
        "verify-provider-fallback",
        "clear-primary-provider-fault",
        "verify-primary-provider-recovery",
    }:
        return {
            **common,
            "model": scenario.model,
            "primaryProvider": scenario.primary_provider,
            "fallbackProvider": scenario.fallback_provider,
            "failureStatusCode": 503,
            "faultTtlSeconds": scenario.fault_ttl_seconds,
        }
    if action in {
        "inject-control-plane-fault",
        "verify-control-plane-fail-closed",
        "clear-control-plane-fault",
        "verify-control-plane-recovery",
    }:
        return {
            **common,
            "dependency": scenario.control_plane_fault,
            "faultTtlSeconds": scenario.fault_ttl_seconds,
        }
    if action in {
        "induce-initialization-timeout",
        "observe-exit-124",
        "observe-runtime-replacement",
        "verify-replacement-ready",
    }:
        return {
            "startupDeadlineSeconds": scenario.startup_deadline_seconds,
            "faultTtlSeconds": scenario.fault_ttl_seconds,
        }
    if action == "cleanup":
        return {
            "ownership": dict(ownership),
            "primaryTableArn": config.resources.state_table_arn,
            "primaryTableName": config.resources.state_table_arn.rsplit("/", 1)[-1],
            "restoredTableArn": config.resources.restored_state_table_arn,
            "restoredTableName": (config.resources.restored_state_table_arn.rsplit("/", 1)[-1]),
            "outboxQueueArn": config.resources.outbox_queue_arn,
            "deadLetterQueueArn": config.resources.dead_letter_queue_arn,
        }
    raise LaunchOperationError("unsupported rehearsal action")


def _request_payload(
    *,
    gate: str,
    action: str,
    correlation_id: str,
    idempotency_key: str,
    config: OperationConfig,
    binding: ReleaseBinding,
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": ACTION_SCHEMA,
        "gate": gate,
        "operation": action,
        "owner": {
            "id": binding.owner_id,
            "repository": binding.execution["repository"],
            "workflowCommit": binding.execution["workflowCommit"],
            "runId": binding.execution["runId"],
            "runAttempt": binding.execution["runAttempt"],
            "expiresAt": _time_text(config.review.expires_at),
            "authorizationExpiresAtEpoch": str(int((config.review.expires_at + timedelta(days=7)).timestamp())),
        },
        "release": dict(binding.release),
        "execution": dict(binding.execution),
        "correlationId": correlation_id,
        "idempotencyKey": idempotency_key,
        "binding": _binding_payload(config, binding),
        "parameters": _action_parameters(
            action,
            config,
            ownership=ownership,
        ),
    }


def _invoke_action(
    coordinator: CoordinatorTransport,
    *,
    gate: str,
    action: str,
    correlation_id: str,
    idempotency_key: str,
    config: OperationConfig,
    binding: ReleaseBinding,
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _request_payload(
        gate=gate,
        action=action,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        config=config,
        binding=binding,
        ownership=ownership,
    )
    return _object(
        coordinator.invoke(
            payload,
            config=config,
            binding=binding,
        ),
        "launch coordinator result",
    )


def _validate_binding_result(
    value: Any,
    *,
    config: OperationConfig,
    binding: ReleaseBinding,
) -> dict[str, Any]:
    result = _object(value, "action result binding")
    expected = _binding_payload(config, binding)
    _exact_fields(result, set(expected), "action result binding")
    if result != expected:
        raise LaunchOperationError("action result is not bound to the reviewed release resources")
    return expected


def _validate_action_evidence(action: str, value: Any) -> dict[str, Any]:
    evidence = _object(value, "action result evidence")
    expected = ACTION_EVIDENCE_FIELDS[action]
    _exact_fields(evidence, expected, "action result evidence")
    if len(_canonical_bytes(evidence)) > 64 * 1024:
        raise LaunchOperationError("action result evidence is too large")
    return evidence


def _validate_ownership_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    action: str,
) -> None:
    previous_faults = set(previous["faultIds"])
    current_faults = set(current["faultIds"])
    previous_fixtures = set(previous["fixtureIds"])
    current_fixtures = set(current["fixtureIds"])
    previous_dlq = set(previous["dlqCorrelationIds"])
    current_dlq = set(current["dlqCorrelationIds"])
    if current_faults - previous_faults and action not in FAULT_ADD_ACTIONS:
        raise LaunchOperationError("action acquired an unexpected fault")
    if previous_faults - current_faults and action not in FAULT_REMOVE_ACTIONS:
        raise LaunchOperationError("action cleared an unapproved fault")
    if current_fixtures - previous_fixtures and action not in FIXTURE_ADD_ACTIONS:
        raise LaunchOperationError("action acquired an unexpected fixture")
    if previous_fixtures - current_fixtures:
        raise LaunchOperationError("non-cleanup action removed a fixture")
    if current_dlq - previous_dlq and action not in DLQ_ADD_ACTIONS:
        raise LaunchOperationError("action acquired an unexpected DLQ correlation")
    if previous_dlq - current_dlq and action not in DLQ_REMOVE_ACTIONS:
        raise LaunchOperationError("action removed an unapproved DLQ correlation")
    previous_snapshots = previous["snapshots"]
    current_snapshots = current["snapshots"]
    for name in ("model", "tenantConfig"):
        if previous_snapshots[name] is not None and current_snapshots[name] != previous_snapshots[name]:
            raise LaunchOperationError("action replaced or discarded a cleanup snapshot")


def _validate_action_result(
    value: Any,
    *,
    gate: str,
    action: str,
    correlation_id: str,
    idempotency_key: str,
    config: OperationConfig,
    binding: ReleaseBinding,
    previous_ownership: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _object(value, "action result")
    _exact_fields(
        result,
        {
            "schema",
            "gate",
            "operation",
            "ownerId",
            "correlationId",
            "idempotencyKey",
            "status",
            "binding",
            "evidence",
            "ownership",
        },
        "action result",
    )
    if (
        result["schema"] != ACTION_RESULT_SCHEMA
        or result["gate"] != gate
        or result["operation"] != action
        or result["ownerId"] != binding.owner_id
        or result["correlationId"] != correlation_id
        or result["idempotencyKey"] != idempotency_key
        or result["status"] != "SUCCEEDED"
    ):
        raise LaunchOperationError("action result does not prove this exact operation succeeded")
    _validate_binding_result(
        result["binding"],
        config=config,
        binding=binding,
    )
    evidence = _validate_action_evidence(action, result["evidence"])
    ownership = _validate_ownership(
        result["ownership"],
        owner_id=binding.owner_id,
        expected_expiry=_time_text(config.review.expires_at),
    )
    _validate_ownership_transition(
        previous_ownership,
        ownership,
        action=action,
    )
    return evidence, ownership


def _cleanup_evidence(
    value: Any,
    *,
    prior_ownership: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = _object(value, "cleanup evidence")
    expected_fields = {
        "restoredSnapshotRefs",
        "clearedFaultIds",
        "clearedFixtureIds",
        "redrivenDlqCorrelationIds",
        "removedDlqCorrelationIds",
        "primaryStateSelected",
        "productionEndpointStatus",
        "faultsRemaining",
        "fixturesRemaining",
        "correlatedDlqMessagesRemaining",
    }
    _exact_fields(evidence, expected_fields, "cleanup evidence")

    def sorted_list(name: str) -> list[str]:
        raw = evidence[name]
        if (
            type(raw) is not list
            or len(set(raw)) != len(raw)
            or raw != sorted(raw)
            or any(not isinstance(item, str) for item in raw)
        ):
            raise LaunchOperationError(f"cleanup {name} must be a sorted unique array")
        return raw

    snapshot_refs = sorted(
        snapshot["ref"] for snapshot in prior_ownership["snapshots"].values() if snapshot is not None
    )
    redriven = sorted_list("redrivenDlqCorrelationIds")
    removed = sorted_list("removedDlqCorrelationIds")
    if (
        sorted_list("restoredSnapshotRefs") != snapshot_refs
        or sorted_list("clearedFaultIds") != list(prior_ownership["faultIds"])
        or sorted_list("clearedFixtureIds") != list(prior_ownership["fixtureIds"])
        or set(redriven).intersection(removed)
        or sorted(redriven + removed) != list(prior_ownership["dlqCorrelationIds"])
        or evidence["primaryStateSelected"] is not True
        or evidence["productionEndpointStatus"] != "READY"
        or evidence["faultsRemaining"] != 0
        or evidence["fixturesRemaining"] != 0
        or evidence["correlatedDlqMessagesRemaining"] != 0
    ):
        raise LaunchOperationError("cleanup did not restore every owned rehearsal effect")
    return evidence


def _validate_cleanup_result(
    value: Any,
    *,
    correlation_id: str,
    idempotency_key: str,
    config: OperationConfig,
    binding: ReleaseBinding,
    previous_ownership: Mapping[str, Any],
) -> dict[str, Any]:
    result = _object(value, "cleanup result")
    _exact_fields(
        result,
        {
            "schema",
            "gate",
            "operation",
            "ownerId",
            "correlationId",
            "idempotencyKey",
            "status",
            "binding",
            "evidence",
            "ownership",
        },
        "cleanup result",
    )
    if (
        result["schema"] != ACTION_RESULT_SCHEMA
        or result["gate"] != "cleanup"
        or result["operation"] != "cleanup"
        or result["ownerId"] != binding.owner_id
        or result["correlationId"] != correlation_id
        or result["idempotencyKey"] != idempotency_key
        or result["status"] != "SUCCEEDED"
    ):
        raise LaunchOperationError("cleanup result does not prove this exact cleanup succeeded")
    _validate_binding_result(
        result["binding"],
        config=config,
        binding=binding,
    )
    evidence = _cleanup_evidence(
        result["evidence"],
        prior_ownership=previous_ownership,
    )
    ownership = _validate_ownership(
        result["ownership"],
        owner_id=binding.owner_id,
        expected_expiry=_time_text(config.review.expires_at),
    )
    if ownership != _empty_ownership(
        binding.owner_id,
        _time_text(config.review.expires_at),
    ):
        raise LaunchOperationError("cleanup left owned resources behind")
    return evidence


def _correlation_id(binding: ReleaseBinding, gate: str, action: str) -> str:
    material = f"{binding.owner_id}:{gate}:{action}".encode("ascii")
    return hashlib.sha256(material).hexdigest()[:32]


def _idempotency_key(
    binding: ReleaseBinding,
    gate: str,
    action: str,
) -> str:
    material = {
        "ownerId": binding.owner_id,
        "gate": gate,
        "action": action,
        "release": binding.release,
        "execution": binding.execution,
    }
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _command_output(
    gate: str,
    action: str,
    binding: ReleaseBinding,
    observations: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": COMMAND_OUTPUT_SCHEMA,
        "gate": gate,
        "action": action,
        "release": dict(binding.release),
        "execution": dict(binding.execution),
        "observations": observations,
    }


class LaunchRehearsal:
    """Stateful client for a durable out-of-band rehearsal coordinator."""

    def __init__(
        self,
        *,
        config: OperationConfig,
        binding: ReleaseBinding,
        state: StateDirectory,
        aws: AwsTransport,
        coordinator: CoordinatorTransport,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config
        self.binding = binding
        self.state_directory = state
        self.aws = aws
        self.coordinator = coordinator
        self.now = now

    def _load_or_create(
        self,
        *,
        allow_create: bool,
        allow_expired: bool = False,
    ) -> dict[str, Any]:
        current_time = self.now()
        state_value = self.state_directory.read()
        if state_value is None:
            if not allow_create:
                raise LaunchOperationError("cleanup requires an existing owned rehearsal state")
            state_value = _new_state(
                self.config,
                self.binding,
                now=current_time,
            )
            self.state_directory.write(state_value)
        return _validate_state(
            state_value,
            config=self.config,
            binding=self.binding,
            now=current_time,
            allow_expired=allow_expired,
        )

    def run(self, gate: str, action: str) -> dict[str, Any]:
        if gate not in ALL_GATES:
            raise LaunchOperationError("unsupported rehearsal gate")
        expected = EXPECTED_COMMANDS[gate]
        if action not in expected:
            raise LaunchOperationError("action does not belong to the selected rehearsal gate")
        state_value = self._load_or_create(
            allow_create=action == expected[0],
        )
        if state_value["cleanup"] is not None:
            raise LaunchOperationError("rehearsal state is already in cleanup")
        active_gate = state_value["activeGate"]
        if active_gate not in {None, gate}:
            raise LaunchOperationError("another gate owns the rehearsal state")
        gates = state_value["gates"]
        gate_state = gates.get(gate)
        if gate_state is None:
            if action != expected[0]:
                raise LaunchOperationError("gate must start with its first required command")
            gate_state = {
                "nextIndex": 0,
                "evidence": {},
                "actions": {},
            }
            gates[gate] = gate_state
        index = expected.index(action)
        next_index = gate_state["nextIndex"]
        record = gate_state["actions"].get(action)
        correlation_id = _correlation_id(self.binding, gate, action)
        idempotency_key = _idempotency_key(self.binding, gate, action)

        if index < next_index:
            if index != next_index - 1 or record is None:
                raise LaunchOperationError("completed rehearsal commands cannot be replayed out of order")
            verify_launch_role_identity(
                self.aws,
                self.config,
                self.binding,
            )
            result = _invoke_action(
                self.coordinator,
                gate=gate,
                action=action,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                config=self.config,
                binding=self.binding,
                ownership=state_value["ownership"],
            )
            action_evidence, ownership = _validate_action_result(
                result,
                gate=gate,
                action=action,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                config=self.config,
                binding=self.binding,
                previous_ownership=state_value["ownership"],
            )
            recorded_evidence = {name: gate_state["evidence"][name] for name in ACTION_EVIDENCE_FIELDS[action]}
            if (
                record["status"] != "complete"
                or action_evidence != recorded_evidence
                or ownership != state_value["ownership"]
                or hashlib.sha256(_canonical_bytes(result)).hexdigest() != record["resultSha256"]
            ):
                raise LaunchOperationError("resumed action does not match its completed result")
            observations = dict(gate_state["evidence"]) if index == len(expected) - 1 else None
            return _command_output(
                gate,
                action,
                self.binding,
                observations,
            )
        if index != next_index:
            raise LaunchOperationError("rehearsal action is outside the exact command order")

        if record is None:
            if (
                action in FAULT_ADD_ACTIONS
                and self.now() + timedelta(seconds=self.config.scenario.fault_ttl_seconds)
                > self.config.review.expires_at
            ):
                raise LaunchOperationError("fault lease would outlive the reviewed operation window")
            record = {
                "status": "in_progress",
                "correlationId": correlation_id,
                "idempotencyKey": idempotency_key,
                "startedAt": _time_text(self.now()),
            }
            gate_state["actions"][action] = record
            state_value["activeGate"] = gate
            state_value["updatedAt"] = _time_text(self.now())
            self.state_directory.write(state_value)
        elif (
            record["status"] != "in_progress"
            or record["correlationId"] != correlation_id
            or record["idempotencyKey"] != idempotency_key
        ):
            raise LaunchOperationError("in-progress action ownership does not match this invocation")

        verify_launch_role_identity(
            self.aws,
            self.config,
            self.binding,
        )
        result = _invoke_action(
            self.coordinator,
            gate=gate,
            action=action,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            config=self.config,
            binding=self.binding,
            ownership=state_value["ownership"],
        )
        evidence, ownership = _validate_action_result(
            result,
            gate=gate,
            action=action,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            config=self.config,
            binding=self.binding,
            previous_ownership=state_value["ownership"],
        )
        merged = dict(gate_state["evidence"])
        for name, value in evidence.items():
            if name in merged and merged[name] != value:
                raise LaunchOperationError("action contradicted previously recorded evidence")
            merged[name] = value
        observations: dict[str, Any] | None = None
        final = index == len(expected) - 1
        if final:
            try:
                observations = launch_evidence._validate_observations(
                    gate,
                    merged,
                    release=self.binding.release,
                )
            except launch_evidence.LaunchRehearsalError as exc:
                raise LaunchOperationError("final action did not produce complete passing observations") from exc
            if observations != merged:
                merged = observations

        completed = self.now()
        gate_state["evidence"] = merged
        gate_state["nextIndex"] = index + 1
        gate_state["actions"][action] = {
            **record,
            "status": "complete",
            "completedAt": _time_text(completed),
            "resultSha256": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
        }
        state_value["ownership"] = ownership
        state_value["activeGate"] = None if final else gate
        state_value["updatedAt"] = _time_text(completed)
        self.state_directory.write(state_value)
        return _command_output(
            gate,
            action,
            self.binding,
            observations,
        )

    def cleanup(self) -> dict[str, Any]:
        state_value = self._load_or_create(
            allow_create=False,
            allow_expired=True,
        )
        cleanup = state_value["cleanup"]
        correlation_id = _correlation_id(
            self.binding,
            "cleanup",
            "cleanup",
        )
        idempotency_key = _idempotency_key(
            self.binding,
            "cleanup",
            "cleanup",
        )
        if cleanup is not None and cleanup["status"] == "complete":
            prior_ownership = cleanup["priorOwnership"]
            result = _invoke_action(
                self.coordinator,
                gate="cleanup",
                action="cleanup",
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                config=self.config,
                binding=self.binding,
                ownership=prior_ownership,
            )
            cleanup_evidence = _validate_cleanup_result(
                result,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                config=self.config,
                binding=self.binding,
                previous_ownership=prior_ownership,
            )
            if (
                cleanup_evidence != cleanup["evidence"]
                or hashlib.sha256(_canonical_bytes(result)).hexdigest() != cleanup["resultSha256"]
            ):
                raise LaunchOperationError("resumed cleanup does not match its completed result")
            verify_launch_role_identity(
                self.aws,
                self.config,
                self.binding,
            )
            return _command_output(
                "cleanup",
                "cleanup",
                self.binding,
                cleanup_evidence,
            )
        if cleanup is None:
            prior_ownership = state_value["ownership"]
            cleanup = {
                "status": "in_progress",
                "correlationId": correlation_id,
                "idempotencyKey": idempotency_key,
                "startedAt": _time_text(self.now()),
                "priorOwnership": deepcopy(prior_ownership),
            }
            state_value["cleanup"] = cleanup
            state_value["updatedAt"] = _time_text(self.now())
            self.state_directory.write(state_value)
        else:
            _exact_fields(
                cleanup,
                {
                    "status",
                    "correlationId",
                    "idempotencyKey",
                    "startedAt",
                    "priorOwnership",
                },
                "state cleanup",
            )
            if (
                cleanup["status"] != "in_progress"
                or cleanup["correlationId"] != correlation_id
                or cleanup["idempotencyKey"] != idempotency_key
            ):
                raise LaunchOperationError("cleanup ownership does not match this invocation")
            prior_ownership = cleanup["priorOwnership"]
            if prior_ownership != state_value["ownership"]:
                raise LaunchOperationError("cleanup ownership changed after cleanup started")
        result = _invoke_action(
            self.coordinator,
            gate="cleanup",
            action="cleanup",
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            config=self.config,
            binding=self.binding,
            ownership=prior_ownership,
        )
        cleanup_evidence = _validate_cleanup_result(
            result,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            config=self.config,
            binding=self.binding,
            previous_ownership=prior_ownership,
        )
        verify_launch_role_identity(
            self.aws,
            self.config,
            self.binding,
        )
        completed = self.now()
        state_value["ownership"] = _empty_ownership(
            self.binding.owner_id,
            _time_text(self.config.review.expires_at),
        )
        state_value["activeGate"] = None
        state_value["cleanup"] = {
            **cleanup,
            "status": "complete",
            "completedAt": _time_text(completed),
            "resultSha256": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
            "evidenceSha256": hashlib.sha256(_canonical_bytes(cleanup_evidence)).hexdigest(),
            "evidence": cleanup_evidence,
        }
        state_value["updatedAt"] = _time_text(completed)
        self.state_directory.write(state_value)
        return _command_output(
            "cleanup",
            "cleanup",
            self.binding,
            cleanup_evidence,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = SilentArgumentParser(
        description="Run one release-bound AgentCore launch rehearsal action",
        add_help=True,
    )
    parser.add_argument("action")
    parser.add_argument("--reviewed-config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--gate")
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--agentcore-image", required=True)
    parser.add_argument("--control-plane-image", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--reviewed-config-s3-uri", required=True)
    parser.add_argument("--reviewed-config-version-id", required=True)
    parser.add_argument("--reviewed-config-sha256", required=True)
    return parser


def _run_cli(
    args: argparse.Namespace,
    *,
    aws: AwsTransport,
    coordinator: CoordinatorTransport,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    current_time = now()
    config = load_config(
        args.reviewed_config,
        region=args.region,
        now=current_time,
        allow_expired=args.action == "cleanup",
    )
    binding = build_release_binding(
        release_commit=args.release_commit,
        agentcore_image=args.agentcore_image,
        control_plane_image=args.control_plane_image,
        region=args.region,
        repository=args.repository,
        workflow_ref=args.workflow_ref,
        workflow_commit=args.workflow_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        account_id=config.account_id,
        config_sha256=config.sha256,
        reviewed_config_uri=args.reviewed_config_s3_uri,
        reviewed_config_version_id=args.reviewed_config_version_id,
        reviewed_config_sha256=args.reviewed_config_sha256,
    )
    with StateDirectory(args.state_dir) as state_directory:
        runner = LaunchRehearsal(
            config=config,
            binding=binding,
            state=state_directory,
            aws=aws,
            coordinator=coordinator,
            now=now,
        )
        if args.action == "cleanup":
            if args.gate is not None:
                raise LaunchOperationError("cleanup must not specify a gate")
            return runner.cleanup()
        if args.action not in ALL_ACTIONS:
            raise LaunchOperationError("unsupported rehearsal action")
        if args.gate is None:
            raise LaunchOperationError("non-cleanup actions require a gate")
        return runner.run(args.gate, args.action)


def main(
    argv: list[str] | None = None,
    *,
    aws: AwsTransport | None = None,
    coordinator: CoordinatorTransport | None = None,
    now: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run one action, emitting only its normalized command-output object."""
    try:
        args = build_parser().parse_args(argv)
        resolved_aws = aws or BotoAwsTransport()
        resolved_coordinator = coordinator or StepFunctionsCoordinator(
            resolved_aws,
            sleep=sleep,
            monotonic=monotonic,
        )
        output = _run_cli(
            args,
            aws=resolved_aws,
            coordinator=resolved_coordinator,
            now=now,
        )
        sys.stdout.buffer.write(_canonical_bytes(output))
        sys.stdout.buffer.flush()
        return 0
    except CoordinatorNotDrainedError:
        return COORDINATOR_NOT_DRAINED_EXIT_CODE
    except (LaunchOperationError, OSError, ValueError):
        return 2
    except Exception:
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
