"""Network-free end-to-end launch-domain contracts."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import launch_activity_domains as framework
import launch_activity_worker as worker
from launch_domains import control_plane, recovery, security


OWNER = "a" * 64
NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=1)
ACCOUNT = "123456789012"
REGION = "us-east-1"
PRIMARY_NAME = "axonllm-agentcore-state-managed"
RESTORED_NAME = f"{PRIMARY_NAME}-restore-validation-test"
PRIMARY = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{PRIMARY_NAME}"
RESTORED = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{RESTORED_NAME}"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/AxonLLM-abcdefghij"
ENDPOINT_ARN = f"{RUNTIME_ARN}/runtime-endpoint/production"
AGENT_STACK = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:"
    "stack/AxonLLMAgentCoreStack-managed/11111111-1111-1111-1111-111111111111"
)
CONTROL_STACK = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:"
    "stack/AxonLLMControlPlaneStack-managed/22222222-2222-2222-2222-222222222222"
)
BROKER_VERSION = "7"
BROKER_ARN = (
    f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:axonllm-qualification-selector-mutation-broker:{BROKER_VERSION}"
)
OUTBOX = f"arn:aws:sqs:{REGION}:{ACCOUNT}:axonllm-outbox.fifo"
DLQ = f"arn:aws:sqs:{REGION}:{ACCOUNT}:axonllm-dlq.fifo"
OUTBOX_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/axonllm-outbox.fifo"
DLQ_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/axonllm-dlq.fifo"
ALARM = f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:axonllm-dlq"
LOG_GROUP_NAME = "/aws/axonllm/security"
LOG_GROUP_ARN = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{LOG_GROUP_NAME}"


@pytest.fixture(autouse=True)
def _qualification_broker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AXON_QUALIFICATION_MUTATION_BROKER_VERSION_ARN",
        BROKER_ARN,
    )


def _ownership() -> dict[str, Any]:
    return {
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _parameters(operation: str) -> dict[str, Any]:
    if operation in recovery.OPERATIONS:
        return {
            "primaryTableArn": PRIMARY,
            "restoredTableArn": RESTORED,
        }
    if operation in security.OPERATIONS:
        return {
            "tenantId": "tenant",
            "projectId": "project",
            "outboxQueueArn": OUTBOX,
            "deadLetterQueueArn": DLQ,
            "deadLetterAlarmArn": ALARM,
        }
    return {
        "tenantId": "tenant",
        "projectId": "project",
        "dependency": "dynamodb",
        "faultTtlSeconds": 60,
    }


def _task(operation: str) -> worker.ActionTask:
    gate = worker.ACTION_TO_GATE[operation]
    return worker.ActionTask(
        payload={
            "release": {"commit": "1" * 40},
            "owner": {"expiresAt": EXPIRES.isoformat()},
            "binding": {
                "region": REGION,
                "tenantId": "tenant",
                "projectId": "project",
                "runtimeArn": RUNTIME_ARN,
                "runtimeEndpointArn": ENDPOINT_ARN,
                "agentcoreStackArn": AGENT_STACK,
                "controlPlaneStackArn": CONTROL_STACK,
                "outboxQueueUrl": OUTBOX_URL,
                "deadLetterQueueUrl": DLQ_URL,
                "securityEventLogGroupArn": LOG_GROUP_ARN,
            },
            "parameters": _parameters(operation),
        },
        gate=gate,
        operation=operation,
        owner_id=OWNER,
        correlation_id=hashlib.sha256(f"{OWNER}:{gate}:{operation}".encode()).hexdigest()[:32],
        idempotency_key=hashlib.sha256(f"{OWNER}:{operation}".encode()).hexdigest(),
        expires_at=EXPIRES,
        fence_token=7,
        request_sha256="d" * 64,
    )


def _context(aws: Any) -> worker.HandlerContext:
    return worker.HandlerContext(
        aws=aws,
        region=REGION,
        state_store=SimpleNamespace(),
        owner_state=None,
        cancellation=worker.CancellationToken(threading.Event()),
        fence_token=7,
    )


def _outputs(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"OutputKey": name, "OutputValue": value} for name, value in values.items()]


class RecoveryAws:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.restored_exists = False
        self.restored_pitr = False
        self.restored_ttl = False
        self.restored_protected = False
        self.agent_mode = "normal"
        self.agent_selected = PRIMARY_NAME
        self.agent_approval = ""
        self.control_mode = "normal"
        self.control_selected = PRIMARY_NAME
        self.control_approval = ""
        self.desired = 2
        self.running = 2
        self.minimum = 2
        self.maximum = 10
        self.suspended = {key: False for key in recovery._SUSPENSION_KEYS}

    def _stack(self, stack_arn: str) -> dict[str, Any]:
        parameters = [
            {"ParameterKey": name, "ParameterValue": ""}
            for name in (
                "RecoveryApprovalId",
                "RecoveryCutoverMode",
                "RuntimeStateTableName",
                "UnchangedParameter",
            )
        ]
        if stack_arn == AGENT_STACK:
            values = {
                "RecoveryApprovalId": self.agent_approval,
                "RecoveryCutoverMode": self.agent_mode,
                "RuntimeArn": RUNTIME_ARN,
                "SelectedRuntimeStateTableName": self.agent_selected,
                "StateTableName": PRIMARY_NAME,
            }
        elif stack_arn == CONTROL_STACK:
            values = {
                "AgentCoreStackName": "AxonLLMAgentCoreStack-managed",
                "ClusterName": "cluster-a",
                "PrimaryStateTableName": PRIMARY_NAME,
                "RecoveryApprovalId": self.control_approval,
                "RecoveryCutoverMode": self.control_mode,
                "SelectedRuntimeStateTableName": self.control_selected,
                "ServiceName": "service-a",
            }
        else:
            raise AssertionError(stack_arn)
        return {
            "Stacks": [
                {
                    "StackId": stack_arn,
                    "StackStatus": "UPDATE_COMPLETE",
                    "RoleARN": (f"arn:aws:iam::{ACCOUNT}:role/cfn-execution"),
                    "Parameters": parameters,
                    "Outputs": _outputs(values),
                }
            ]
        }

    def _table(self, name: str) -> dict[str, Any]:
        if name == RESTORED_NAME and not self.restored_exists:
            raise worker.AwsTransportError(
                "dynamodb",
                "describe_table",
                "ResourceNotFoundException",
            )
        arn = PRIMARY if name == PRIMARY_NAME else RESTORED
        return {
            "Table": {
                "TableName": name,
                "TableArn": arn,
                "TableStatus": "ACTIVE",
                "DeletionProtectionEnabled": (True if name == PRIMARY_NAME else self.restored_protected),
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                "SSEDescription": {
                    "Status": "ENABLED",
                },
            }
        }

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        assert region == REGION
        assert timeout_seconds == recovery.AWS_TIMEOUT_SECONDS
        self.calls.append((service, operation, deepcopy(parameters)))
        if (service, operation) == ("cloudformation", "describe_stacks"):
            return self._stack(parameters["StackName"])
        if (service, operation) == ("cloudformation", "update_stack"):
            raise AssertionError("worker must not mutate CloudFormation directly")
        if (service, operation) == ("lambda", "invoke"):
            assert parameters["FunctionName"] == BROKER_ARN
            assert parameters["InvocationType"] == "RequestResponse"
            event = json.loads(parameters["Payload"].decode("ascii"))
            stack_kind = event["stackKind"]
            legal_edge = event["legalEdge"]
            assert event == {
                "schema": "axonllm.qualification-selector-mutation",
                "version": 1,
                "authorizationId": (f"{OWNER}:7:{stack_kind}:{legal_edge}"),
                "ownerId": OWNER,
                "fenceToken": 7,
                "stackKind": stack_kind,
                "legalEdge": legal_edge,
            }
            target = PRIMARY_NAME if legal_edge.endswith("-primary") else RESTORED_NAME
            if stack_kind == "agentcore":
                if legal_edge.startswith("quiesce-"):
                    self.agent_mode = "quiesced"
                elif legal_edge.startswith("cutover-to-"):
                    self.agent_selected = target
                    self.agent_mode = "validation" if self.agent_mode == "selected" else "selected"
                else:
                    self.agent_mode = "normal"
                self.agent_approval = f"launch/{OWNER}"
            else:
                if legal_edge.startswith("quiesce-"):
                    self.control_mode = "quiesced"
                elif legal_edge.startswith("cutover-to-"):
                    self.control_selected = target
                    self.control_mode = "selected"
                else:
                    self.control_mode = "normal"
                self.control_approval = f"launch/{OWNER}"
            return {
                "StatusCode": 200,
                "ExecutedVersion": BROKER_VERSION,
                "Payload": b'{"status":"PENDING"}',
            }
        if (service, operation) == ("dynamodb", "describe_table"):
            return self._table(parameters["TableName"])
        if (
            service,
            operation,
        ) == ("dynamodb", "restore_table_to_point_in_time"):
            assert parameters == {
                "SourceTableArn": PRIMARY,
                "TargetTableName": RESTORED_NAME,
                "UseLatestRestorableTime": True,
            }
            self.restored_exists = True
            return {"TableDescription": self._table(RESTORED_NAME)["Table"]}
        if (service, operation) == (
            "dynamodb",
            "describe_continuous_backups",
        ):
            return {
                "ContinuousBackupsDescription": {
                    "PointInTimeRecoveryDescription": {
                        "PointInTimeRecoveryStatus": ("ENABLED" if self.restored_pitr else "DISABLED")
                    }
                }
            }
        if (service, operation) == (
            "dynamodb",
            "update_continuous_backups",
        ):
            self.restored_pitr = True
            return {}
        if (service, operation) == (
            "dynamodb",
            "describe_time_to_live",
        ):
            return {
                "TimeToLiveDescription": {
                    "TimeToLiveStatus": ("ENABLED" if self.restored_ttl else "DISABLED"),
                    "AttributeName": ("expires_at" if self.restored_ttl else None),
                }
            }
        if (service, operation) == (
            "dynamodb",
            "update_time_to_live",
        ):
            self.restored_ttl = True
            return {}
        if (service, operation) == ("dynamodb", "update_table"):
            self.restored_protected = parameters["DeletionProtectionEnabled"]
            return {}
        if (service, operation) == ("dynamodb", "delete_table"):
            assert parameters["TableName"] == RESTORED_NAME
            self.restored_exists = False
            return {}
        if (service, operation) == ("ecs", "describe_services"):
            return {
                "services": [
                    {
                        "desiredCount": self.desired,
                        "pendingCount": 0,
                        "runningCount": self.running,
                        "deployments": [
                            {
                                "status": "PRIMARY",
                                "runningCount": self.running,
                                "pendingCount": 0,
                                "rolloutState": "COMPLETED",
                            }
                        ],
                    }
                ],
                "failures": [],
            }
        if (service, operation) == ("ecs", "update_service"):
            self.desired = parameters["desiredCount"]
            self.running = self.desired
            return {}
        if (service, operation) == (
            "application-autoscaling",
            "describe_scalable_targets",
        ):
            return {
                "ScalableTargets": [
                    {
                        "MinCapacity": self.minimum,
                        "MaxCapacity": self.maximum,
                        "SuspendedState": dict(self.suspended),
                    }
                ]
            }
        if (service, operation) == (
            "application-autoscaling",
            "register_scalable_target",
        ):
            self.minimum = parameters["MinCapacity"]
            self.maximum = parameters["MaxCapacity"]
            self.suspended = dict(parameters["SuspendedState"])
            return {}
        if (service, operation) == (
            "bedrock-agentcore-control",
            "get_agent_runtime_endpoint",
        ):
            return {
                "status": "READY",
                "liveVersion": "7",
                "targetVersion": "7",
            }
        raise AssertionError((service, operation, parameters))


def _run_until_success(
    domain: Any,
    operation: str,
    aws: Any,
    state: dict[str, Any],
    ownership: dict[str, Any],
) -> framework.DomainActionResult:
    task = _task(operation)
    for _ in range(40):
        try:
            return domain.handle_action(
                operation=operation,
                task=task,
                context=_context(aws),
                state=state,
                ownership=ownership,
            )
        except worker.DomainTaskFailure as exc:
            assert exc.retryable is True
    raise AssertionError(f"{operation} did not converge")


def test_recovery_all_operations_and_owned_cleanup() -> None:
    aws = RecoveryAws()
    domain = recovery.RecoveryDomain()
    state: dict[str, Any] = {}
    ownership = _ownership()
    expected = {
        "restore-state": {
            "primaryTableArn": PRIMARY,
            "restoredTableArn": RESTORED,
        },
        "cutover-restored-state": {
            "cutoverPhases": [
                "quiesced",
                "selected",
                "validation",
                "normal",
            ],
            "cutoverSelectedTableArn": RESTORED,
        },
        "verify-restored-state": {},
        "rollback-primary-state": {
            "rollbackPhases": [
                "quiesced",
                "selected",
                "validation",
                "normal",
            ],
            "rollbackSelectedTableArn": PRIMARY,
        },
        "verify-primary-state": {
            "finalSelectedTableArn": PRIMARY,
            "productionEndpointStatusAfter": "READY",
            "controlPlaneDesiredCountAfter": 2,
            "controlPlaneRunningCountAfter": 2,
        },
    }
    for operation in recovery.OPERATIONS:
        result = _run_until_success(
            domain,
            operation,
            aws,
            state,
            ownership,
        )
        assert result.evidence == expected[operation]
        state = dict(result.state)
        ownership = dict(result.ownership)
    assert state["completed"] == list(recovery.OPERATIONS)
    assert aws.agent_mode == aws.control_mode == "normal"
    assert aws.agent_selected == aws.control_selected == PRIMARY_NAME
    assert ownership["fixtureIds"] == [f"{OWNER}:restored-state-table"]

    owner = framework.OwnerBinding(
        owner_id=OWNER,
        expires_at=EXPIRES,
        expires_at_text=EXPIRES.isoformat(),
    )
    for _ in range(10):
        try:
            cleanup = domain.cleanup(
                owner=owner,
                context=_context(aws),
                state=state,
                ownership=ownership,
            )
            break
        except worker.DomainTaskFailure as exc:
            assert exc.retryable is True
    else:
        raise AssertionError("recovery cleanup did not converge")
    assert cleanup.verified_complete is True
    assert cleanup.primary_state_selected is True
    assert cleanup.production_endpoint_status == "READY"
    assert cleanup.cleared_fixture_ids == [f"{OWNER}:restored-state-table"]
    assert aws.restored_exists is False
    assert aws.agent_selected == aws.control_selected == PRIMARY_NAME


class SecurityAws:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.queues: dict[str, list[dict[str, Any]]] = {
            OUTBOX_URL: [],
            DLQ_URL: [],
        }
        self.logs: list[dict[str, Any]] = []
        self.stream_exists = False
        self.visibility_resets: list[str] = []
        self.delayed_messages: dict[str, int] = {
            OUTBOX_URL: 0,
            DLQ_URL: 0,
        }
        self.not_visible_messages: dict[str, int] = {
            OUTBOX_URL: 0,
            DLQ_URL: 0,
        }
        self.next_message = 1

    def _deliver(self, body: str) -> None:
        envelope = json.loads(body)
        self.logs.append(
            {
                "eventId": f"log-{len(self.logs) + 1}",
                "message": json.dumps(envelope["event"]),
            }
        )

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        assert region == REGION
        assert timeout_seconds == security.AWS_TIMEOUT_SECONDS
        self.calls.append((service, operation, deepcopy(parameters)))
        if (service, operation) == ("sqs", "get_queue_attributes"):
            url = parameters["QueueUrl"]
            arn = OUTBOX if url == OUTBOX_URL else DLQ
            attributes = {"QueueArn": arn}
            if "ApproximateNumberOfMessages" in parameters["AttributeNames"]:
                attributes.update(
                    {
                        "ApproximateNumberOfMessages": str(len(self.queues[url])),
                        "ApproximateNumberOfMessagesDelayed": str(self.delayed_messages[url]),
                        "ApproximateNumberOfMessagesNotVisible": str(self.not_visible_messages[url]),
                    }
                )
            return {"Attributes": attributes}
        if (service, operation) == ("logs", "create_log_stream"):
            if self.stream_exists:
                raise worker.AwsTransportError(
                    "logs",
                    operation,
                    "ResourceAlreadyExistsException",
                )
            self.stream_exists = True
            return {}
        if (service, operation) == ("logs", "filter_log_events"):
            return {"events": deepcopy(self.logs)}
        if (service, operation) == ("logs", "delete_log_stream"):
            if not self.stream_exists:
                raise worker.AwsTransportError(
                    "logs",
                    operation,
                    "ResourceNotFoundException",
                )
            self.stream_exists = False
            self.logs = []
            return {}
        if (service, operation) == ("sqs", "send_message"):
            body = parameters["MessageBody"]
            if parameters["QueueUrl"] == OUTBOX_URL:
                self._deliver(body)
            else:
                receipt = f"receipt-{self.next_message}"
                self.next_message += 1
                self.queues[DLQ_URL].append(
                    {
                        "MessageId": f"message-{self.next_message}",
                        "ReceiptHandle": receipt,
                        "Body": body,
                    }
                )
            return {"MessageId": f"sent-{self.next_message}"}
        if (service, operation) == ("sqs", "receive_message"):
            return {"Messages": deepcopy(self.queues[parameters["QueueUrl"]][:10])}
        if (service, operation) == (
            "sqs",
            "change_message_visibility",
        ):
            self.visibility_resets.append(parameters["ReceiptHandle"])
            return {}
        if (service, operation) == ("sqs", "delete_message"):
            queue = self.queues[parameters["QueueUrl"]]
            queue[:] = [item for item in queue if item["ReceiptHandle"] != parameters["ReceiptHandle"]]
            return {}
        if (service, operation) == ("cloudwatch", "describe_alarms"):
            return {"MetricAlarms": [{"AlarmArn": ALARM, "StateValue": "ALARM"}]}
        raise AssertionError((service, operation, parameters))


def test_security_all_operations_redrive_and_owned_cleanup() -> None:
    aws = SecurityAws()
    domain = security.SecurityDomain()
    state: dict[str, Any] = {}
    ownership = _ownership()
    expected = {
        "deliver-security-events": {
            "configuredDestinationCount": 1,
            "deliveredDestinationCount": 1,
        },
        "verify-outbox-drained": {"outboxMessagesAfterDelivery": 0},
        "force-dead-letter": {"dlqMessagesAfterFailure": 1},
        "verify-dead-letter-alarm": {"dlqAlarmState": "ALARM"},
        "redrive-dead-letter": {"redrivenMessageCount": 1},
        "verify-redelivery": {
            "dlqMessagesAfterRedrive": 0,
            "outboxMessagesAfterRedrive": 0,
        },
    }
    for operation in security.OPERATIONS:
        result = _run_until_success(
            domain,
            operation,
            aws,
            state,
            ownership,
        )
        assert result.evidence == expected[operation]
        state = dict(result.state)
        ownership = dict(result.ownership)
    assert state["completed"] == list(security.OPERATIONS)
    assert ownership["fixtureIds"] == [f"{OWNER}:security-event-stream"]
    assert ownership["dlqCorrelationIds"] == []
    assert aws.queues[DLQ_URL] == []
    assert "foreign-receipt" not in aws.visibility_resets

    aws.queues[DLQ_URL].append(
        {
            "MessageId": "foreign",
            "ReceiptHandle": "foreign-receipt",
            "Body": '{"foreign":true}',
        }
    )

    cleanup = domain.cleanup(
        owner=framework.OwnerBinding(
            owner_id=OWNER,
            expires_at=EXPIRES,
            expires_at_text=EXPIRES.isoformat(),
        ),
        context=_context(aws),
        state=state,
        ownership=ownership,
    )
    assert cleanup.verified_complete is True
    assert cleanup.cleared_fixture_ids == [f"{OWNER}:security-event-stream"]
    assert cleanup.removed_dlq_correlation_ids == []
    assert aws.stream_exists is False
    assert aws.queues[DLQ_URL][0]["MessageId"] == "foreign"
    assert "foreign-receipt" not in aws.visibility_resets


@pytest.mark.parametrize(
    ("attribute", "url"),
    [
        ("delayed_messages", OUTBOX_URL),
        ("not_visible_messages", DLQ_URL),
    ],
)
def test_security_absence_is_not_certified_with_invisible_queue_state(
    attribute: str,
    url: str,
) -> None:
    aws = SecurityAws()
    foreign = {
        "MessageId": "foreign",
        "ReceiptHandle": "foreign-receipt",
        "Body": '{"foreign":true}',
    }
    aws.queues[url].append(foreign)
    getattr(aws, attribute)[url] = 1
    arn = OUTBOX if url == OUTBOX_URL else DLQ

    with pytest.raises(
        worker.DomainTaskFailure,
        match="SecurityOwnedMessageVisibilityPending",
    ) as raised:
        security._require_owned_absent(
            _context(aws),
            url=url,
            arn=arn,
            expected_bodies=['{"owned":true}'],
            failure_code="SecurityCleanupPending",
        )

    assert raised.value.retryable is True
    assert aws.queues[url] == [foreign]
    assert aws.visibility_resets == []
    assert not any(operation == "delete_message" for _service, operation, _parameters in aws.calls)


class FakeSession:
    revision = 10
    statuses: list[int] = []

    def __init__(self, *, task, context, **_kwargs):
        del context
        self.task = task
        self.binding = SimpleNamespace(
            fence_token=task.fence_token,
            correlation_id=hashlib.sha256(f"{task.owner_id}:{task.gate}:runtime-control".encode()).hexdigest()[:32],
        )
        self.controls: list[bool] = []

    def write_control(self, **kwargs):
        self.controls.append(kwargs["active"])
        self.revision += 1
        return self.revision

    def invoke(self, *_args, **_kwargs):
        return SimpleNamespace(
            status_code=self.statuses.pop(0),
            transport_error=False,
        )

    def observations(self, *_kinds):
        outcome = "unavailable" if self.task.operation.endswith("fail-closed") else "available"
        status = 503 if outcome == "unavailable" else 200
        count = 3 if outcome == "unavailable" else 2
        return tuple(
            SimpleNamespace(
                kind="dependency-call",
                payload={
                    "dependency": "dynamodb",
                    "outcome": outcome,
                    "request_id": self.binding.correlation_id,
                    "status_code": status,
                },
            )
            for _ in range(count)
        )


def test_control_plane_exact_order_fence_and_probe_statuses(
    monkeypatch,
) -> None:
    monkeypatch.setattr(control_plane.common, "LaunchSession", FakeSession)
    domain = control_plane.ControlPlaneDomain()
    state: dict[str, Any] = {}
    ownership = _ownership()
    expected = [
        ("inject-control-plane-fault", {"faultedDependency": "dynamodb"}),
        (
            "verify-control-plane-fail-closed",
            {
                "readyDuringFaultStatusCode": 503,
                "readDuringFaultStatusCode": 503,
                "mutationDuringFaultStatusCode": 503,
            },
        ),
        ("clear-control-plane-fault", {}),
        (
            "verify-control-plane-recovery",
            {
                "readyAfterRecoveryStatusCode": 200,
                "readAfterRecoveryStatusCode": 200,
            },
        ),
    ]
    FakeSession.statuses = [503, 503, 503, 200, 200]
    for operation, evidence in expected:
        result = domain.handle_action(
            operation=operation,
            task=_task(operation),
            context=_context(SimpleNamespace()),
            state=state,
            ownership=ownership,
        )
        assert result.evidence == evidence
        state = dict(result.state)
        ownership = dict(result.ownership)
    assert state["completed"] == list(control_plane.OPERATIONS)
    assert state["fenceToken"] == 7
    assert ownership["faultIds"] == []


def test_control_plane_cleanup_reuses_fence_and_clears_owned_fault(
    monkeypatch,
) -> None:
    monkeypatch.setattr(control_plane.common, "LaunchSession", FakeSession)
    domain = control_plane.ControlPlaneDomain()
    injected = domain.handle_action(
        operation="inject-control-plane-fault",
        task=_task("inject-control-plane-fault"),
        context=_context(SimpleNamespace()),
        state={},
        ownership=_ownership(),
    )
    result = domain.cleanup(
        owner=framework.OwnerBinding(
            owner_id=OWNER,
            expires_at=EXPIRES,
            expires_at_text=EXPIRES.isoformat(),
        ),
        context=_context(SimpleNamespace()),
        state=injected.state,
        ownership=injected.ownership,
    )
    assert result.verified_complete is True
    assert result.cleared_fault_ids == [f"{OWNER}:control-plane-dependency-unavailable"]
    assert result.ownership["faultIds"] == []
    assert result.state["faultActive"] is False
