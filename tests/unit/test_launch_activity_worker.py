"""Network-free contracts for the production launch Activity worker."""

from __future__ import annotations

import hashlib
import json
import signal
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import launch_activity_worker as worker


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=2)
REGION = "us-east-1"
ACCOUNT = "123456789012"
COMMIT = "4" * 40
WORKFLOW_COMMIT = "5" * 40
CONFIG_SHA = "6" * 64
AGENTCORE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm-agentcore@sha256:{'7' * 64}"
CONTROL_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm-control@sha256:{'8' * 64}"
ACTION_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:activity:axonllm-agentcore-launch-actions"
CLEANUP_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:activity:axonllm-agentcore-launch-cleanup"
LEASE_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-launch-rehearsal-leases"
STATE_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-agentcore-state"
RESTORED_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/axonllm-agentcore-state-restore"
OUTBOX_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:axonllm-outbox"
DLQ_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:axonllm-outbox-dlq"
ALARM_ARN = f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:axonllm-outbox-dlq"
AGENTCORE_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/AxonLLMAgentCoreStack/11111111-1111-1111-1111-111111111111"
)
CONTROL_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/AxonLLMControlPlaneStack/22222222-2222-2222-2222-222222222222"
)
OUTBOX_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/axonllm-outbox"
DLQ_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/axonllm-outbox-dlq"
SECURITY_LOG_GROUP_ARN = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/axonllm/security"


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


_RELEASE_IDENTITY = {
    "commit": COMMIT,
    "region": REGION,
    "agentcoreImage": AGENTCORE_IMAGE,
    "controlPlaneImage": CONTROL_IMAGE,
}
_EXECUTION_IDENTITY = {
    "repository": "AxonLLM/axonllm",
    "workflowRef": ("AxonLLM/axonllm/.github/workflows/agentcore-launch-gates.yml@refs/heads/main"),
    "workflowCommit": WORKFLOW_COMMIT,
    "checkedOutCommit": COMMIT,
    "runId": "41",
    "runAttempt": "2",
    "reviewedConfigS3Uri": "s3://axonllm-review/config/launch.json",
    "reviewedConfigVersionId": "version-1",
    "reviewedConfigSha256": CONFIG_SHA,
}


def _producer_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256((encoded + "\n").encode()).hexdigest()


OWNER = _producer_digest(
    {
        "release": _RELEASE_IDENTITY,
        "execution": _EXECUTION_IDENTITY,
        "configSha256": CONFIG_SHA,
    }
)
CORRELATION = hashlib.sha256(f"{OWNER}:initializationTimeoutReplacement:observe-exit-124".encode()).hexdigest()[:32]
IDEMPOTENCY = _producer_digest(
    {
        "ownerId": OWNER,
        "gate": "initializationTimeoutReplacement",
        "action": "observe-exit-124",
        "release": _RELEASE_IDENTITY,
        "execution": _EXECUTION_IDENTITY,
    }
)


def _config(
    mode: str = worker.ACTION_MODE,
    *,
    heartbeat: float = 0.05,
    owner_expiry_index_name: str | None = None,
) -> worker.WorkerConfig:
    return worker.WorkerConfig(
        mode=mode,
        activity_arn=(ACTION_ARN if mode == worker.ACTION_MODE else CLEANUP_ARN),
        region=REGION,
        lease_table_arn=LEASE_TABLE_ARN,
        owner_expiry_index_name=owner_expiry_index_name,
        api_timeout_seconds=0.1,
        heartbeat_interval_seconds=heartbeat,
        claim_ttl_seconds=61,
        idle_delay_seconds=0,
        error_backoff_seconds=0.1,
        worker_id="9" * 32,
    )


def _binding() -> dict[str, Any]:
    return {
        "accountId": ACCOUNT,
        "region": REGION,
        "reviewId": "launch-review-1",
        "reviewExpiresAt": _time_text(EXPIRES),
        "reviewedConfigS3Uri": "s3://axonllm-review/config/launch.json",
        "reviewedConfigVersionId": "version-1",
        "reviewedConfigSha256": CONFIG_SHA,
        "coordinatorStateMachineVersionArn": (
            f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:AxonLLMLaunchCoordinator:7"
        ),
        "coordinatorLeaseTableArn": LEASE_TABLE_ARN,
        "coordinatorWatchdogAlarmArn": (f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:axonllm-launch-watchdog"),
        "coordinatorCleanupDeadlineSeconds": 900,
        "tenantId": "tenant-launch",
        "projectId": "project-launch",
        "runtimeArn": (f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/AxonLLMRuntime-abcdefghij"),
        "runtimeEndpointArn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            "runtime/AxonLLMRuntime-abcdefghij/runtime-endpoint/production"
        ),
        "agentcoreStackArn": AGENTCORE_STACK_ARN,
        "controlPlaneStackArn": CONTROL_STACK_ARN,
        "stateTableArn": STATE_TABLE_ARN,
        "restoredStateTableArn": RESTORED_TABLE_ARN,
        "outboxQueueArn": OUTBOX_ARN,
        "outboxQueueUrl": OUTBOX_URL,
        "deadLetterQueueArn": DLQ_ARN,
        "deadLetterQueueUrl": DLQ_URL,
        "deadLetterAlarmArn": ALARM_ARN,
        "securityEventLogGroupArn": SECURITY_LOG_GROUP_ARN,
        "agentcoreImage": AGENTCORE_IMAGE,
        "controlPlaneImage": CONTROL_IMAGE,
    }


def _ownership() -> dict[str, Any]:
    return {
        "ownerId": OWNER,
        "expiresAt": _time_text(EXPIRES),
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _action_payload(
    *,
    operation: str = "observe-exit-124",
    fence: int = 11,
) -> dict[str, Any]:
    gate = worker.ACTION_TO_GATE[operation]
    correlation_id = hashlib.sha256(f"{OWNER}:{gate}:{operation}".encode()).hexdigest()[:32]
    idempotency_key = _producer_digest(
        {
            "ownerId": OWNER,
            "gate": gate,
            "action": operation,
            "release": _RELEASE_IDENTITY,
            "execution": _EXECUTION_IDENTITY,
        }
    )
    if operation in {
        "restore-state",
        "cutover-restored-state",
        "verify-restored-state",
        "rollback-primary-state",
        "verify-primary-state",
    }:
        parameters = {
            "primaryTableArn": STATE_TABLE_ARN,
            "primaryTableName": STATE_TABLE_ARN.rsplit("/", 1)[-1],
            "restoredTableArn": RESTORED_TABLE_ARN,
            "restoredTableName": RESTORED_TABLE_ARN.rsplit("/", 1)[-1],
        }
    elif operation in {
        "deliver-security-events",
        "verify-outbox-drained",
        "force-dead-letter",
        "verify-dead-letter-alarm",
        "redrive-dead-letter",
        "verify-redelivery",
    }:
        parameters = {
            "tenantId": "tenant-launch",
            "projectId": "project-launch",
            "outboxQueueArn": OUTBOX_ARN,
            "deadLetterQueueArn": DLQ_ARN,
            "deadLetterAlarmArn": ALARM_ARN,
        }
    elif operation in {
        "exercise-routing-strategies",
        "verify-routing-decisions",
    }:
        parameters = {
            "tenantId": "tenant-launch",
            "projectId": "project-launch",
            "model": "launch-model",
            "strategies": list(worker.ROUTING_STRATEGIES),
            "candidateProviders": ["anthropic", "openai"],
        }
    elif operation in {
        "inject-primary-provider-fault",
        "verify-provider-fallback",
        "clear-primary-provider-fault",
        "verify-primary-provider-recovery",
    }:
        parameters = {
            "tenantId": "tenant-launch",
            "projectId": "project-launch",
            "model": "launch-model",
            "primaryProvider": "openai",
            "fallbackProvider": "anthropic",
            "failureStatusCode": 503,
            "faultTtlSeconds": 300,
        }
    elif operation in {
        "inject-control-plane-fault",
        "verify-control-plane-fail-closed",
        "clear-control-plane-fault",
        "verify-control-plane-recovery",
    }:
        parameters = {
            "tenantId": "tenant-launch",
            "projectId": "project-launch",
            "dependency": "dynamodb",
            "faultTtlSeconds": 300,
        }
    else:
        parameters = {
            "startupDeadlineSeconds": 60,
            "faultTtlSeconds": 300,
        }
    return {
        "schema": worker.ACTION_SCHEMA,
        "gate": gate,
        "operation": operation,
        "owner": {
            "id": OWNER,
            "repository": "AxonLLM/axonllm",
            "workflowCommit": WORKFLOW_COMMIT,
            "runId": "41",
            "runAttempt": "2",
            "expiresAt": _time_text(EXPIRES),
            "authorizationExpiresAtEpoch": str(int((EXPIRES + worker.OWNER_RETENTION).timestamp())),
        },
        "release": deepcopy(_RELEASE_IDENTITY),
        "execution": deepcopy(_EXECUTION_IDENTITY),
        "correlationId": correlation_id,
        "idempotencyKey": idempotency_key,
        "binding": _binding(),
        "parameters": parameters,
        "lease": {
            "Attributes": {
                "leaseKey": {"S": "production"},
                "ownerId": {"S": OWNER},
                "correlationId": {"S": correlation_id},
                "idempotencyKey": {"S": idempotency_key},
                "status": {"S": "ACTIVE"},
                "updatedAt": {"S": _time_text(NOW)},
                "fenceToken": {"N": str(fence)},
            }
        },
    }


def _cleanup_payload() -> dict[str, Any]:
    payload = _action_payload()
    payload["gate"] = "cleanup"
    payload["operation"] = "cleanup"
    payload["correlationId"] = hashlib.sha256(f"{OWNER}:cleanup:cleanup".encode()).hexdigest()[:32]
    payload["idempotencyKey"] = _producer_digest(
        {
            "ownerId": OWNER,
            "gate": "cleanup",
            "action": "cleanup",
            "release": _RELEASE_IDENTITY,
            "execution": _EXECUTION_IDENTITY,
        }
    )
    payload.pop("lease")
    payload["parameters"] = {
        "ownership": _ownership(),
        "primaryTableArn": STATE_TABLE_ARN,
        "primaryTableName": STATE_TABLE_ARN.rsplit("/", 1)[-1],
        "restoredTableArn": RESTORED_TABLE_ARN,
        "restoredTableName": RESTORED_TABLE_ARN.rsplit("/", 1)[-1],
        "outboxQueueArn": OUTBOX_ARN,
        "deadLetterQueueArn": DLQ_ARN,
    }
    return payload


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    operation = payload["operation"]
    evidence = (
        {
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
        }
        if operation == "cleanup"
        else {"timeoutExitCode": 124}
    )
    return {
        "schema": worker.RESULT_SCHEMA,
        "gate": payload["gate"],
        "operation": operation,
        "ownerId": OWNER,
        "correlationId": payload["correlationId"],
        "idempotencyKey": payload["idempotencyKey"],
        "status": "SUCCEEDED",
        "binding": deepcopy(payload["binding"]),
        "evidence": evidence,
        "ownership": _ownership(),
    }


def _raw(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _owner_state(revision: int = 0) -> worker.OwnerState:
    value: dict[str, Any] = {} if revision == 0 else {"step": revision}
    encoded = _raw(value)
    return worker.OwnerState(
        owner_id=OWNER,
        expires_at=EXPIRES,
        revision=revision,
        value=value,
        sha256=hashlib.sha256(encoded.encode()).hexdigest(),
    )


class FakeAws:
    def __init__(self, polls: list[dict[str, Any]] | None = None) -> None:
        self.polls = list(polls or [])
        self.calls: list[tuple[str, str, str, dict[str, Any], float]] = []
        self.fail_operation: str | None = None

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                service,
                operation,
                region,
                deepcopy(parameters),
                timeout_seconds,
            )
        )
        if operation == self.fail_operation:
            raise RuntimeError("Authorization: Bearer must-never-reach-task-failure")
        if operation == "get_activity_task":
            return self.polls.pop(0) if self.polls else {}
        return {}

    def operations(self) -> list[str]:
        return [call[1] for call in self.calls]

    def parameters(self, operation: str) -> list[dict[str, Any]]:
        return [call[3] for call in self.calls if call[1] == operation]


class FakeStateStore:
    def __init__(self) -> None:
        self.owner = _owner_state()
        self.replay: worker.ReplayRecord | None = None
        self.verify_count = 0
        self.renew_count = 0
        self.acquire_count = 0
        self.complete_count = 0
        self.commit_count = 0
        self.failed: list[str] = []
        self.failure_retryability: list[bool] = []
        self.fail_claim_error: Exception | None = None
        self.fence_failure_at: int | None = None
        self.on_renew: Any = None

    def verify_fence(self, _task: worker.ActionTask) -> None:
        self.verify_count += 1
        if self.fence_failure_at is not None and self.verify_count >= self.fence_failure_at:
            raise worker.FenceLostError

    def load_replay(self, _task: worker.ActionTask) -> worker.ReplayRecord | None:
        return self.replay

    def acquire_claim(self, _task: worker.ActionTask) -> None:
        self.acquire_count += 1

    def load_owner(self, _owner_id: str, _expires_at: datetime) -> worker.OwnerState:
        return self.owner

    def renew_claim(self, _task: worker.ActionTask) -> None:
        self.renew_count += 1
        if self.on_renew is not None:
            self.on_renew(self.renew_count)

    def complete_claim(
        self,
        *,
        task: worker.ActionTask,
        output_json: str,
        output_sha256: str,
        previous: worker.OwnerState,
        next_state_json: str,
        next_state_sha256: str,
    ) -> worker.ReplayRecord:
        self.complete_count += 1
        self.owner = worker.OwnerState(
            owner_id=previous.owner_id,
            expires_at=previous.expires_at,
            revision=previous.revision + 1,
            value=json.loads(next_state_json),
            sha256=next_state_sha256,
        )
        self.replay = worker.ReplayRecord(
            owner_id=task.owner_id,
            idempotency_key=task.idempotency_key,
            request_sha256=task.request_sha256,
            status="COMPLETE",
            worker_id="9" * 32,
            claim_expires_at_epoch=None,
            result_json=output_json,
            result_sha256=output_sha256,
            base_revision=previous.revision,
            next_revision=previous.revision + 1,
            base_state_sha256=previous.sha256,
            next_state_json=next_state_json,
            next_state_sha256=next_state_sha256,
            expires_at=task.expires_at,
        )
        return self.replay

    def commit_owner(
        self,
        *,
        previous: worker.OwnerState,
        next_state_json: str,
        next_state_sha256: str,
        next_revision: int,
    ) -> worker.OwnerState:
        self.commit_count += 1
        self.owner = worker.OwnerState(
            owner_id=previous.owner_id,
            expires_at=previous.expires_at,
            revision=next_revision,
            value=json.loads(next_state_json),
            sha256=next_state_sha256,
        )
        return self.owner

    def fail_claim(
        self,
        _task: worker.ActionTask,
        failure_code: str,
        *,
        retryable: bool = False,
    ) -> bool:
        if self.fail_claim_error is not None:
            raise self.fail_claim_error
        self.failed.append(failure_code)
        self.failure_retryability.append(retryable)
        return True

    def reconcile_replay(
        self,
        _task: worker.ActionTask,
        replay: worker.ReplayRecord,
    ) -> str:
        assert replay.result_json is not None
        return replay.result_json


class FakeHandler:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[str] = []
        self.failure: Exception | None = None
        self.block: Any = None

    def handle_action(
        self,
        _task: worker.ActionTask,
        context: worker.HandlerContext,
    ) -> worker.HandlerOutcome:
        self.calls.append("action")
        if self.block is not None:
            self.block(context)
        if self.failure is not None:
            raise self.failure
        return worker.HandlerOutcome(
            output=_result(self.payload),
            state={"lastAction": self.payload["operation"]},
        )

    def handle_cleanup(
        self,
        _task: worker.ActionTask,
        _context: worker.HandlerContext,
    ) -> worker.HandlerOutcome:
        self.calls.append("cleanup")
        if self.failure is not None:
            raise self.failure
        return worker.HandlerOutcome(
            output=_result(self.payload),
            state={},
        )

    def handle_cleanup_expired(
        self,
        _task: worker.MaintenanceTask,
        _context: worker.HandlerContext,
    ) -> dict[str, Any]:
        self.calls.append("cleanup-expired")
        return {
            "schema": "axonllm.launch-maintenance-result/v1",
            "operation": "cleanup-expired",
            "ownersCleaned": 0,
        }

    def handle_watchdog(
        self,
        _task: worker.MaintenanceTask,
        _context: worker.HandlerContext,
    ) -> dict[str, Any]:
        self.calls.append("watchdog")
        return {
            "schema": "axonllm.launch-maintenance-result/v1",
            "operation": "watchdog",
            "heartbeatPublished": True,
        }


def _runtime(
    payload: dict[str, Any],
    *,
    mode: str = worker.ACTION_MODE,
    aws: FakeAws | None = None,
    store: FakeStateStore | None = None,
    handler: FakeHandler | None = None,
) -> tuple[
    worker.LaunchActivityWorker,
    FakeAws,
    FakeStateStore,
    FakeHandler,
]:
    transport = aws or FakeAws([{"taskToken": "opaque-task-token", "input": _raw(payload)}])
    state_store = store or FakeStateStore()
    domain = handler or FakeHandler(payload)
    runtime = worker.LaunchActivityWorker(
        config=_config(mode),
        aws=transport,
        handler=domain,
        state_store=state_store,
        now=lambda: NOW,
    )
    return runtime, transport, state_store, domain


@pytest.mark.parametrize(
    ("mutation", "mode"),
    [
        (
            lambda values: values.update({"activity_arn": CLEANUP_ARN}),
            worker.ACTION_MODE,
        ),
        (
            lambda values: values.update({"region": "us-west-2"}),
            worker.ACTION_MODE,
        ),
        (
            lambda values: values.update(
                {"lease_table_arn": ("arn:aws:dynamodb:us-east-1:999999999999:table/axonllm-launch-rehearsal-leases")}
            ),
            worker.ACTION_MODE,
        ),
        (
            lambda values: values.update({"activity_arn": ACTION_ARN}),
            worker.CLEANUP_MODE,
        ),
    ],
)
def test_config_requires_exact_mode_activity_region_and_account(
    mutation,
    mode: str,
) -> None:
    values = {
        "mode": mode,
        "activity_arn": (ACTION_ARN if mode == worker.ACTION_MODE else CLEANUP_ARN),
        "region": REGION,
        "lease_table_arn": LEASE_TABLE_ARN,
        "worker_id": "9" * 32,
    }
    mutation(values)
    with pytest.raises(worker.ConfigurationError):
        worker.WorkerConfig(**values)


def test_boto_transport_splits_one_bounded_poll_budget_without_sdk_retries(
    monkeypatch,
) -> None:
    import boto3
    import botocore.config

    observed: dict[str, Any] = {}

    class Client:
        def get_activity_task(self, **parameters):
            observed["parameters"] = parameters
            return {}

    def config_factory(**values):
        observed["config"] = values
        return values

    def client_factory(service, **values):
        observed["service"] = service
        observed["client"] = values
        return Client()

    monkeypatch.setattr(botocore.config, "Config", config_factory)
    monkeypatch.setattr(boto3, "client", client_factory)
    transport = worker.BotoAwsTransport()
    assert (
        transport.call(
            "stepfunctions",
            "get_activity_task",
            region=REGION,
            parameters={"activityArn": ACTION_ARN},
            timeout_seconds=70,
        )
        == {}
    )
    assert observed["service"] == "stepfunctions"
    assert observed["client"]["region_name"] == REGION
    assert observed["config"]["connect_timeout"] == 5
    assert observed["config"]["read_timeout"] == 65
    assert observed["config"]["retries"] == {
        "total_max_attempts": 1,
        "mode": "standard",
    }


def test_valid_action_is_strictly_parsed_and_fence_is_not_request_identity() -> None:
    first = worker.parse_task_input(
        _raw(_action_payload(fence=11)),
        config=_config(),
        now=NOW,
    )
    second = worker.parse_task_input(
        _raw(_action_payload(fence=12)),
        config=_config(),
        now=NOW,
    )
    assert isinstance(first, worker.ActionTask)
    assert first.fence_token == 11
    assert second.fence_token == 12
    assert first.request_sha256 == second.request_sha256
    assert first.operation == "observe-exit-124"


@pytest.mark.parametrize("operation", sorted(worker.ACTION_OPERATIONS))
def test_all_29_coordinator_action_contracts_are_accepted(
    operation: str,
) -> None:
    parsed = worker.parse_task_input(
        _raw(_action_payload(operation=operation)),
        config=_config(),
        now=NOW,
    )
    assert isinstance(parsed, worker.ActionTask)
    assert parsed.operation == operation
    assert parsed.gate == worker.ACTION_TO_GATE[operation]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unsupported": True}),
        lambda value: value.update({"gate": "providerFallbackRecovery"}),
        lambda value: value["owner"].update({"id": "foreign"}),
        lambda value: value["lease"]["Attributes"].update({"ownerId": {"S": "0" * 64}}),
        lambda value: value["binding"].update(
            {"coordinatorLeaseTableArn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foreign")}
        ),
        lambda value: value["parameters"].update({"faultTtlSeconds": 0}),
        lambda value: value["execution"].update({"reviewedConfigVersionId": "null"}),
    ],
)
def test_action_schema_fails_closed_on_unknown_or_unbound_values(
    mutate,
) -> None:
    payload = _action_payload()
    mutate(payload)
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            _raw(payload),
            config=_config(),
            now=NOW,
        )


def test_action_identity_is_recomputed_from_protected_producer_fields() -> None:
    foreign_owner = "a" * 64
    owner_payload = _action_payload()
    owner_payload["owner"]["id"] = foreign_owner
    owner_payload["lease"]["Attributes"]["ownerId"] = {"S": foreign_owner}
    owner_payload["correlationId"] = hashlib.sha256(
        (f"{foreign_owner}:{owner_payload['gate']}:{owner_payload['operation']}").encode()
    ).hexdigest()[:32]
    owner_payload["lease"]["Attributes"]["correlationId"] = {"S": owner_payload["correlationId"]}
    owner_payload["idempotencyKey"] = _producer_digest(
        {
            "ownerId": foreign_owner,
            "gate": owner_payload["gate"],
            "action": owner_payload["operation"],
            "release": owner_payload["release"],
            "execution": owner_payload["execution"],
        }
    )
    owner_payload["lease"]["Attributes"]["idempotencyKey"] = {"S": owner_payload["idempotencyKey"]}

    correlation_payload = _action_payload()
    correlation_payload["correlationId"] = "b" * 32
    correlation_payload["lease"]["Attributes"]["correlationId"] = {"S": "b" * 32}

    idempotency_payload = _action_payload()
    idempotency_payload["idempotencyKey"] = "c" * 64
    idempotency_payload["lease"]["Attributes"]["idempotencyKey"] = {"S": "c" * 64}

    null_version_payload = _action_payload()
    null_version_payload["execution"]["reviewedConfigVersionId"] = "null"
    null_version_payload["binding"]["reviewedConfigVersionId"] = "null"

    for payload in (
        owner_payload,
        correlation_payload,
        idempotency_payload,
        null_version_payload,
    ):
        with pytest.raises(worker.TaskInputError):
            worker.parse_task_input(
                _raw(payload),
                config=_config(),
                now=NOW,
            )


def test_duplicate_json_fields_and_oversized_input_are_rejected() -> None:
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            '{"schema":"x","schema":"y"}',
            config=_config(),
            now=NOW,
        )
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            '{"padding":"' + "x" * worker.MAX_TASK_INPUT_BYTES + '"}',
            config=_config(),
            now=NOW,
        )


def test_expired_action_is_rejected_but_cleanup_has_bounded_retention() -> None:
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            _raw(_action_payload()),
            config=_config(),
            now=EXPIRES + timedelta(seconds=1),
        )
    cleanup = worker.parse_task_input(
        _raw(_cleanup_payload()),
        config=_config(worker.CLEANUP_MODE),
        now=EXPIRES + timedelta(days=6),
    )
    assert isinstance(cleanup, worker.ActionTask)
    assert cleanup.is_cleanup
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            _raw(_cleanup_payload()),
            config=_config(worker.CLEANUP_MODE),
            now=EXPIRES + timedelta(days=7, seconds=1),
        )


def test_fault_lease_cannot_outlive_review_and_heartbeat_stops_at_expiry() -> None:
    payload = _action_payload(operation="inject-primary-provider-fault")
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            _raw(payload),
            config=_config(),
            now=EXPIRES - timedelta(seconds=299),
        )

    task = worker.parse_task_input(
        _raw(_action_payload()),
        config=_config(),
        now=NOW,
    )
    assert isinstance(task, worker.ActionTask)
    runtime, aws, _store, _handler = _runtime(_action_payload())
    runtime.now = lambda: EXPIRES + timedelta(seconds=1)
    with pytest.raises(worker.ReviewExpiredError):
        runtime._heartbeat_once(
            "opaque-task-token",
            task,
            has_claim=False,
        )
    assert aws.parameters("send_task_heartbeat") == []


@pytest.mark.parametrize("operation", ["cleanup-expired", "watchdog"])
def test_maintenance_schema_is_exact_and_cleanup_only(
    operation: str,
) -> None:
    raw = _raw({"schema": worker.MAINTENANCE_SCHEMA, "operation": operation})
    parsed = worker.parse_task_input(
        raw,
        config=_config(worker.CLEANUP_MODE),
        now=NOW,
    )
    assert isinstance(parsed, worker.MaintenanceTask)
    assert parsed.operation == operation
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(raw, config=_config(), now=NOW)

    invalid = json.loads(raw)
    invalid["owner"] = OWNER
    with pytest.raises(worker.TaskInputError):
        worker.parse_task_input(
            _raw(invalid),
            config=_config(worker.CLEANUP_MODE),
            now=NOW,
        )

    continuation = {
        "schema": worker.MAINTENANCE_SCHEMA,
        "operation": "cleanup-expired",
        "cursor": {"leaseKey": {"S": f"owner#{OWNER}"}},
        "page": 1,
    }
    parsed_continuation = worker.parse_task_input(
        _raw(continuation),
        config=_config(worker.CLEANUP_MODE),
        now=NOW,
    )
    assert parsed_continuation.payload == continuation
    for key in ("cursor", "page"):
        incomplete = dict(continuation)
        incomplete.pop(key)
        with pytest.raises(worker.TaskInputError):
            worker.parse_task_input(
                _raw(incomplete),
                config=_config(worker.CLEANUP_MODE),
                now=NOW,
            )


def test_success_path_heartbeats_persists_then_sends_exact_output() -> None:
    payload = _action_payload()
    runtime, aws, store, handler = _runtime(payload)
    assert runtime.poll_once() == "succeeded"
    assert handler.calls == ["action"]
    assert store.acquire_count == 1
    assert store.complete_count == 1
    assert store.commit_count == 0
    assert store.owner.revision == 1
    assert store.owner.value == {"lastAction": payload["operation"]}
    assert store.verify_count >= 3
    assert aws.operations()[0] == "get_activity_task"
    assert aws.parameters("get_activity_task") == [
        {
            "activityArn": ACTION_ARN,
            "workerName": "axonllm-action-" + "9" * 16,
        }
    ]
    poll_call = aws.calls[0]
    assert poll_call[4] == 70.0
    assert aws.operations().index("send_task_heartbeat") < (aws.operations().index("send_task_success"))
    sent = aws.parameters("send_task_success")[0]
    assert sent["taskToken"] == "opaque-task-token"
    assert json.loads(sent["output"]) == _result(payload)
    assert not aws.parameters("send_task_failure")


def test_blocking_handler_gets_periodic_heartbeats_before_completion() -> None:
    payload = _action_payload()
    runtime, aws, store, handler = _runtime(payload)
    periodic = __import__("threading").Event()

    def on_renew(count: int) -> None:
        if count >= 2:
            periodic.set()

    store.on_renew = on_renew

    def block(context: worker.HandlerContext) -> None:
        assert context.fence_token == 11
        assert periodic.wait(timeout=1)
        assert not context.cancellation.is_set()

    handler.block = block
    assert runtime.poll_once() == "succeeded"
    assert store.renew_count >= 3
    assert len(aws.parameters("send_task_heartbeat")) >= 3


def test_fence_loss_prevents_domain_work_and_reports_bounded_failure() -> None:
    payload = _action_payload()
    store = FakeStateStore()
    store.fence_failure_at = 1
    runtime, aws, _store, handler = _runtime(payload, store=store)
    assert runtime.poll_once() == "failed"
    assert handler.calls == []
    failure = aws.parameters("send_task_failure")[0]
    assert failure["error"] == "LaunchFenceLost"
    assert len(failure["cause"].encode()) <= worker.MAX_FAILURE_CAUSE_BYTES
    assert json.loads(failure["cause"]) == {
        "schema": worker.FAILURE_SCHEMA,
        "code": "LaunchFenceLost",
        "retryable": False,
    }


def test_lost_fence_during_work_cancels_success_and_does_not_commit() -> None:
    payload = _action_payload()
    store = FakeStateStore()
    store.fence_failure_at = 2
    runtime, aws, _store, handler = _runtime(payload, store=store)

    def wait_for_loss(context: worker.HandlerContext) -> None:
        deadline = __import__("time").monotonic() + 1
        while not context.cancellation.is_set() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.005)
        assert context.cancellation.is_set()

    handler.block = wait_for_loss
    assert runtime.poll_once() == "failed"
    assert store.complete_count == 0
    assert store.commit_count == 0
    assert aws.parameters("send_task_failure")[0]["error"] == ("LaunchFenceLost")


def test_completed_operation_is_replayed_exactly_without_domain_work() -> None:
    payload = _action_payload()
    parsed = worker.parse_task_input(_raw(payload), config=_config(), now=NOW)
    assert isinstance(parsed, worker.ActionTask)
    output_json = _raw(_result(payload))
    state_json = _raw({"lastAction": payload["operation"]})
    store = FakeStateStore()
    store.replay = worker.ReplayRecord(
        owner_id=OWNER,
        idempotency_key=IDEMPOTENCY,
        request_sha256=parsed.request_sha256,
        status="COMPLETE",
        worker_id="a" * 32,
        claim_expires_at_epoch=None,
        result_json=output_json,
        result_sha256=hashlib.sha256(output_json.encode()).hexdigest(),
        base_revision=0,
        next_revision=1,
        base_state_sha256=store.owner.sha256,
        next_state_json=state_json,
        next_state_sha256=hashlib.sha256(state_json.encode()).hexdigest(),
        expires_at=EXPIRES,
    )
    runtime, aws, _store, handler = _runtime(payload, store=store)
    assert runtime.poll_once() == "replayed"
    assert handler.calls == []
    assert store.acquire_count == 0
    assert aws.parameters("send_task_success")[0]["output"] == output_json


def test_replay_winning_during_claim_acquisition_is_not_reexecuted() -> None:
    payload = _action_payload()
    parsed = worker.parse_task_input(_raw(payload), config=_config(), now=NOW)
    assert isinstance(parsed, worker.ActionTask)
    output_json = _raw(_result(payload))
    state_json = _raw({"lastAction": payload["operation"]})
    replay = worker.ReplayRecord(
        owner_id=OWNER,
        idempotency_key=parsed.idempotency_key,
        request_sha256=parsed.request_sha256,
        status="COMPLETE",
        worker_id="a" * 32,
        claim_expires_at_epoch=None,
        result_json=output_json,
        result_sha256=hashlib.sha256(output_json.encode()).hexdigest(),
        base_revision=0,
        next_revision=1,
        base_state_sha256=_owner_state().sha256,
        next_state_json=state_json,
        next_state_sha256=hashlib.sha256(state_json.encode()).hexdigest(),
        expires_at=EXPIRES,
    )
    store = FakeStateStore()
    store.acquire_claim = lambda _task: replay
    runtime, aws, _store, handler = _runtime(payload, store=store)
    assert runtime.poll_once() == "replayed"
    assert handler.calls == []
    assert store.complete_count == 0
    assert aws.parameters("send_task_success")[0]["output"] == output_json


def test_complete_replay_cannot_apply_owner_state_after_the_fact() -> None:
    task = worker.parse_task_input(
        _raw(_action_payload()),
        config=_config(),
        now=NOW,
    )
    assert isinstance(task, worker.ActionTask)
    base = _owner_state()
    output_json = _raw(_result(_action_payload()))
    next_state_json = _raw({"lastAction": task.operation})
    replay = worker.ReplayRecord(
        owner_id=task.owner_id,
        idempotency_key=task.idempotency_key,
        request_sha256=task.request_sha256,
        status="COMPLETE",
        worker_id="a" * 32,
        claim_expires_at_epoch=None,
        result_json=output_json,
        result_sha256=hashlib.sha256(output_json.encode()).hexdigest(),
        base_revision=base.revision,
        next_revision=base.revision + 1,
        base_state_sha256=base.sha256,
        next_state_json=next_state_json,
        next_state_sha256=hashlib.sha256(next_state_json.encode()).hexdigest(),
        expires_at=task.expires_at,
    )
    store = worker.DurableStateStore(
        config=_config(),
        aws=FakeAws(),
        now=lambda: NOW,
    )
    store.load_owner = lambda *_args: base
    store.commit_owner = lambda **_kwargs: pytest.fail("a COMPLETE replay must never mutate owner state")

    with pytest.raises(worker.ReplayConflictError):
        store.reconcile_replay(task, replay)


def test_handler_exception_never_leaks_exception_or_credentials() -> None:
    payload = _action_payload()
    handler = FakeHandler(payload)
    handler.failure = RuntimeError("password=hunter2 Authorization: Bearer abc")
    runtime, aws, store, _handler = _runtime(payload, handler=handler)
    assert runtime.poll_once() == "failed"
    assert store.failed == ["DomainHandlerFailed"]
    failure = aws.parameters("send_task_failure")[0]
    serialized = json.dumps(failure)
    assert failure["error"] == "DomainHandlerFailed"
    assert "hunter2" not in serialized
    assert "Bearer" not in serialized
    assert "RuntimeError" not in serialized


def test_claim_failure_error_does_not_suppress_task_failure_callback() -> None:
    payload = _action_payload()
    handler = FakeHandler(payload)
    handler.failure = RuntimeError("credential=must-never-reach-callback")
    store = FakeStateStore()
    store.fail_claim_error = worker.AwsTransportError(
        "dynamodb",
        "update_item",
        "ServiceUnavailable",
    )
    runtime, aws, _store, _handler = _runtime(
        payload,
        handler=handler,
        store=store,
    )

    assert runtime.poll_once() == "failed"
    failure = aws.parameters("send_task_failure")
    assert len(failure) == 1
    assert failure[0]["error"] == "DomainHandlerFailed"
    assert "credential" not in json.dumps(failure[0])


def test_handler_must_return_complete_real_evidence_not_placeholder() -> None:
    payload = _action_payload()
    handler = FakeHandler(payload)

    def incomplete(_task: worker.ActionTask, _context: worker.HandlerContext) -> worker.HandlerOutcome:
        value = _result(payload)
        value["evidence"] = {}
        return worker.HandlerOutcome(output=value, state={})

    handler.handle_action = incomplete
    runtime, aws, store, _handler = _runtime(payload, handler=handler)
    assert runtime.poll_once() == "failed"
    assert store.complete_count == 0
    assert aws.parameters("send_task_failure")[0]["error"] == ("InvalidHandlerResult")


def test_domain_declared_failure_is_safe_and_preserves_retryability() -> None:
    payload = _action_payload()
    handler = FakeHandler(payload)
    handler.failure = worker.DomainTaskFailure(
        "ProviderFixtureUnavailable",
        retryable=True,
    )
    runtime, aws, _store, _handler = _runtime(payload, handler=handler)
    assert runtime.poll_once() == "retry"
    assert _store.failed == ["ProviderFixtureUnavailable"]
    assert _store.failure_retryability == [True]
    assert aws.parameters("send_task_failure") == []
    retry = json.loads(aws.parameters("send_task_success")[0]["output"])
    assert retry == {
        "schema": worker.RETRY_SCHEMA,
        "status": "RETRY",
        "gate": payload["gate"],
        "operation": payload["operation"],
        "ownerId": OWNER,
        "correlationId": payload["correlationId"],
        "idempotencyKey": payload["idempotencyKey"],
        "code": "ProviderFixtureUnavailable",
        "retryable": True,
    }
    with pytest.raises(worker.HandlerContractError):
        worker.DomainTaskFailure("bad error with a credential")


def test_invalid_input_failure_does_not_echo_payload_or_task_token() -> None:
    secret = "never-echo-this-value"
    invalid = _action_payload()
    invalid["password"] = secret
    aws = FakeAws([{"taskToken": "opaque-token-secret", "input": _raw(invalid)}])
    runtime, aws, _store, handler = _runtime(_action_payload(), aws=aws)
    assert runtime.poll_once() == "failed"
    assert handler.calls == []
    failure = aws.parameters("send_task_failure")[0]
    assert failure["error"] == "InvalidTaskInput"
    assert secret not in failure["cause"]
    assert "opaque-token-secret" not in failure["cause"]


def test_missing_maintenance_operation_sends_failure_callback() -> None:
    payload = {"schema": worker.MAINTENANCE_SCHEMA}
    runtime, aws, _store, handler = _runtime(
        payload,
        mode=worker.CLEANUP_MODE,
    )

    assert runtime.poll_once() == "failed"
    assert handler.calls == []
    failure = aws.parameters("send_task_failure")
    assert len(failure) == 1
    assert failure[0]["error"] == "InvalidTaskInput"


def test_cleanup_action_dispatches_owner_cleanup_and_has_no_fence() -> None:
    payload = _cleanup_payload()
    runtime, aws, store, handler = _runtime(payload, mode=worker.CLEANUP_MODE)
    assert runtime.poll_once() == "succeeded"
    assert handler.calls == ["cleanup"]
    assert store.verify_count >= 1
    assert aws.parameters("send_task_success")


@pytest.mark.parametrize("operation", ["cleanup-expired", "watchdog"])
def test_maintenance_operations_dispatch_to_distinct_handler_methods(
    operation: str,
) -> None:
    payload = {
        "schema": worker.MAINTENANCE_SCHEMA,
        "operation": operation,
    }
    runtime, aws, _store, handler = _runtime(payload, mode=worker.CLEANUP_MODE)
    assert runtime.poll_once() == "succeeded"
    assert handler.calls == [operation]
    assert aws.parameters("send_task_heartbeat")
    output = json.loads(aws.parameters("send_task_success")[0]["output"])
    assert output["operation"] == operation


def test_shutdown_stops_new_polls_and_rejects_task_returned_during_drain() -> None:
    payload = _action_payload()
    runtime, aws, _store, handler = _runtime(payload)
    runtime.request_shutdown()
    assert runtime.poll_once() == "stopped"
    assert aws.calls == []
    assert handler.calls == []

    runtime, aws, _store, handler = _runtime(payload)
    original_poll = runtime._poll

    def poll_and_shutdown():
        response = original_poll()
        runtime.request_shutdown()
        return response

    runtime._poll = poll_and_shutdown
    assert runtime.poll_once() == "failed"
    assert handler.calls == []
    assert aws.parameters("send_task_failure")[0]["error"] == ("WorkerShuttingDown")


def test_run_forever_has_a_bounded_test_poll_count() -> None:
    runtime, aws, _store, _handler = _runtime(_action_payload(), aws=FakeAws([{}, {}, {}]))
    assert runtime.run_forever(max_polls=2) == 2
    assert aws.operations() == [
        "get_activity_task",
        "get_activity_task",
    ]


def test_fence_read_is_consistent_and_bound_to_production_row() -> None:
    task = worker.parse_task_input(
        _raw(_action_payload()),
        config=_config(),
        now=NOW,
    )
    assert isinstance(task, worker.ActionTask)

    class FenceAws(FakeAws):
        def call(self, service, operation, **kwargs):
            super().call(service, operation, **kwargs)
            return {
                "Item": {
                    "leaseKey": {"S": "production"},
                    "ownerId": {"S": OWNER},
                    "correlationId": {"S": CORRELATION},
                    "idempotencyKey": {"S": IDEMPOTENCY},
                    "status": {"S": "ACTIVE"},
                    "fenceToken": {"N": "11"},
                }
            }

    aws = FenceAws()
    store = worker.DurableStateStore(
        config=_config(),
        aws=aws,
        now=lambda: NOW,
    )
    store.verify_fence(task)
    parameters = aws.calls[0][3]
    assert parameters["TableName"] == LEASE_TABLE_ARN
    assert parameters["Key"] == {"leaseKey": {"S": "production"}}
    assert parameters["ConsistentRead"] is True

    task_with_new_fence = worker.parse_task_input(
        _raw(_action_payload(fence=12)),
        config=_config(),
        now=NOW,
    )
    assert isinstance(task_with_new_fence, worker.ActionTask)
    with pytest.raises(worker.FenceLostError):
        store.verify_fence(task_with_new_fence)


def test_completion_atomically_cas_updates_owner_and_replay() -> None:
    task = worker.parse_task_input(
        _raw(_action_payload()),
        config=_config(),
        now=NOW,
    )
    assert isinstance(task, worker.ActionTask)
    previous = _owner_state(revision=3)
    output_json = _raw(_result(_action_payload()))
    next_state_json = _raw({"step": 4})
    aws = FakeAws()
    store = worker.DurableStateStore(
        config=_config(),
        aws=aws,
        now=lambda: NOW,
    )

    replay = store.complete_claim(
        task=task,
        output_json=output_json,
        output_sha256=hashlib.sha256(output_json.encode()).hexdigest(),
        previous=previous,
        next_state_json=next_state_json,
        next_state_sha256=hashlib.sha256(next_state_json.encode()).hexdigest(),
    )

    request = aws.parameters("transact_write_items")[0]
    items = request["TransactItems"]
    assert len(items) == 3
    assert items[0]["ConditionCheck"]["Key"] == {"leaseKey": {"S": "production"}}
    owner_update = items[1]["Update"]
    replay_update = items[2]["Update"]
    assert owner_update["ConditionExpression"] == (
        "recordType = :recordType AND ownerId = :owner AND "
        "ownerExpiresAt = :ownerExpiry AND "
        "revision = :baseRevision AND stateSha256 = :baseSha"
    )
    assert replay_update["ConditionExpression"] == (
        "ownerId = :owner AND idempotencyKey = :idempotency AND "
        "requestSha256 = :request AND "
        "#status = :running AND workerId = :worker"
    )
    assert replay.status == "COMPLETE"
    assert replay.base_revision == 3
    assert replay.next_revision == 4


def test_completion_cas_failure_never_returns_complete_replay() -> None:
    task = worker.parse_task_input(
        _raw(_action_payload()),
        config=_config(),
        now=NOW,
    )
    assert isinstance(task, worker.ActionTask)
    output_json = _raw(_result(_action_payload()))
    next_state_json = _raw({"step": 2})
    aws = FakeAws()
    aws.fail_operation = "transact_write_items"
    store = worker.DurableStateStore(
        config=_config(),
        aws=aws,
        now=lambda: NOW,
    )

    with pytest.raises(worker.ReplayConflictError):
        store.complete_claim(
            task=task,
            output_json=output_json,
            output_sha256=hashlib.sha256(output_json.encode()).hexdigest(),
            previous=_owner_state(revision=1),
            next_state_json=next_state_json,
            next_state_sha256=hashlib.sha256(next_state_json.encode()).hexdigest(),
        )

    assert aws.operations() == ["transact_write_items"]


def test_expired_owner_scan_is_bounded_for_cleanup_domain_handler() -> None:
    state_json = _raw({"fixture": "owned"})
    state_sha = hashlib.sha256(state_json.encode()).hexdigest()

    class ScanAws(FakeAws):
        def call(self, service, operation, **kwargs):
            super().call(service, operation, **kwargs)
            return {
                "Items": [
                    {
                        "leaseKey": {"S": f"owner#{OWNER}"},
                        "recordType": {"S": "OWNER"},
                        "ownerId": {"S": OWNER},
                        "ownerExpiresAt": {"S": _time_text(EXPIRES)},
                        "ownerExpiresAtEpoch": {"N": str(int(EXPIRES.timestamp()))},
                        "revision": {"N": "4"},
                        "stateJson": {"S": state_json},
                        "stateSha256": {"S": state_sha},
                    }
                ],
                "LastEvaluatedKey": {"leaseKey": {"S": f"owner#{OWNER}"}},
            }

    aws = ScanAws()
    store = worker.DurableStateStore(
        config=_config(worker.CLEANUP_MODE),
        aws=aws,
        now=lambda: EXPIRES + timedelta(minutes=1),
    )
    page = store.list_expired_owners(limit=10)
    assert page.owners[0].value == {"fixture": "owned"}
    assert page.owners[0].revision == 4
    assert page.cursor == {"leaseKey": {"S": f"owner#{OWNER}"}}
    call = aws.calls[0]
    assert call[1] == "scan"
    assert call[3]["Limit"] == 10
    assert "ownerExpiresAtEpoch <= :now" in call[3]["FilterExpression"]
    with pytest.raises(worker.HandlerContractError):
        store.list_expired_owners(limit=101)


def test_expired_owner_index_query_preserves_full_cursor_across_pages() -> None:
    state_json = _raw({"fixture": "owned"})
    state_sha = hashlib.sha256(state_json.encode()).hexdigest()
    cursor = {
        "leaseKey": {"S": f"owner#{OWNER}"},
        "recordType": {"S": "OWNER"},
        "ownerExpiresAtEpoch": {"N": str(int(EXPIRES.timestamp()))},
    }

    class QueryAws(FakeAws):
        def call(self, service, operation, **kwargs):
            super().call(service, operation, **kwargs)
            if len(self.calls) == 1:
                return {
                    "Items": [
                        {
                            "leaseKey": {"S": f"owner#{OWNER}"},
                            "recordType": {"S": "OWNER"},
                            "ownerId": {"S": OWNER},
                            "ownerExpiresAt": {"S": _time_text(EXPIRES)},
                            "ownerExpiresAtEpoch": {"N": str(int(EXPIRES.timestamp()))},
                            "revision": {"N": "4"},
                            "stateJson": {"S": state_json},
                            "stateSha256": {"S": state_sha},
                        }
                    ],
                    "LastEvaluatedKey": cursor,
                }
            return {"Items": []}

    aws = QueryAws()
    store = worker.DurableStateStore(
        config=_config(
            worker.CLEANUP_MODE,
            owner_expiry_index_name="owner-expiry-index",
        ),
        aws=aws,
        now=lambda: EXPIRES + timedelta(minutes=1),
    )

    first = store.list_expired_owners(limit=10)
    second = store.list_expired_owners(limit=10, cursor=first.cursor)

    assert first.cursor == cursor
    assert second.cursor is None
    assert [call[1] for call in aws.calls] == ["query", "query"]
    first_request = aws.calls[0][3]
    assert first_request["IndexName"] == "owner-expiry-index"
    assert first_request["ConsistentRead"] is False
    assert first_request["KeyConditionExpression"] == ("recordType = :ownerType AND ownerExpiresAtEpoch <= :now")
    assert aws.calls[1][3]["ExclusiveStartKey"] == cursor


@pytest.mark.parametrize(
    "cursor",
    [
        {"leaseKey": {"S": f"owner#{OWNER}"}},
        {
            "leaseKey": {"S": f"owner#{OWNER}"},
            "recordType": {"S": "REPLAY"},
            "ownerExpiresAtEpoch": {"N": str(int(EXPIRES.timestamp()))},
        },
        {
            "leaseKey": {"S": f"owner#{OWNER}"},
            "recordType": {"S": "OWNER"},
            "ownerExpiresAtEpoch": {"S": "not-a-number"},
        },
    ],
)
def test_expired_owner_index_rejects_malformed_cursor(
    cursor: dict[str, Any],
) -> None:
    aws = FakeAws()
    store = worker.DurableStateStore(
        config=_config(
            worker.CLEANUP_MODE,
            owner_expiry_index_name="owner-expiry-index",
        ),
        aws=aws,
        now=lambda: EXPIRES + timedelta(minutes=1),
    )

    with pytest.raises(worker.HandlerContractError):
        store.list_expired_owners(cursor=cursor)

    assert aws.calls == []


@pytest.mark.parametrize("mutation", ["lease", "expiry"])
def test_expired_owner_listing_rejects_malformed_owner_identity(
    mutation: str,
) -> None:
    state_json = _raw({"fixture": "owned"})
    state_sha = hashlib.sha256(state_json.encode()).hexdigest()

    class InvalidExpiryAws(FakeAws):
        def call(self, service, operation, **kwargs):
            super().call(service, operation, **kwargs)
            lease_key = "owner#" + "a" * 64 if mutation == "lease" else f"owner#{OWNER}"
            expiry_epoch = int(EXPIRES.timestamp()) + (1 if mutation == "expiry" else 0)
            return {
                "Items": [
                    {
                        "leaseKey": {"S": lease_key},
                        "recordType": {"S": "OWNER"},
                        "ownerId": {"S": OWNER},
                        "ownerExpiresAt": {"S": _time_text(EXPIRES)},
                        "ownerExpiresAtEpoch": {"N": str(expiry_epoch)},
                        "revision": {"N": "4"},
                        "stateJson": {"S": state_json},
                        "stateSha256": {"S": state_sha},
                    }
                ]
            }

    store = worker.DurableStateStore(
        config=_config(worker.CLEANUP_MODE),
        aws=InvalidExpiryAws(),
        now=lambda: EXPIRES + timedelta(minutes=1),
    )

    with pytest.raises(worker.ReplayConflictError):
        store.list_expired_owners()


def test_default_handler_loader_uses_documented_factory_api(
    monkeypatch,
) -> None:
    payload = _action_payload()
    expected = FakeHandler(payload)
    module = ModuleType("launch_activity_domains")
    observed: dict[str, Any] = {}

    def create_handler(**kwargs):
        observed.update(kwargs)
        return expected

    module.create_handler = create_handler
    monkeypatch.setitem(sys.modules, "launch_activity_domains", module)
    aws = FakeAws()
    loaded = worker.load_handler(
        "launch_activity_domains",
        aws=aws,
        config=_config(),
    )
    assert loaded is expected
    assert observed == {
        "aws": aws,
        "region": REGION,
        "lease_table_arn": LEASE_TABLE_ARN,
    }


def test_sigterm_handler_only_requests_cooperative_shutdown(
    monkeypatch,
) -> None:
    runtime, _aws, _store, _handler = _runtime(_action_payload())
    installed: dict[int, Any] = {}

    monkeypatch.setattr(signal, "getsignal", lambda signum: f"old-{signum}")
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed.update({signum: handler}),
    )
    previous = worker._install_signal_handlers(runtime)
    assert previous == {
        signal.SIGTERM: f"old-{signal.SIGTERM}",
        signal.SIGINT: f"old-{signal.SIGINT}",
    }
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert runtime.shutdown_requested.is_set()
