#!/usr/bin/env python3
"""Run a fenced Step Functions Activity worker for launch rehearsals.

The worker owns transport safety, task-envelope validation, heartbeats,
fencing, durable idempotency, and Step Functions callbacks. Domain handlers
own every external side effect and must return the complete result evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import re
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import FrameType
from typing import Any, Protocol, TypeAlias


ACTION_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-action/v1"
MAINTENANCE_SCHEMA = "axonllm.agentcore-launch-rehearsal-maintenance/v1"
RESULT_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-result/v1"
RETRY_SCHEMA = "axonllm.agentcore-launch-rehearsal-coordinator-retry/v1"
FAILURE_SCHEMA = "axonllm.agentcore-launch-activity-failure/v1"

ACTION_MODE = "action"
CLEANUP_MODE = "cleanup"
ACTIVITY_NAMES = {
    ACTION_MODE: "axonllm-agentcore-launch-actions",
    CLEANUP_MODE: "axonllm-agentcore-launch-cleanup",
}
MAINTENANCE_OPERATIONS = frozenset({"cleanup-expired", "watchdog"})
LEASE_KEY = "production"
MAX_MAINTENANCE_PAGES = 100

MAX_TASK_INPUT_BYTES = 256 * 1024
MAX_HANDLER_OUTPUT_BYTES = 240 * 1024
MAX_OWNER_STATE_BYTES = 96 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_FAILURE_CAUSE_BYTES = 1024
MAX_JSON_DEPTH = 32
OWNER_RETENTION = timedelta(days=7)
HEARTBEAT_EXPIRY_SECONDS = 60.0

GATE_ACTIONS: Mapping[str, tuple[str, ...]] = {
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
ACTION_TO_GATE = {action: gate for gate, actions in GATE_ACTIONS.items() for action in actions}
ACTION_OPERATIONS = frozenset(ACTION_TO_GATE)
FAULT_ADD_OPERATIONS = frozenset(
    {
        "induce-initialization-timeout",
        "inject-primary-provider-fault",
        "inject-control-plane-fault",
    }
)
ROUTING_STRATEGIES = (
    "cost-optimized",
    "ensemble",
    "least-latency",
    "round-robin",
    "smart",
    "weighted",
)

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
CLEANUP_EVIDENCE_FIELDS = frozenset(
    {
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
)

SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")
ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z0-9-]+-[1-9][0-9]*$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_NUMBER = re.compile(r"^[1-9][0-9]*$")
PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
VERSION_ID = re.compile(r"^[A-Za-z0-9._~+/=-]{1,1024}$")
ERROR_CODE = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
DDB_INDEX_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
ACTIVITY_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):states:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"activity:(?P<name>[A-Za-z0-9_-]{1,80})$"
)
TABLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):dynamodb:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"table/(?P<name>[A-Za-z0-9_.-]{3,255})$"
)
GENERIC_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):"
    r"(?P<service>[a-z0-9-]+):(?P<region>[a-z0-9-]*):"
    r"(?P<account>[0-9]{12})?:(?P<resource>[^\x00-\x1f\x7f]{1,512})$"
)
ECR_IMAGE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@"
    r"sha256:[0-9a-f]{64}$"
)
S3_URI = re.compile(
    r"^s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/"
    r"(?P<key>[A-Za-z0-9][A-Za-z0-9._/-]{0,1023})$"
)
SQS_URL = re.compile(
    r"^https://sqs\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<account>[0-9]{12})/(?P<name>[A-Za-z0-9_-]{1,80}(?:\.fifo)?)$"
)
SECRET_FIELD = re.compile(
    r"(?i)^(?:password|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|private[_-]?key|client[_-]?secret|"
    r"session[_-]?cookie|bearer[_-]?token)$"
)

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class ActivityWorkerError(RuntimeError):
    """A bounded, credential-safe worker failure."""

    code = "ActivityWorkerFailed"
    retryable = False


class ConfigurationError(ActivityWorkerError):
    code = "InvalidWorkerConfiguration"


class TaskInputError(ActivityWorkerError):
    code = "InvalidTaskInput"


class TaskResponseError(ActivityWorkerError):
    code = "InvalidActivityResponse"


class FenceLostError(ActivityWorkerError):
    code = "LaunchFenceLost"


class ReviewExpiredError(ActivityWorkerError):
    code = "LaunchReviewExpired"


class ReplayConflictError(ActivityWorkerError):
    code = "IdempotencyConflict"


class ReplayBusyError(ActivityWorkerError):
    code = "IdempotencyInProgress"
    retryable = True


class HandlerContractError(ActivityWorkerError):
    code = "InvalidHandlerResult"


class HandlerExecutionError(ActivityWorkerError):
    code = "DomainHandlerFailed"
    retryable = True


class ShutdownError(ActivityWorkerError):
    code = "WorkerShuttingDown"
    retryable = True


class HeartbeatLostError(ActivityWorkerError):
    code = "TaskHeartbeatLost"
    retryable = True


class AwsTransportError(ActivityWorkerError):
    """An AWS failure retaining only safe machine-readable identifiers."""

    code = "AwsApiFailure"
    retryable = True

    def __init__(self, service: str, operation: str, aws_code: str) -> None:
        self.service = _bounded_identifier(service, "service", maximum=32)
        self.operation = _bounded_identifier(operation, "operation", maximum=64)
        self.aws_code = aws_code if ERROR_CODE.fullmatch(aws_code) is not None else "Unknown"
        super().__init__(self.code)


class DomainTaskFailure(ActivityWorkerError):
    """A handler-declared safe failure without an arbitrary message."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if ERROR_CODE.fullmatch(code) is None:
            raise HandlerContractError from None
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class AwsTransport(Protocol):
    """Finite-time AWS transport used by the worker and domain handlers."""

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class WorkerConfig:
    """Immutable worker binding to one activity and one lease table."""

    mode: str
    activity_arn: str
    region: str
    lease_table_arn: str
    owner_expiry_index_name: str | None = None
    poll_timeout_seconds: float = 70.0
    api_timeout_seconds: float = 8.0
    heartbeat_interval_seconds: float = 20.0
    claim_ttl_seconds: int = 90
    idle_delay_seconds: float = 0.25
    error_backoff_seconds: float = 2.0
    worker_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    partition: str = field(init=False)
    account_id: str = field(init=False)
    activity_name: str = field(init=False)

    def __post_init__(self) -> None:
        if self.mode not in ACTIVITY_NAMES:
            raise ConfigurationError from None
        if REGION.fullmatch(self.region) is None:
            raise ConfigurationError from None
        activity = ACTIVITY_ARN.fullmatch(self.activity_arn)
        table = TABLE_ARN.fullmatch(self.lease_table_arn)
        if (
            activity is None
            or activity.group("region") != self.region
            or activity.group("name") != ACTIVITY_NAMES[self.mode]
            or table is None
            or table.group("partition") != activity.group("partition")
            or table.group("region") != self.region
            or table.group("account") != activity.group("account")
        ):
            raise ConfigurationError from None
        if self.owner_expiry_index_name is not None and DDB_INDEX_NAME.fullmatch(self.owner_expiry_index_name) is None:
            raise ConfigurationError from None
        if not 61.0 <= self.poll_timeout_seconds <= 75.0:
            raise ConfigurationError from None
        if not 0.1 <= self.api_timeout_seconds <= 8.0:
            raise ConfigurationError from None
        if not 0.05 <= self.heartbeat_interval_seconds <= 30.0:
            raise ConfigurationError from None
        if self.heartbeat_interval_seconds + 3 * self.api_timeout_seconds >= HEARTBEAT_EXPIRY_SECONDS - 5:
            raise ConfigurationError from None
        if not max(61, math.ceil(2 * self.heartbeat_interval_seconds)) <= self.claim_ttl_seconds <= 300:
            raise ConfigurationError from None
        if not 0 <= self.idle_delay_seconds <= 10:
            raise ConfigurationError from None
        if not 0.1 <= self.error_backoff_seconds <= 30:
            raise ConfigurationError from None
        if re.fullmatch(r"[0-9a-f]{32}", self.worker_id) is None:
            raise ConfigurationError from None
        object.__setattr__(self, "partition", activity.group("partition"))
        object.__setattr__(self, "account_id", activity.group("account"))
        object.__setattr__(self, "activity_name", activity.group("name"))

    @property
    def worker_name(self) -> str:
        return f"axonllm-{self.mode}-{self.worker_id[:16]}"


@dataclass(frozen=True)
class ActionTask:
    """Validated action or owner-scoped cleanup task."""

    payload: Mapping[str, JsonValue]
    gate: str
    operation: str
    owner_id: str
    correlation_id: str
    idempotency_key: str
    expires_at: datetime
    fence_token: int | None
    request_sha256: str

    @property
    def is_cleanup(self) -> bool:
        return self.operation == "cleanup"


@dataclass(frozen=True)
class MaintenanceTask:
    """Validated scheduled cleanup or watchdog task."""

    payload: Mapping[str, JsonValue]
    operation: str


ActivityTask: TypeAlias = ActionTask | MaintenanceTask


@dataclass(frozen=True)
class OwnerState:
    """A point-in-time durable domain state snapshot."""

    owner_id: str
    expires_at: datetime
    revision: int
    value: Mapping[str, JsonValue]
    sha256: str


@dataclass(frozen=True)
class HandlerOutcome:
    """Complete domain output and an optional replacement owner state."""

    output: Mapping[str, JsonValue]
    state: Mapping[str, JsonValue] | None = None


class CancellationToken:
    """Cooperative cancellation view over shutdown and task-loss events."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            raise ShutdownError from None


@dataclass(frozen=True)
class HandlerContext:
    """Runtime services available to a domain handler."""

    aws: AwsTransport
    region: str
    state_store: DurableStateStore
    owner_state: OwnerState | None
    cancellation: CancellationToken
    fence_token: int | None


class LaunchActivityHandler(Protocol):
    """Domain API required by :class:`LaunchActivityWorker`."""

    def handle_action(self, task: ActionTask, context: HandlerContext) -> HandlerOutcome: ...

    def handle_cleanup(self, task: ActionTask, context: HandlerContext) -> HandlerOutcome: ...

    def handle_cleanup_expired(self, task: MaintenanceTask, context: HandlerContext) -> Mapping[str, JsonValue]: ...

    def handle_watchdog(self, task: MaintenanceTask, context: HandlerContext) -> Mapping[str, JsonValue]: ...


@dataclass(frozen=True)
class ReplayRecord:
    owner_id: str
    idempotency_key: str
    request_sha256: str
    status: str
    worker_id: str | None
    claim_expires_at_epoch: int | None
    result_json: str | None
    result_sha256: str | None
    base_revision: int | None
    next_revision: int | None
    base_state_sha256: str | None
    next_state_json: str | None
    next_state_sha256: str | None
    expires_at: datetime


@dataclass(frozen=True)
class ExpiredOwnerPage:
    owners: tuple[OwnerState, ...]
    cursor: Mapping[str, Any] | None


class BotoAwsTransport:
    """Boto3 transport with one bounded SDK attempt per runtime call."""

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
        _bounded_identifier(service, "service", maximum=32)
        _bounded_identifier(operation, "operation", maximum=64)
        if REGION.fullmatch(region) is None or not 0.1 <= timeout_seconds <= 75:
            raise AwsTransportError(service, operation, "InvalidRequest")
        timeout = float(timeout_seconds)
        key = (service, region, int(math.ceil(timeout * 1000)))
        client = self._clients.get(key)
        if client is None:
            try:
                import boto3
                from botocore.config import Config

                connect_timeout = min(5.0, timeout * 0.2)
                read_timeout = timeout - connect_timeout
                client = boto3.client(
                    service,
                    region_name=region,
                    config=Config(
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                        retries={"total_max_attempts": 1, "mode": "standard"},
                    ),
                )
            except Exception as exc:
                raise AwsTransportError(service, operation, "ClientInitializationFailed") from exc
            self._clients[key] = client
        method = getattr(client, operation, None)
        if not callable(method):
            raise AwsTransportError(service, operation, "UnknownOperation")
        try:
            response = method(**dict(parameters))
        except Exception as exc:
            raw_response = getattr(exc, "response", None)
            raw_error = raw_response.get("Error") if isinstance(raw_response, Mapping) else None
            raw_code = raw_error.get("Code") if isinstance(raw_error, Mapping) else None
            code = raw_code if isinstance(raw_code, str) and ERROR_CODE.fullmatch(raw_code) is not None else "Unknown"
            raise AwsTransportError(service, operation, code) from exc
        if not isinstance(response, Mapping):
            raise AwsTransportError(service, operation, "InvalidResponse")
        return response


def _bounded_identifier(value: Any, location: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) is None
    ):
        raise ConfigurationError from None
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskInputError from None
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise TaskInputError from None


def _canonical_json(value: Any, *, location: str) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        error = HandlerContractError if location in {"handler output", "owner state"} else TaskInputError
        raise error from exc
    return encoded


def _parse_json_object(
    raw: str,
    *,
    maximum_bytes: int,
    error_type: type[ActivityWorkerError] = TaskInputError,
) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > maximum_bytes:
        raise error_type from None
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ActivityWorkerError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise error_type from exc
    if type(value) is not dict:
        raise error_type from None
    _validate_json_tree(value, error_type=error_type)
    return value


def _validate_json_tree(
    value: Any,
    *,
    error_type: type[ActivityWorkerError],
    depth: int = 0,
) -> None:
    if depth > MAX_JSON_DEPTH:
        raise error_type from None
    if value is None or type(value) in {bool, int, float, str}:
        if isinstance(value, float) and not math.isfinite(value):
            raise error_type from None
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_TASK_INPUT_BYTES:
            raise error_type from None
        return
    if type(value) is list:
        if len(value) > 4096:
            raise error_type from None
        for item in value:
            _validate_json_tree(item, error_type=error_type, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 1024:
            raise error_type from None
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 256
                or any(ord(character) < 32 or ord(character) == 127 for character in key)
            ):
                raise error_type from None
            _validate_json_tree(item, error_type=error_type, depth=depth + 1)
        return
    raise error_type from None


def _object(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise TaskInputError from None
    return value


def _exact(value: Mapping[str, Any], fields: set[str] | frozenset[str]) -> None:
    if set(value) != set(fields):
        raise TaskInputError from None


def _safe_text(
    value: Any,
    *,
    maximum: int = 2048,
    allow_newlines: bool = False,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value.encode("utf-8")) > maximum:
        raise TaskInputError from None
    for character in value:
        ordinal = ord(character)
        if ordinal == 127 or (ordinal < 32 and not (allow_newlines and character in "\n\r\t")):
            raise TaskInputError from None
    return value


def _pattern_text(value: Any, pattern: re.Pattern[str], *, maximum: int = 2048) -> str:
    result = _safe_text(value, maximum=maximum)
    if pattern.fullmatch(result) is None:
        raise TaskInputError from None
    return result


def _integer(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise TaskInputError from None
    return value


def _timestamp(value: Any) -> datetime:
    text = _safe_text(value, maximum=40)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TaskInputError from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise TaskInputError from None
    return parsed.astimezone(timezone.utc)


def _arn(
    value: Any,
    *,
    config: WorkerConfig,
    service: str,
    resource_prefix: str | None = None,
) -> str:
    text = _safe_text(value, maximum=768)
    match = GENERIC_ARN.fullmatch(text)
    if (
        match is None
        or match.group("partition") != config.partition
        or match.group("service") != service
        or match.group("region") != config.region
        or match.group("account") != config.account_id
        or (resource_prefix is not None and not match.group("resource").startswith(resource_prefix))
    ):
        raise TaskInputError from None
    return text


def _validate_secret_free(value: Any) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if SECRET_FIELD.fullmatch(key) is not None:
                raise HandlerContractError from None
            _validate_secret_free(item)
    elif type(value) is list:
        for item in value:
            _validate_secret_free(item)


def _validate_owner_identity(
    owner: Any,
    execution: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> tuple[str, datetime]:
    value = _object(owner)
    _exact(
        value,
        {
            "authorizationExpiresAtEpoch",
            "id",
            "repository",
            "workflowCommit",
            "runId",
            "runAttempt",
            "expiresAt",
        },
    )
    owner_id = _pattern_text(value["id"], SHA256, maximum=64)
    expires_at = _timestamp(value["expiresAt"])
    authorization_expiry = value["authorizationExpiresAtEpoch"]
    if (
        not isinstance(authorization_expiry, str)
        or authorization_expiry != str(_epoch(expires_at + OWNER_RETENTION))
        or value["repository"] != execution["repository"]
        or value["workflowCommit"] != execution["workflowCommit"]
        or value["runId"] != execution["runId"]
        or value["runAttempt"] != execution["runAttempt"]
        or value["expiresAt"] != binding["reviewExpiresAt"]
    ):
        raise TaskInputError from None
    return owner_id, expires_at


def _validate_release(value: Any, config: WorkerConfig) -> dict[str, Any]:
    release = _object(value)
    _exact(
        release,
        {"commit", "region", "agentcoreImage", "controlPlaneImage"},
    )
    _pattern_text(release["commit"], SHA, maximum=40)
    if release["region"] != config.region:
        raise TaskInputError from None
    for name in ("agentcoreImage", "controlPlaneImage"):
        image = _safe_text(release[name], maximum=512)
        match = ECR_IMAGE.fullmatch(image)
        if match is None or match.group("region") != config.region or match.group("account") != config.account_id:
            raise TaskInputError from None
    if release["agentcoreImage"] == release["controlPlaneImage"]:
        raise TaskInputError from None
    return release


def _validate_execution(value: Any) -> dict[str, Any]:
    execution = _object(value)
    _exact(
        execution,
        {
            "repository",
            "workflowRef",
            "workflowCommit",
            "checkedOutCommit",
            "runId",
            "runAttempt",
            "reviewedConfigS3Uri",
            "reviewedConfigVersionId",
            "reviewedConfigSha256",
        },
    )
    repository = _pattern_text(execution["repository"], REPOSITORY, maximum=256)
    workflow_ref = _safe_text(execution["workflowRef"], maximum=512)
    if workflow_ref != (f"{repository}/.github/workflows/agentcore-launch-gates.yml@refs/heads/main"):
        raise TaskInputError from None
    _pattern_text(execution["workflowCommit"], SHA, maximum=40)
    _pattern_text(execution["checkedOutCommit"], SHA, maximum=40)
    _pattern_text(execution["runId"], RUN_NUMBER, maximum=32)
    _pattern_text(execution["runAttempt"], RUN_NUMBER, maximum=16)
    _pattern_text(execution["reviewedConfigS3Uri"], S3_URI, maximum=1100)
    version_id = _pattern_text(execution["reviewedConfigVersionId"], VERSION_ID, maximum=1024)
    if version_id == "null":
        raise TaskInputError from None
    _pattern_text(execution["reviewedConfigSha256"], SHA256, maximum=64)
    return execution


def _validate_binding(
    value: Any,
    *,
    config: WorkerConfig,
    release: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _object(value)
    _exact(
        binding,
        {
            "accountId",
            "region",
            "reviewId",
            "reviewExpiresAt",
            "reviewedConfigS3Uri",
            "reviewedConfigVersionId",
            "reviewedConfigSha256",
            "coordinatorStateMachineVersionArn",
            "coordinatorLeaseTableArn",
            "coordinatorWatchdogAlarmArn",
            "coordinatorCleanupDeadlineSeconds",
            "tenantId",
            "projectId",
            "runtimeArn",
            "runtimeEndpointArn",
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
            "agentcoreImage",
            "controlPlaneImage",
        },
    )
    if binding["accountId"] != config.account_id or binding["region"] != config.region:
        raise TaskInputError from None
    _pattern_text(binding["accountId"], ACCOUNT_ID, maximum=12)
    _pattern_text(binding["reviewId"], SAFE_ID, maximum=256)
    _timestamp(binding["reviewExpiresAt"])
    if (
        binding["reviewedConfigS3Uri"] != execution["reviewedConfigS3Uri"]
        or binding["reviewedConfigVersionId"] != execution["reviewedConfigVersionId"]
        or binding["reviewedConfigSha256"] != execution["reviewedConfigSha256"]
        or binding["agentcoreImage"] != release["agentcoreImage"]
        or binding["controlPlaneImage"] != release["controlPlaneImage"]
        or binding["coordinatorLeaseTableArn"] != config.lease_table_arn
    ):
        raise TaskInputError from None
    _arn(
        binding["coordinatorStateMachineVersionArn"],
        config=config,
        service="states",
        resource_prefix="stateMachine:",
    )
    _arn(
        binding["coordinatorWatchdogAlarmArn"],
        config=config,
        service="cloudwatch",
        resource_prefix="alarm:",
    )
    _integer(
        binding["coordinatorCleanupDeadlineSeconds"],
        minimum=60,
        maximum=1800,
    )
    _pattern_text(binding["tenantId"], SAFE_ID, maximum=256)
    _pattern_text(binding["projectId"], SAFE_ID, maximum=256)
    _arn(
        binding["runtimeArn"],
        config=config,
        service="bedrock-agentcore",
        resource_prefix="runtime/",
    )
    _arn(
        binding["runtimeEndpointArn"],
        config=config,
        service="bedrock-agentcore",
        resource_prefix="runtime/",
    )
    for name in ("agentcoreStackArn", "controlPlaneStackArn"):
        _arn(
            binding[name],
            config=config,
            service="cloudformation",
            resource_prefix="stack/",
        )
    for name in ("stateTableArn", "restoredStateTableArn"):
        _arn(
            binding[name],
            config=config,
            service="dynamodb",
            resource_prefix="table/",
        )
    for name in ("outboxQueueArn", "deadLetterQueueArn"):
        _arn(binding[name], config=config, service="sqs")
    for url_name, arn_name in (
        ("outboxQueueUrl", "outboxQueueArn"),
        ("deadLetterQueueUrl", "deadLetterQueueArn"),
    ):
        queue_url = _pattern_text(
            binding[url_name],
            SQS_URL,
            maximum=2048,
        )
        queue_match = SQS_URL.fullmatch(queue_url)
        queue_arn = GENERIC_ARN.fullmatch(binding[arn_name])
        if (
            queue_match is None
            or queue_arn is None
            or queue_match.group("region") != config.region
            or queue_match.group("account") != config.account_id
            or queue_match.group("name") != queue_arn.group("resource")
        ):
            raise TaskInputError from None
    _arn(
        binding["deadLetterAlarmArn"],
        config=config,
        service="cloudwatch",
        resource_prefix="alarm:",
    )
    _arn(
        binding["securityEventLogGroupArn"],
        config=config,
        service="logs",
        resource_prefix="log-group:",
    )
    return binding


def _validate_owned_id(value: Any, owner_id: str) -> str:
    identifier = _pattern_text(value, SAFE_ID, maximum=256)
    if not identifier.startswith(f"{owner_id}:"):
        raise TaskInputError from None
    return identifier


def _validate_ownership(
    value: Any,
    *,
    owner_id: str,
    expires_at_text: str,
    error_type: type[ActivityWorkerError] = TaskInputError,
) -> dict[str, Any]:
    try:
        ownership = _object(value)
        _exact(
            ownership,
            {
                "ownerId",
                "expiresAt",
                "faultIds",
                "fixtureIds",
                "dlqCorrelationIds",
                "snapshots",
            },
        )
        if ownership["ownerId"] != owner_id or ownership["expiresAt"] != expires_at_text:
            raise TaskInputError from None

        def owned_list(name: str) -> list[str]:
            raw = ownership[name]
            if type(raw) is not list or len(raw) > 256 or raw != sorted(raw) or len(set(raw)) != len(raw):
                raise TaskInputError from None
            return [_validate_owned_id(item, owner_id) for item in raw]

        snapshots = _object(ownership["snapshots"])
        _exact(snapshots, {"model", "tenantConfig"})
        normalized_snapshots: dict[str, Any] = {}
        for name in ("model", "tenantConfig"):
            snapshot = snapshots[name]
            if snapshot is None:
                normalized_snapshots[name] = None
                continue
            item = _object(snapshot)
            _exact(item, {"ref", "sha256", "revision"})
            normalized_snapshots[name] = {
                "ref": _validate_owned_id(item["ref"], owner_id),
                "sha256": _pattern_text(item["sha256"], SHA256, maximum=64),
                "revision": _integer(item["revision"]),
            }
        return {
            "ownerId": owner_id,
            "expiresAt": expires_at_text,
            "faultIds": owned_list("faultIds"),
            "fixtureIds": owned_list("fixtureIds"),
            "dlqCorrelationIds": owned_list("dlqCorrelationIds"),
            "snapshots": normalized_snapshots,
        }
    except TaskInputError as exc:
        if error_type is TaskInputError:
            raise
        raise error_type from exc


def _validate_parameters(
    value: Any,
    *,
    task: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    parameters = _object(value)
    operation = task["operation"]
    common = {"tenantId", "projectId"}
    state = {
        "primaryTableArn",
        "primaryTableName",
        "restoredTableArn",
        "restoredTableName",
    }
    events = {
        *common,
        "outboxQueueArn",
        "deadLetterQueueArn",
        "deadLetterAlarmArn",
    }
    routing = {
        *common,
        "model",
        "strategies",
        "candidateProviders",
    }
    provider_fault = {
        *common,
        "model",
        "primaryProvider",
        "fallbackProvider",
        "failureStatusCode",
        "faultTtlSeconds",
    }
    control_fault = {
        *common,
        "dependency",
        "faultTtlSeconds",
    }
    startup = {"startupDeadlineSeconds", "faultTtlSeconds"}
    groups: tuple[tuple[frozenset[str], set[str]], ...] = (
        (
            frozenset(
                {
                    "restore-state",
                    "cutover-restored-state",
                    "verify-restored-state",
                    "rollback-primary-state",
                    "verify-primary-state",
                }
            ),
            state,
        ),
        (
            frozenset(
                {
                    "deliver-security-events",
                    "verify-outbox-drained",
                    "force-dead-letter",
                    "verify-dead-letter-alarm",
                    "redrive-dead-letter",
                    "verify-redelivery",
                }
            ),
            events,
        ),
        (
            frozenset({"exercise-routing-strategies", "verify-routing-decisions"}),
            routing,
        ),
        (
            frozenset(
                {
                    "inject-primary-provider-fault",
                    "verify-provider-fallback",
                    "clear-primary-provider-fault",
                    "verify-primary-provider-recovery",
                }
            ),
            provider_fault,
        ),
        (
            frozenset(
                {
                    "inject-control-plane-fault",
                    "verify-control-plane-fail-closed",
                    "clear-control-plane-fault",
                    "verify-control-plane-recovery",
                }
            ),
            control_fault,
        ),
        (
            frozenset(
                {
                    "induce-initialization-timeout",
                    "observe-exit-124",
                    "observe-runtime-replacement",
                    "verify-replacement-ready",
                }
            ),
            startup,
        ),
    )
    if operation == "cleanup":
        expected = {
            "ownership",
            "primaryTableArn",
            "primaryTableName",
            "restoredTableArn",
            "restoredTableName",
            "outboxQueueArn",
            "deadLetterQueueArn",
        }
    else:
        expected = next(
            (fields for operations, fields in groups if operation in operations),
            None,
        )
        if expected is None:
            raise TaskInputError from None
    _exact(parameters, expected)

    for name in common.intersection(parameters):
        _pattern_text(parameters[name], SAFE_ID, maximum=256)
    for name, binding_name in (
        ("primaryTableArn", "stateTableArn"),
        ("restoredTableArn", "restoredStateTableArn"),
        ("outboxQueueArn", "outboxQueueArn"),
        ("deadLetterQueueArn", "deadLetterQueueArn"),
        ("deadLetterAlarmArn", "deadLetterAlarmArn"),
    ):
        if name in parameters and parameters[name] != binding[binding_name]:
            raise TaskInputError from None
    for arn_name, table_name in (
        ("primaryTableArn", "primaryTableName"),
        ("restoredTableArn", "restoredTableName"),
    ):
        if arn_name in parameters:
            match = TABLE_ARN.fullmatch(parameters[arn_name])
            if match is None or parameters.get(table_name) != match.group("name"):
                raise TaskInputError from None
    if "model" in parameters:
        _pattern_text(parameters["model"], MODEL, maximum=256)
    if "strategies" in parameters:
        if parameters["strategies"] != list(ROUTING_STRATEGIES):
            raise TaskInputError from None
        candidates = parameters["candidateProviders"]
        if (
            type(candidates) is not list
            or len(candidates) < 2
            or len(candidates) > 16
            or candidates != sorted(candidates)
            or len(set(candidates)) != len(candidates)
        ):
            raise TaskInputError from None
        for candidate in candidates:
            _pattern_text(candidate, PROVIDER, maximum=64)
    if "primaryProvider" in parameters:
        primary = _pattern_text(parameters["primaryProvider"], PROVIDER, maximum=64)
        fallback = _pattern_text(parameters["fallbackProvider"], PROVIDER, maximum=64)
        if primary == fallback or parameters["failureStatusCode"] != 503:
            raise TaskInputError from None
    if "dependency" in parameters and parameters["dependency"] not in {
        "dynamodb",
        "secrets-manager",
        "security-event-outbox",
    }:
        raise TaskInputError from None
    if "faultTtlSeconds" in parameters:
        _integer(parameters["faultTtlSeconds"], minimum=30, maximum=3600)
    if "startupDeadlineSeconds" in parameters:
        _integer(parameters["startupDeadlineSeconds"], minimum=5, maximum=300)
    if operation == "cleanup":
        owner = _object(task["owner"])
        _validate_ownership(
            parameters["ownership"],
            owner_id=owner["id"],
            expires_at_text=owner["expiresAt"],
        )


def _wire_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if type(value) is not dict or set(value) != {"S"}:
        raise TaskInputError from None
    return _safe_text(value["S"], maximum=2048)


def _wire_integer(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if type(value) is not dict or set(value) != {"N"}:
        raise TaskInputError from None
    raw = value["N"]
    if not isinstance(raw, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", raw) is None:
        raise TaskInputError from None
    return int(raw)


def _validate_lease(
    value: Any,
    *,
    owner_id: str,
    correlation_id: str,
    idempotency_key: str,
) -> int:
    lease = _object(value)
    _exact(lease, {"Attributes"})
    attributes = _object(lease["Attributes"])
    required = {
        "leaseKey",
        "ownerId",
        "correlationId",
        "idempotencyKey",
        "status",
        "updatedAt",
        "fenceToken",
    }
    if not required.issubset(attributes) or not set(attributes).issubset(required | {"completedAt", "failedAt"}):
        raise TaskInputError from None
    if (
        _wire_string(attributes, "leaseKey") != LEASE_KEY
        or _wire_string(attributes, "ownerId") != owner_id
        or _wire_string(attributes, "correlationId") != correlation_id
        or _wire_string(attributes, "idempotencyKey") != idempotency_key
        or _wire_string(attributes, "status") != "ACTIVE"
    ):
        raise TaskInputError from None
    _timestamp(_wire_string(attributes, "updatedAt"))
    for optional in ("completedAt", "failedAt"):
        if optional in attributes:
            _timestamp(_wire_string(attributes, optional))
    return _wire_integer(attributes, "fenceToken")


def _validate_maintenance_cursor(value: Any) -> None:
    cursor = _object(value)
    if set(cursor) not in (
        {"leaseKey"},
        {"leaseKey", "recordType", "ownerExpiresAtEpoch"},
    ):
        raise TaskInputError from None
    key = _object(cursor["leaseKey"])
    _exact(key, {"S"})
    lease_key = _safe_text(key["S"], maximum=512)
    if not lease_key.startswith("owner#"):
        raise TaskInputError from None
    if "recordType" in cursor:
        record_type = _object(cursor["recordType"])
        expiry = _object(cursor["ownerExpiresAtEpoch"])
        _exact(record_type, {"S"})
        _exact(expiry, {"N"})
        if record_type["S"] != "OWNER":
            raise TaskInputError from None
        _wire_integer(cursor, "ownerExpiresAtEpoch")


def parse_task_input(
    raw: str,
    *,
    config: WorkerConfig,
    now: datetime,
) -> ActivityTask:
    """Parse and fully validate one activity input envelope."""

    payload = _parse_json_object(
        raw,
        maximum_bytes=MAX_TASK_INPUT_BYTES,
    )
    schema = payload.get("schema")
    if schema == MAINTENANCE_SCHEMA:
        if config.mode != CLEANUP_MODE:
            raise TaskInputError from None
        operation = payload.get("operation")
        if operation not in MAINTENANCE_OPERATIONS:
            raise TaskInputError from None
        if operation == "cleanup-expired" and ("cursor" in payload or "page" in payload):
            _exact(payload, {"schema", "operation", "cursor", "page"})
            _validate_maintenance_cursor(payload["cursor"])
            _integer(
                payload["page"],
                minimum=1,
                maximum=MAX_MAINTENANCE_PAGES,
            )
        else:
            _exact(payload, {"schema", "operation"})
        return MaintenanceTask(payload=payload, operation=operation)
    if schema != ACTION_SCHEMA:
        raise TaskInputError from None
    expected_fields = {
        "schema",
        "gate",
        "operation",
        "owner",
        "release",
        "execution",
        "correlationId",
        "idempotencyKey",
        "binding",
        "parameters",
    }
    operation = payload.get("operation")
    if operation == "cleanup":
        if config.mode != CLEANUP_MODE:
            raise TaskInputError from None
    else:
        if config.mode != ACTION_MODE:
            raise TaskInputError from None
        expected_fields.add("lease")
    _exact(payload, expected_fields)
    if operation == "cleanup":
        if payload["gate"] != "cleanup":
            raise TaskInputError from None
    elif operation not in ACTION_OPERATIONS or payload["gate"] != ACTION_TO_GATE[operation]:
        raise TaskInputError from None

    release = _validate_release(payload["release"], config)
    execution = _validate_execution(payload["execution"])
    if execution["checkedOutCommit"] != release["commit"]:
        raise TaskInputError from None
    binding = _validate_binding(
        payload["binding"],
        config=config,
        release=release,
        execution=execution,
    )
    owner_id, expires_at = _validate_owner_identity(payload["owner"], execution, binding)
    correlation_id = _pattern_text(payload["correlationId"], CORRELATION_ID, maximum=32)
    idempotency_key = _pattern_text(payload["idempotencyKey"], SHA256, maximum=64)
    owner_material = {
        "release": release,
        "execution": execution,
        "configSha256": execution["reviewedConfigSha256"],
    }
    expected_owner_id = hashlib.sha256(
        (_canonical_json(owner_material, location="task input") + "\n").encode("utf-8")
    ).hexdigest()
    expected_correlation_id = hashlib.sha256(f"{owner_id}:{payload['gate']}:{operation}".encode("ascii")).hexdigest()[
        :32
    ]
    idempotency_material = {
        "ownerId": owner_id,
        "gate": payload["gate"],
        "action": operation,
        "release": release,
        "execution": execution,
    }
    expected_idempotency_key = hashlib.sha256(
        (
            _canonical_json(
                idempotency_material,
                location="task input",
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if (
        owner_id != expected_owner_id
        or correlation_id != expected_correlation_id
        or idempotency_key != expected_idempotency_key
    ):
        raise TaskInputError from None
    current = now.astimezone(timezone.utc)
    if operation == "cleanup":
        if current > expires_at + OWNER_RETENTION:
            raise TaskInputError from None
        fence_token = None
    else:
        if current > expires_at:
            raise TaskInputError from None
        fence_token = _validate_lease(
            payload["lease"],
            owner_id=owner_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
    _validate_parameters(
        payload["parameters"],
        task=payload,
        binding=binding,
    )
    if (
        operation in FAULT_ADD_OPERATIONS
        and current + timedelta(seconds=payload["parameters"]["faultTtlSeconds"]) > expires_at
    ):
        raise TaskInputError from None
    digest_payload = dict(payload)
    digest_payload.pop("lease", None)
    request_sha256 = hashlib.sha256(
        _canonical_json(
            digest_payload,
            location="task input",
        ).encode("utf-8")
    ).hexdigest()
    return ActionTask(
        payload=payload,
        gate=payload["gate"],
        operation=operation,
        owner_id=owner_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        expires_at=expires_at,
        fence_token=fence_token,
        request_sha256=request_sha256,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _epoch(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp())


def _ddb_s(value: str) -> dict[str, str]:
    return {"S": value}


def _ddb_n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _ddb_item_string(
    item: Mapping[str, Any],
    name: str,
    *,
    maximum: int = 2048,
    required: bool = True,
) -> str | None:
    raw = item.get(name)
    if raw is None and not required:
        return None
    if type(raw) is not dict or set(raw) != {"S"}:
        raise ReplayConflictError from None
    value = raw["S"]
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ReplayConflictError from None
    return value


def _ddb_item_integer(
    item: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
) -> int | None:
    raw = item.get(name)
    if raw is None and not required:
        return None
    if type(raw) is not dict or set(raw) != {"N"}:
        raise ReplayConflictError from None
    value = raw["N"]
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value) is None:
        raise ReplayConflictError from None
    return int(value)


def _owner_key(owner_id: str) -> str:
    return f"owner#{owner_id}"


def _replay_key(owner_id: str, idempotency_key: str) -> str:
    return f"replay#{owner_id}#{idempotency_key}"


class DurableStateStore:
    """DynamoDB-backed owner state, operation claims, and exact replays."""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        aws: AwsTransport,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config
        self.aws = aws
        self.now = now

    def _call(self, operation: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = self.aws.call(
                "dynamodb",
                operation,
                region=self.config.region,
                parameters=parameters,
                timeout_seconds=self.config.api_timeout_seconds,
            )
        except ActivityWorkerError:
            raise
        except Exception as exc:
            raise AwsTransportError("dynamodb", operation, "TransportFailure") from exc
        if not isinstance(response, Mapping):
            raise AwsTransportError("dynamodb", operation, "InvalidResponse")
        return response

    def _get(self, key: str) -> Mapping[str, Any] | None:
        response = self._call(
            "get_item",
            {
                "TableName": self.config.lease_table_arn,
                "Key": {"leaseKey": _ddb_s(key)},
                "ConsistentRead": True,
            },
        )
        item = response.get("Item")
        if item is None:
            return None
        if type(item) is not dict:
            raise ReplayConflictError from None
        return item

    def verify_fence(self, task: ActionTask) -> None:
        """Fail closed unless the production row still matches this task."""

        if task.fence_token is None:
            return
        response = self._call(
            "get_item",
            {
                "TableName": self.config.lease_table_arn,
                "Key": {"leaseKey": _ddb_s(LEASE_KEY)},
                "ConsistentRead": True,
                "ProjectionExpression": ("leaseKey, ownerId, correlationId, idempotencyKey, #status, fenceToken"),
                "ExpressionAttributeNames": {"#status": "status"},
            },
        )
        item = response.get("Item")
        if type(item) is not dict:
            raise FenceLostError from None
        try:
            valid = (
                _ddb_item_string(item, "leaseKey") == LEASE_KEY
                and _ddb_item_string(item, "ownerId") == task.owner_id
                and _ddb_item_string(item, "correlationId") == task.correlation_id
                and _ddb_item_string(item, "idempotencyKey") == task.idempotency_key
                and _ddb_item_string(item, "status") == "ACTIVE"
                and _ddb_item_integer(item, "fenceToken") == task.fence_token
            )
        except ReplayConflictError as exc:
            raise FenceLostError from exc
        if not valid:
            raise FenceLostError from None

    def load_owner(self, owner_id: str, expires_at: datetime) -> OwnerState:
        item = self._get(_owner_key(owner_id))
        empty_json = "{}"
        empty_sha = hashlib.sha256(empty_json.encode("ascii")).hexdigest()
        if item is None:
            return OwnerState(
                owner_id=owner_id,
                expires_at=expires_at,
                revision=0,
                value={},
                sha256=empty_sha,
            )
        if _ddb_item_string(item, "recordType") != "OWNER" or _ddb_item_string(item, "ownerId") != owner_id:
            raise ReplayConflictError from None
        stored_expiry = _timestamp(_ddb_item_string(item, "ownerExpiresAt", maximum=40))
        if stored_expiry != expires_at:
            raise ReplayConflictError from None
        revision = _ddb_item_integer(item, "revision")
        state_json = _ddb_item_string(item, "stateJson", maximum=MAX_OWNER_STATE_BYTES)
        state_sha = _ddb_item_string(item, "stateSha256", maximum=64)
        if (
            revision is None
            or state_json is None
            or state_sha is None
            or SHA256.fullmatch(state_sha) is None
            or hashlib.sha256(state_json.encode("utf-8")).hexdigest() != state_sha
        ):
            raise ReplayConflictError from None
        try:
            value = _parse_json_object(
                state_json,
                maximum_bytes=MAX_OWNER_STATE_BYTES,
                error_type=ReplayConflictError,
            )
        except TaskInputError as exc:
            raise ReplayConflictError from exc
        return OwnerState(
            owner_id=owner_id,
            expires_at=expires_at,
            revision=revision,
            value=value,
            sha256=state_sha,
        )

    def commit_owner(
        self,
        *,
        previous: OwnerState,
        next_state_json: str,
        next_state_sha256: str,
        next_revision: int,
    ) -> OwnerState:
        if (
            next_revision != previous.revision + 1
            or SHA256.fullmatch(next_state_sha256) is None
            or hashlib.sha256(next_state_json.encode("utf-8")).hexdigest() != next_state_sha256
        ):
            raise ReplayConflictError from None
        current = self.load_owner(previous.owner_id, previous.expires_at)
        if current.revision == next_revision and current.sha256 == next_state_sha256:
            return current
        if current.revision != previous.revision or current.sha256 != previous.sha256:
            raise ReplayConflictError from None
        now = self.now().astimezone(timezone.utc)
        values = {
            ":baseRevision": _ddb_n(previous.revision),
            ":baseSha": _ddb_s(previous.sha256),
            ":expires": _ddb_n(_epoch(previous.expires_at + OWNER_RETENTION)),
            ":nextRevision": _ddb_n(next_revision),
            ":nextSha": _ddb_s(next_state_sha256),
            ":nextState": _ddb_s(next_state_json),
            ":owner": _ddb_s(previous.owner_id),
            ":ownerExpiry": _ddb_s(_time_text(previous.expires_at)),
            ":ownerExpiryEpoch": _ddb_n(_epoch(previous.expires_at)),
            ":recordType": _ddb_s("OWNER"),
            ":updated": _ddb_s(_time_text(now)),
        }
        condition = (
            "attribute_not_exists(leaseKey)"
            if previous.revision == 0
            else ("ownerId = :owner AND revision = :baseRevision AND stateSha256 = :baseSha")
        )
        try:
            self._call(
                "update_item",
                {
                    "TableName": self.config.lease_table_arn,
                    "Key": {"leaseKey": _ddb_s(_owner_key(previous.owner_id))},
                    "ConditionExpression": condition,
                    "UpdateExpression": (
                        "SET recordType = :recordType, ownerId = :owner, "
                        "ownerExpiresAt = :ownerExpiry, "
                        "ownerExpiresAtEpoch = :ownerExpiryEpoch, "
                        "expiresAtEpoch = :expires, revision = :nextRevision, "
                        "stateJson = :nextState, stateSha256 = :nextSha, "
                        "updatedAt = :updated"
                    ),
                    "ExpressionAttributeValues": values,
                },
            )
        except AwsTransportError as exc:
            raise ReplayConflictError from exc
        return OwnerState(
            owner_id=previous.owner_id,
            expires_at=previous.expires_at,
            revision=next_revision,
            value=_parse_json_object(
                next_state_json,
                maximum_bytes=MAX_OWNER_STATE_BYTES,
                error_type=ReplayConflictError,
            ),
            sha256=next_state_sha256,
        )

    def load_replay(self, task: ActionTask) -> ReplayRecord | None:
        item = self._get(_replay_key(task.owner_id, task.idempotency_key))
        if item is None:
            return None
        if (
            _ddb_item_string(item, "recordType") != "REPLAY"
            or _ddb_item_string(item, "ownerId") != task.owner_id
            or _ddb_item_string(item, "idempotencyKey") != task.idempotency_key
            or _ddb_item_string(item, "requestSha256") != task.request_sha256
        ):
            raise ReplayConflictError from None
        status = _ddb_item_string(item, "status")
        if status not in {"RUNNING", "FAILED", "COMPLETE"}:
            raise ReplayConflictError from None
        expires_at = _timestamp(_ddb_item_string(item, "ownerExpiresAt", maximum=40))
        if expires_at != task.expires_at:
            raise ReplayConflictError from None
        worker_id = _ddb_item_string(item, "workerId", maximum=32, required=False)
        claim_expiry = _ddb_item_integer(item, "claimExpiresAtEpoch", required=False)
        result_json = _ddb_item_string(
            item,
            "resultJson",
            maximum=MAX_HANDLER_OUTPUT_BYTES,
            required=False,
        )
        result_sha = _ddb_item_string(item, "resultSha256", maximum=64, required=False)
        base_revision = _ddb_item_integer(item, "baseRevision", required=False)
        next_revision = _ddb_item_integer(item, "nextRevision", required=False)
        base_state_sha = _ddb_item_string(item, "baseStateSha256", maximum=64, required=False)
        next_state_json = _ddb_item_string(
            item,
            "nextStateJson",
            maximum=MAX_OWNER_STATE_BYTES,
            required=False,
        )
        next_state_sha = _ddb_item_string(item, "nextStateSha256", maximum=64, required=False)
        if status == "COMPLETE":
            if (
                worker_id is None
                or result_json is None
                or result_sha is None
                or base_revision is None
                or next_revision is None
                or base_state_sha is None
                or next_state_json is None
                or next_state_sha is None
                or hashlib.sha256(result_json.encode("utf-8")).hexdigest() != result_sha
                or hashlib.sha256(next_state_json.encode("utf-8")).hexdigest() != next_state_sha
                or next_revision != base_revision + 1
            ):
                raise ReplayConflictError from None
        return ReplayRecord(
            owner_id=task.owner_id,
            idempotency_key=task.idempotency_key,
            request_sha256=task.request_sha256,
            status=status,
            worker_id=worker_id,
            claim_expires_at_epoch=claim_expiry,
            result_json=result_json,
            result_sha256=result_sha,
            base_revision=base_revision,
            next_revision=next_revision,
            base_state_sha256=base_state_sha,
            next_state_json=next_state_json,
            next_state_sha256=next_state_sha,
            expires_at=expires_at,
        )

    def acquire_claim(self, task: ActionTask) -> ReplayRecord | None:
        replay = self.load_replay(task)
        now = self.now().astimezone(timezone.utc)
        now_epoch = _epoch(now)
        if replay is not None:
            if replay.status == "COMPLETE":
                return replay
            if (
                replay.status == "RUNNING"
                and replay.worker_id != self.config.worker_id
                and replay.claim_expires_at_epoch is not None
                and replay.claim_expires_at_epoch >= now_epoch
            ):
                raise ReplayBusyError from None
        values = {
            ":failed": _ddb_s("FAILED"),
            ":expires": _ddb_n(_epoch(task.expires_at + OWNER_RETENTION)),
            ":idempotency": _ddb_s(task.idempotency_key),
            ":nowEpoch": _ddb_n(now_epoch),
            ":owner": _ddb_s(task.owner_id),
            ":ownerExpiry": _ddb_s(_time_text(task.expires_at)),
            ":recordType": _ddb_s("REPLAY"),
            ":request": _ddb_s(task.request_sha256),
            ":running": _ddb_s("RUNNING"),
            ":updated": _ddb_s(_time_text(now)),
            ":worker": _ddb_s(self.config.worker_id),
            ":claimExpiry": _ddb_n(now_epoch + self.config.claim_ttl_seconds),
        }
        condition = (
            "attribute_not_exists(leaseKey) OR "
            "(ownerId = :owner AND idempotencyKey = :idempotency "
            "AND requestSha256 = :request AND "
            "(#status = :failed OR claimExpiresAtEpoch < :nowEpoch "
            "OR workerId = :worker))"
        )
        try:
            self._call(
                "update_item",
                {
                    "TableName": self.config.lease_table_arn,
                    "Key": {"leaseKey": _ddb_s(_replay_key(task.owner_id, task.idempotency_key))},
                    "ConditionExpression": condition,
                    "UpdateExpression": (
                        "SET recordType = :recordType, ownerId = :owner, "
                        "idempotencyKey = :idempotency, "
                        "requestSha256 = :request, #status = :running, "
                        "workerId = :worker, "
                        "claimExpiresAtEpoch = :claimExpiry, "
                        "ownerExpiresAt = :ownerExpiry, "
                        "expiresAtEpoch = :expires, updatedAt = :updated "
                        "REMOVE failureCode, resultJson, resultSha256, "
                        "failureRetryable, "
                        "baseRevision, nextRevision, baseStateSha256, "
                        "nextStateJson, nextStateSha256"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                },
            )
        except AwsTransportError as exc:
            raise ReplayConflictError from exc
        return None

    def renew_claim(self, task: ActionTask) -> None:
        now = self.now().astimezone(timezone.utc)
        try:
            self._call(
                "update_item",
                {
                    "TableName": self.config.lease_table_arn,
                    "Key": {"leaseKey": _ddb_s(_replay_key(task.owner_id, task.idempotency_key))},
                    "ConditionExpression": (
                        "ownerId = :owner AND "
                        "idempotencyKey = :idempotency AND "
                        "requestSha256 = :request AND #status = :running "
                        "AND workerId = :worker"
                    ),
                    "UpdateExpression": ("SET claimExpiresAtEpoch = :claimExpiry, updatedAt = :updated"),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":claimExpiry": _ddb_n(_epoch(now) + self.config.claim_ttl_seconds),
                        ":idempotency": _ddb_s(task.idempotency_key),
                        ":owner": _ddb_s(task.owner_id),
                        ":request": _ddb_s(task.request_sha256),
                        ":running": _ddb_s("RUNNING"),
                        ":updated": _ddb_s(_time_text(now)),
                        ":worker": _ddb_s(self.config.worker_id),
                    },
                },
            )
        except AwsTransportError as exc:
            raise ReplayConflictError from exc

    def complete_claim(
        self,
        *,
        task: ActionTask,
        output_json: str,
        output_sha256: str,
        previous: OwnerState,
        next_state_json: str,
        next_state_sha256: str,
    ) -> ReplayRecord:
        now = self.now().astimezone(timezone.utc)
        next_revision = previous.revision + 1
        if (
            hashlib.sha256(output_json.encode("utf-8")).hexdigest() != output_sha256
            or hashlib.sha256(next_state_json.encode("utf-8")).hexdigest() != next_state_sha256
        ):
            raise HandlerContractError from None
        replay_values = {
            ":baseRevision": _ddb_n(previous.revision),
            ":baseSha": _ddb_s(previous.sha256),
            ":complete": _ddb_s("COMPLETE"),
            ":idempotency": _ddb_s(task.idempotency_key),
            ":nextRevision": _ddb_n(next_revision),
            ":nextSha": _ddb_s(next_state_sha256),
            ":nextState": _ddb_s(next_state_json),
            ":output": _ddb_s(output_json),
            ":outputSha": _ddb_s(output_sha256),
            ":owner": _ddb_s(task.owner_id),
            ":request": _ddb_s(task.request_sha256),
            ":running": _ddb_s("RUNNING"),
            ":updated": _ddb_s(_time_text(now)),
            ":worker": _ddb_s(self.config.worker_id),
        }
        owner_values = {
            ":baseRevision": _ddb_n(previous.revision),
            ":baseSha": _ddb_s(previous.sha256),
            ":expires": _ddb_n(_epoch(previous.expires_at + OWNER_RETENTION)),
            ":nextRevision": _ddb_n(next_revision),
            ":nextSha": _ddb_s(next_state_sha256),
            ":nextState": _ddb_s(next_state_json),
            ":owner": _ddb_s(previous.owner_id),
            ":ownerExpiry": _ddb_s(_time_text(previous.expires_at)),
            ":ownerExpiryEpoch": _ddb_n(_epoch(previous.expires_at)),
            ":recordType": _ddb_s("OWNER"),
            ":updated": _ddb_s(_time_text(now)),
        }
        owner_condition = (
            "attribute_not_exists(leaseKey)"
            if previous.revision == 0
            else (
                "recordType = :recordType AND ownerId = :owner AND "
                "ownerExpiresAt = :ownerExpiry AND "
                "revision = :baseRevision AND stateSha256 = :baseSha"
            )
        )
        transact_items: list[dict[str, Any]] = []
        if task.fence_token is not None:
            transact_items.append(
                {
                    "ConditionCheck": {
                        "TableName": self.config.lease_table_arn,
                        "Key": {"leaseKey": _ddb_s(LEASE_KEY)},
                        "ConditionExpression": (
                            "ownerId = :owner AND "
                            "correlationId = :correlation AND "
                            "idempotencyKey = :idempotency AND "
                            "#status = :active AND fenceToken = :fence"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":active": _ddb_s("ACTIVE"),
                            ":correlation": _ddb_s(task.correlation_id),
                            ":fence": _ddb_n(task.fence_token),
                            ":idempotency": _ddb_s(task.idempotency_key),
                            ":owner": _ddb_s(task.owner_id),
                        },
                    }
                }
            )
        transact_items.extend(
            [
                {
                    "Update": {
                        "TableName": self.config.lease_table_arn,
                        "Key": {"leaseKey": _ddb_s(_owner_key(previous.owner_id))},
                        "ConditionExpression": owner_condition,
                        "UpdateExpression": (
                            "SET recordType = :recordType, "
                            "ownerId = :owner, "
                            "ownerExpiresAt = :ownerExpiry, "
                            "ownerExpiresAtEpoch = :ownerExpiryEpoch, "
                            "expiresAtEpoch = :expires, "
                            "revision = :nextRevision, "
                            "stateJson = :nextState, "
                            "stateSha256 = :nextSha, "
                            "updatedAt = :updated"
                        ),
                        "ExpressionAttributeValues": owner_values,
                    }
                },
                {
                    "Update": {
                        "TableName": self.config.lease_table_arn,
                        "Key": {
                            "leaseKey": _ddb_s(
                                _replay_key(
                                    task.owner_id,
                                    task.idempotency_key,
                                )
                            )
                        },
                        "ConditionExpression": (
                            "ownerId = :owner AND "
                            "idempotencyKey = :idempotency AND "
                            "requestSha256 = :request AND "
                            "#status = :running AND workerId = :worker"
                        ),
                        "UpdateExpression": (
                            "SET #status = :complete, "
                            "resultJson = :output, "
                            "resultSha256 = :outputSha, "
                            "baseRevision = :baseRevision, "
                            "nextRevision = :nextRevision, "
                            "baseStateSha256 = :baseSha, "
                            "nextStateJson = :nextState, "
                            "nextStateSha256 = :nextSha, "
                            "updatedAt = :updated "
                            "REMOVE claimExpiresAtEpoch, failureCode, "
                            "failureRetryable"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": replay_values,
                    }
                },
            ]
        )
        try:
            self._call(
                "transact_write_items",
                {
                    "TransactItems": transact_items,
                    "ClientRequestToken": hashlib.sha256(
                        (f"{task.owner_id}:{task.idempotency_key}:{output_sha256}:{next_state_sha256}").encode("ascii")
                    ).hexdigest()[:32],
                },
            )
        except AwsTransportError as exc:
            raise ReplayConflictError from exc
        return ReplayRecord(
            owner_id=task.owner_id,
            idempotency_key=task.idempotency_key,
            request_sha256=task.request_sha256,
            status="COMPLETE",
            worker_id=self.config.worker_id,
            claim_expires_at_epoch=None,
            result_json=output_json,
            result_sha256=output_sha256,
            base_revision=previous.revision,
            next_revision=next_revision,
            base_state_sha256=previous.sha256,
            next_state_json=next_state_json,
            next_state_sha256=next_state_sha256,
            expires_at=task.expires_at,
        )

    def fail_claim(
        self,
        task: ActionTask,
        failure_code: str,
        *,
        retryable: bool = False,
    ) -> bool:
        code = failure_code if ERROR_CODE.fullmatch(failure_code) is not None else "ActivityWorkerFailed"
        now = self.now().astimezone(timezone.utc)
        try:
            self._call(
                "update_item",
                {
                    "TableName": self.config.lease_table_arn,
                    "Key": {"leaseKey": _ddb_s(_replay_key(task.owner_id, task.idempotency_key))},
                    "ConditionExpression": (
                        "ownerId = :owner AND "
                        "idempotencyKey = :idempotency AND "
                        "requestSha256 = :request AND #status = :running "
                        "AND workerId = :worker"
                    ),
                    "UpdateExpression": (
                        "SET #status = :failed, failureCode = :failure, "
                        "failureRetryable = :retryable, "
                        "claimExpiresAtEpoch = :nowEpoch, "
                        "updatedAt = :updated"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":failed": _ddb_s("FAILED"),
                        ":failure": _ddb_s(code),
                        ":idempotency": _ddb_s(task.idempotency_key),
                        ":nowEpoch": _ddb_n(_epoch(now)),
                        ":owner": _ddb_s(task.owner_id),
                        ":request": _ddb_s(task.request_sha256),
                        ":retryable": {"BOOL": retryable},
                        ":running": _ddb_s("RUNNING"),
                        ":updated": _ddb_s(_time_text(now)),
                        ":worker": _ddb_s(self.config.worker_id),
                    },
                },
            )
        except ActivityWorkerError:
            return False
        return True

    def reconcile_replay(self, task: ActionTask, replay: ReplayRecord) -> str:
        if (
            replay.status != "COMPLETE"
            or replay.result_json is None
            or replay.result_sha256 is None
            or replay.base_revision is None
            or replay.next_revision is None
            or replay.base_state_sha256 is None
            or replay.next_state_json is None
            or replay.next_state_sha256 is None
        ):
            raise ReplayConflictError from None
        current = self.load_owner(task.owner_id, task.expires_at)
        if current.revision == replay.next_revision and current.sha256 == replay.next_state_sha256:
            return replay.result_json
        raise ReplayConflictError from None

    def list_expired_owners(
        self,
        *,
        limit: int = 25,
        cursor: Mapping[str, Any] | None = None,
    ) -> ExpiredOwnerPage:
        """Return one bounded, strongly validated page for cleanup handlers."""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise HandlerContractError from None
        now_epoch = _epoch(self.now())
        parameters: dict[str, Any] = {
            "TableName": self.config.lease_table_arn,
            "Limit": limit,
            "ExpressionAttributeValues": {
                ":now": _ddb_n(now_epoch),
                ":ownerType": _ddb_s("OWNER"),
            },
        }
        if self.config.owner_expiry_index_name is None:
            parameters.update(
                {
                    "ConsistentRead": True,
                    "FilterExpression": ("recordType = :ownerType AND ownerExpiresAtEpoch <= :now"),
                }
            )
            operation = "scan"
        else:
            parameters.update(
                {
                    "IndexName": self.config.owner_expiry_index_name,
                    "ConsistentRead": False,
                    "KeyConditionExpression": ("recordType = :ownerType AND ownerExpiresAtEpoch <= :now"),
                }
            )
            operation = "query"
        if cursor is not None:
            parameters["ExclusiveStartKey"] = self._expiry_cursor(
                cursor,
                indexed=self.config.owner_expiry_index_name is not None,
                error_type=HandlerContractError,
            )
        response = self._call(operation, parameters)
        raw_items = response.get("Items", [])
        if type(raw_items) is not list or len(raw_items) > limit:
            raise ReplayConflictError from None
        owners: list[OwnerState] = []
        for item in raw_items:
            if type(item) is not dict:
                raise ReplayConflictError from None
            owner_id = _ddb_item_string(item, "ownerId", maximum=64)
            expires_at = _timestamp(_ddb_item_string(item, "ownerExpiresAt", maximum=40))
            expires_at_epoch = _ddb_item_integer(
                item,
                "ownerExpiresAtEpoch",
            )
            if expires_at_epoch != _epoch(expires_at) or expires_at_epoch > now_epoch:
                raise ReplayConflictError from None
            owners.append(self._owner_from_item(item, owner_id, expires_at))
        next_cursor = response.get("LastEvaluatedKey")
        if next_cursor is not None:
            next_cursor = self._expiry_cursor(
                next_cursor,
                indexed=self.config.owner_expiry_index_name is not None,
                error_type=ReplayConflictError,
            )
        return ExpiredOwnerPage(
            owners=tuple(owners),
            cursor=next_cursor,
        )

    @staticmethod
    def _expiry_cursor(
        value: Any,
        *,
        indexed: bool,
        error_type: type[ActivityWorkerError],
    ) -> dict[str, Any]:
        if type(value) is not dict:
            raise error_type from None
        expected = {"leaseKey", "recordType", "ownerExpiresAtEpoch"} if indexed else {"leaseKey"}
        if set(value) != expected:
            raise error_type from None
        try:
            lease_key = _ddb_item_string(
                value,
                "leaseKey",
                maximum=512,
            )
            if lease_key is None or not lease_key.startswith("owner#"):
                raise error_type from None
            if indexed:
                if _ddb_item_string(value, "recordType") != "OWNER":
                    raise error_type from None
                _ddb_item_integer(value, "ownerExpiresAtEpoch")
        except ReplayConflictError as exc:
            raise error_type from exc
        return dict(value)

    def _owner_from_item(
        self,
        item: Mapping[str, Any],
        owner_id: str,
        expires_at: datetime,
    ) -> OwnerState:
        if _ddb_item_string(item, "recordType") != "OWNER" or _ddb_item_string(
            item,
            "leaseKey",
            maximum=512,
        ) != _owner_key(owner_id):
            raise ReplayConflictError from None
        revision = _ddb_item_integer(item, "revision")
        state_json = _ddb_item_string(item, "stateJson", maximum=MAX_OWNER_STATE_BYTES)
        state_sha = _ddb_item_string(item, "stateSha256", maximum=64)
        if (
            revision is None
            or state_json is None
            or state_sha is None
            or hashlib.sha256(state_json.encode("utf-8")).hexdigest() != state_sha
        ):
            raise ReplayConflictError from None
        state = _parse_json_object(
            state_json,
            maximum_bytes=MAX_OWNER_STATE_BYTES,
            error_type=ReplayConflictError,
        )
        return OwnerState(
            owner_id=owner_id,
            expires_at=expires_at,
            revision=revision,
            value=state,
            sha256=state_sha,
        )


def _handler_json(
    value: Any,
    *,
    location: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str, str]:
    if type(value) is not dict:
        raise HandlerContractError from None
    _validate_json_tree(value, error_type=HandlerContractError)
    _validate_secret_free(value)
    encoded = _canonical_json(value, location=location)
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise HandlerContractError from None
    return value, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_handler_output(
    task: ActivityTask,
    value: Any,
) -> tuple[dict[str, Any], str, str]:
    output, encoded, digest = _handler_json(
        value,
        location="handler output",
        maximum_bytes=MAX_HANDLER_OUTPUT_BYTES,
    )
    if isinstance(task, MaintenanceTask):
        return output, encoded, digest
    try:
        _exact(
            output,
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
        )
    except TaskInputError as exc:
        raise HandlerContractError from exc
    if (
        output["schema"] != RESULT_SCHEMA
        or output["gate"] != task.gate
        or output["operation"] != task.operation
        or output["ownerId"] != task.owner_id
        or output["correlationId"] != task.correlation_id
        or output["idempotencyKey"] != task.idempotency_key
        or output["status"] != "SUCCEEDED"
        or output["binding"] != task.payload["binding"]
    ):
        raise HandlerContractError from None
    evidence = output["evidence"]
    if type(evidence) is not dict:
        raise HandlerContractError from None
    expected_evidence = CLEANUP_EVIDENCE_FIELDS if task.is_cleanup else ACTION_EVIDENCE_FIELDS[task.operation]
    if set(evidence) != set(expected_evidence):
        raise HandlerContractError from None
    evidence_json = _canonical_json(evidence, location="handler output")
    if len(evidence_json.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise HandlerContractError from None
    owner = _object(task.payload["owner"])
    _validate_ownership(
        output["ownership"],
        owner_id=task.owner_id,
        expires_at_text=owner["expiresAt"],
        error_type=HandlerContractError,
    )
    return output, encoded, digest


def _normalize_outcome(
    task: ActionTask,
    outcome: Any,
    previous: OwnerState,
) -> tuple[str, str, str, str]:
    if not isinstance(outcome, HandlerOutcome):
        raise HandlerContractError from None
    _, output_json, output_sha = _validate_handler_output(task, outcome.output)
    next_state = previous.value if outcome.state is None else outcome.state
    _, state_json, state_sha = _handler_json(
        next_state,
        location="owner state",
        maximum_bytes=MAX_OWNER_STATE_BYTES,
    )
    return output_json, output_sha, state_json, state_sha


def _failure_payload(error: ActivityWorkerError) -> tuple[str, str]:
    code = error.code if ERROR_CODE.fullmatch(error.code) is not None else "ActivityWorkerFailed"
    cause = _canonical_json(
        {
            "schema": FAILURE_SCHEMA,
            "code": code,
            "retryable": bool(error.retryable),
        },
        location="failure",
    )
    if len(cause.encode("utf-8")) > MAX_FAILURE_CAUSE_BYTES:
        cause = f'{{"code":"ActivityWorkerFailed","retryable":false,"schema":"{FAILURE_SCHEMA}"}}'
        code = "ActivityWorkerFailed"
    return code, cause


def _retry_output(task: ActionTask, error: DomainTaskFailure) -> str:
    if not error.retryable:
        raise HandlerContractError from None
    value = {
        "schema": RETRY_SCHEMA,
        "status": "RETRY",
        "gate": task.gate,
        "operation": task.operation,
        "ownerId": task.owner_id,
        "correlationId": task.correlation_id,
        "idempotencyKey": task.idempotency_key,
        "code": error.code,
        "retryable": True,
    }
    _, encoded, _ = _handler_json(
        value,
        location="handler output",
        maximum_bytes=MAX_HANDLER_OUTPUT_BYTES,
    )
    return encoded


class _HeartbeatPump:
    def __init__(
        self,
        *,
        worker: LaunchActivityWorker,
        task_token: str,
        task: ActivityTask,
        has_claim: bool,
    ) -> None:
        self.worker = worker
        self.task_token = task_token
        self.task = task
        self.has_claim = has_claim
        self.done = threading.Event()
        self.lost = threading.Event()
        self.failure: ActivityWorkerError | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.worker._heartbeat_once(
            self.task_token,
            self.task,
            has_claim=self.has_claim,
        )
        self.thread = threading.Thread(
            target=self._run,
            name="launch-activity-heartbeat",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.done.wait(self.worker.config.heartbeat_interval_seconds):
            try:
                self.worker._heartbeat_once(
                    self.task_token,
                    self.task,
                    has_claim=self.has_claim,
                )
            except ActivityWorkerError as exc:
                self.failure = exc
                self.lost.set()
                return
            except Exception:
                self.failure = HeartbeatLostError()
                self.lost.set()
                return

    def stop(self) -> None:
        self.done.set()
        if self.thread is not None:
            budget = (
                3 * self.worker.config.api_timeout_seconds + 1
                if isinstance(self.task, ActionTask)
                else self.worker.config.api_timeout_seconds + 1
            )
            self.thread.join(timeout=budget)
            if self.thread.is_alive():
                self.failure = HeartbeatLostError()
                self.lost.set()

    def raise_if_lost(self) -> None:
        if self.lost.is_set():
            if self.failure is not None:
                raise self.failure
            raise HeartbeatLostError from None


class LaunchActivityWorker:
    """A single-activity worker with fenced, replay-safe task execution."""

    def __init__(
        self,
        *,
        config: WorkerConfig,
        aws: AwsTransport,
        handler: LaunchActivityHandler,
        state_store: DurableStateStore | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config
        self.aws = aws
        self.handler = handler
        self.now = now
        self.state_store = state_store or DurableStateStore(
            config=config,
            aws=aws,
            now=now,
        )
        self.shutdown_requested = threading.Event()

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()

    def _aws_call(
        self,
        service: str,
        operation: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = self.aws.call(
                service,
                operation,
                region=self.config.region,
                parameters=parameters,
                timeout_seconds=(self.config.api_timeout_seconds if timeout_seconds is None else timeout_seconds),
            )
        except ActivityWorkerError:
            raise
        except Exception as exc:
            raise AwsTransportError(service, operation, "TransportFailure") from exc
        if not isinstance(response, Mapping):
            raise AwsTransportError(service, operation, "InvalidResponse")
        return response

    def _send_heartbeat(self, task_token: str) -> None:
        self._aws_call(
            "stepfunctions",
            "send_task_heartbeat",
            {"taskToken": task_token},
        )

    def _heartbeat_once(
        self,
        task_token: str,
        task: ActivityTask,
        *,
        has_claim: bool,
    ) -> None:
        if (
            isinstance(task, ActionTask)
            and not task.is_cleanup
            and self.now().astimezone(timezone.utc) > task.expires_at
        ):
            raise ReviewExpiredError from None
        self._send_heartbeat(task_token)
        if isinstance(task, ActionTask):
            self.state_store.verify_fence(task)
            if has_claim:
                self.state_store.renew_claim(task)

    def _send_success(self, task_token: str, output_json: str) -> None:
        if not output_json or len(output_json.encode("utf-8")) > MAX_HANDLER_OUTPUT_BYTES:
            raise HandlerContractError from None
        self._aws_call(
            "stepfunctions",
            "send_task_success",
            {"taskToken": task_token, "output": output_json},
        )

    def _send_failure(self, task_token: str, error: ActivityWorkerError) -> bool:
        error_code, cause = _failure_payload(error)
        try:
            self._aws_call(
                "stepfunctions",
                "send_task_failure",
                {
                    "taskToken": task_token,
                    "error": error_code,
                    "cause": cause,
                },
            )
        except ActivityWorkerError:
            return False
        return True

    def _record_claim_failure(
        self,
        task: ActionTask,
        failure_code: str,
        *,
        retryable: bool = False,
    ) -> bool:
        try:
            return bool(
                self.state_store.fail_claim(
                    task,
                    failure_code,
                    retryable=retryable,
                )
            )
        except Exception:
            # The Step Functions callback is the last bounded way to release
            # the execution. A stale or unavailable replay row must not prevent
            # that callback; the durable replay path reconciles it on retry.
            return False

    def _poll(self) -> Mapping[str, Any]:
        return self._aws_call(
            "stepfunctions",
            "get_activity_task",
            {
                "activityArn": self.config.activity_arn,
                "workerName": self.config.worker_name,
            },
            timeout_seconds=self.config.poll_timeout_seconds,
        )

    @staticmethod
    def _task_response(
        response: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        token = response.get("taskToken")
        raw_input = response.get("input")
        if token in {None, ""}:
            if raw_input not in {None, ""}:
                raise TaskResponseError from None
            return None
        if (
            not isinstance(token, str)
            or len(token.encode("utf-8")) > 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in token)
            or not isinstance(raw_input, str)
        ):
            raise TaskResponseError from None
        return token, raw_input

    def poll_once(self) -> str:
        """Poll once and return a safe lifecycle status."""

        if self.shutdown_requested.is_set():
            return "stopped"
        response = self._poll()
        task_response = self._task_response(response)
        if task_response is None:
            return "idle"
        task_token, raw_input = task_response
        if self.shutdown_requested.is_set():
            return "failed" if self._send_failure(task_token, ShutdownError()) else "callback-lost"
        try:
            task = parse_task_input(
                raw_input,
                config=self.config,
                now=self.now(),
            )
        except ActivityWorkerError as exc:
            return "failed" if self._send_failure(task_token, exc) else "callback-lost"
        return self._execute(task_token, task)

    def _execute(self, task_token: str, task: ActivityTask) -> str:
        if isinstance(task, MaintenanceTask):
            return self._execute_maintenance(task_token, task)
        return self._execute_owner_task(task_token, task)

    def _execute_maintenance(
        self,
        task_token: str,
        task: MaintenanceTask,
    ) -> str:
        pump = _HeartbeatPump(
            worker=self,
            task_token=task_token,
            task=task,
            has_claim=False,
        )
        context = HandlerContext(
            aws=self.aws,
            region=self.config.region,
            state_store=self.state_store,
            owner_state=None,
            cancellation=CancellationToken(self.shutdown_requested, pump.lost),
            fence_token=None,
        )
        try:
            pump.start()
            if task.operation == "cleanup-expired":
                output = self.handler.handle_cleanup_expired(task, context)
            elif task.operation == "watchdog":
                output = self.handler.handle_watchdog(task, context)
            else:
                raise TaskInputError from None
            _, output_json, _ = _validate_handler_output(task, output)
            pump.stop()
            pump.raise_if_lost()
            self._send_heartbeat(task_token)
            self._send_success(task_token, output_json)
            return "succeeded"
        except DomainTaskFailure as exc:
            pump.stop()
            return "failed" if self._send_failure(task_token, exc) else "callback-lost"
        except ActivityWorkerError as exc:
            pump.stop()
            return "failed" if self._send_failure(task_token, exc) else "callback-lost"
        except Exception:
            pump.stop()
            return "failed" if self._send_failure(task_token, HandlerExecutionError()) else "callback-lost"

    def _execute_owner_task(
        self,
        task_token: str,
        task: ActionTask,
    ) -> str:
        claim_acquired = False
        pump: _HeartbeatPump | None = None
        try:
            self._send_heartbeat(task_token)
            self.state_store.verify_fence(task)
            replay = self.state_store.load_replay(task)
            if replay is not None and replay.status == "COMPLETE":
                self._send_heartbeat(task_token)
                result_json = self.state_store.reconcile_replay(task, replay)
                self.state_store.verify_fence(task)
                self._send_success(task_token, result_json)
                return "replayed"

            raced_replay = self.state_store.acquire_claim(task)
            if raced_replay is not None:
                self._send_heartbeat(task_token)
                result_json = self.state_store.reconcile_replay(task, raced_replay)
                self.state_store.verify_fence(task)
                self._send_success(task_token, result_json)
                return "replayed"
            claim_acquired = True
            previous = self.state_store.load_owner(task.owner_id, task.expires_at)
            pump = _HeartbeatPump(
                worker=self,
                task_token=task_token,
                task=task,
                has_claim=True,
            )
            context = HandlerContext(
                aws=self.aws,
                region=self.config.region,
                state_store=self.state_store,
                owner_state=previous,
                cancellation=CancellationToken(self.shutdown_requested, pump.lost),
                fence_token=task.fence_token,
            )
            pump.start()
            if task.is_cleanup:
                outcome = self.handler.handle_cleanup(task, context)
            else:
                outcome = self.handler.handle_action(task, context)
            (
                output_json,
                output_sha,
                next_state_json,
                next_state_sha,
            ) = _normalize_outcome(task, outcome, previous)
            pump.stop()
            pump.raise_if_lost()
            self._heartbeat_once(
                task_token,
                task,
                has_claim=True,
            )
            replay = self.state_store.complete_claim(
                task=task,
                output_json=output_json,
                output_sha256=output_sha,
                previous=previous,
                next_state_json=next_state_json,
                next_state_sha256=next_state_sha,
            )
            if replay.result_json != output_json:
                raise ReplayConflictError from None
            self.state_store.verify_fence(task)
            self._send_success(task_token, output_json)
            return "succeeded"
        except DomainTaskFailure as exc:
            if pump is not None:
                pump.stop()
            if claim_acquired:
                claim_released = self._record_claim_failure(
                    task,
                    exc.code,
                    retryable=exc.retryable,
                )
                if exc.retryable and claim_released:
                    try:
                        self._send_success(
                            task_token,
                            _retry_output(task, exc),
                        )
                    except ActivityWorkerError:
                        return "callback-lost"
                    return "retry"
            return "failed" if self._send_failure(task_token, exc) else "callback-lost"
        except ActivityWorkerError as exc:
            if pump is not None:
                pump.stop()
            if claim_acquired:
                self._record_claim_failure(task, exc.code)
            return "failed" if self._send_failure(task_token, exc) else "callback-lost"
        except Exception:
            if pump is not None:
                pump.stop()
            if claim_acquired:
                self._record_claim_failure(
                    task,
                    HandlerExecutionError.code,
                )
            return "failed" if self._send_failure(task_token, HandlerExecutionError()) else "callback-lost"

    def run_forever(self, *, max_polls: int | None = None) -> int:
        """Poll until SIGTERM/shutdown, with bounded retry backoff."""

        if max_polls is not None and (type(max_polls) is not int or max_polls < 0):
            raise ConfigurationError from None
        polls = 0
        failures = 0
        while not self.shutdown_requested.is_set():
            if max_polls is not None and polls >= max_polls:
                break
            try:
                status = self.poll_once()
                polls += 1
                if status == "idle":
                    self.shutdown_requested.wait(self.config.idle_delay_seconds)
                failures = 0
            except ActivityWorkerError:
                failures += 1
                delay = min(
                    self.config.error_backoff_seconds * (2 ** (failures - 1)),
                    30.0,
                )
                self.shutdown_requested.wait(delay)
        return polls


def load_handler(
    module_name: str,
    *,
    aws: AwsTransport,
    config: WorkerConfig,
) -> LaunchActivityHandler:
    """Load ``create_handler`` from the separately deployed domain module."""

    if MODULE_NAME.fullmatch(module_name) is None:
        raise ConfigurationError from None
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, "create_handler")
        if not callable(factory):
            raise TypeError
        handler = factory(
            aws=aws,
            region=config.region,
            lease_table_arn=config.lease_table_arn,
        )
    except Exception as exc:
        raise ConfigurationError from exc
    for method_name in (
        "handle_action",
        "handle_cleanup",
        "handle_cleanup_expired",
        "handle_watchdog",
    ):
        if not callable(getattr(handler, method_name, None)):
            raise ConfigurationError from None
    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=("Run one exact AxonLLM launch Step Functions Activity worker"))
    parser.add_argument("--mode", choices=sorted(ACTIVITY_NAMES), required=True)
    parser.add_argument("--activity-arn", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--lease-table-arn", required=True)
    parser.add_argument("--owner-expiry-index-name")
    parser.add_argument(
        "--handler-module",
        default="launch_activity_domains",
    )
    parser.add_argument("--poll-timeout-seconds", type=float, default=70.0)
    parser.add_argument("--api-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=20.0)
    parser.add_argument("--claim-ttl-seconds", type=int, default=90)
    return parser


def _install_signal_handlers(
    worker: LaunchActivityWorker,
) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def stop(_signum: int, _frame: FrameType | None) -> None:
        worker.request_shutdown()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, stop)
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = WorkerConfig(
            mode=args.mode,
            activity_arn=args.activity_arn,
            region=args.region,
            lease_table_arn=args.lease_table_arn,
            owner_expiry_index_name=args.owner_expiry_index_name,
            poll_timeout_seconds=args.poll_timeout_seconds,
            api_timeout_seconds=args.api_timeout_seconds,
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            claim_ttl_seconds=args.claim_ttl_seconds,
        )
        aws = BotoAwsTransport()
        handler = load_handler(
            args.handler_module,
            aws=aws,
            config=config,
        )
        worker = LaunchActivityWorker(
            config=config,
            aws=aws,
            handler=handler,
        )
    except ActivityWorkerError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    previous = _install_signal_handlers(worker)
    try:
        worker.run_forever()
    finally:
        _restore_signal_handlers(previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
