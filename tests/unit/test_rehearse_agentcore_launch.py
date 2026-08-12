from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))
sys.path.insert(0, str(ROOT / "scripts" / "release"))

import launch_rehearsal_evidence as evidence  # noqa: E402
import rehearse_agentcore_launch as rehearsal  # noqa: E402


NOW = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
REGION = "us-east-1"
REPOSITORY = "owner/repo"
RELEASE_COMMIT = "1" * 40
WORKFLOW_COMMIT = "2" * 40
WORKFLOW_REF = f"{REPOSITORY}/.github/workflows/agentcore-launch-gates.yml@refs/heads/main"
AGENTCORE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/agentcore@sha256:{'a' * 64}"
CONTROL_PLANE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/fargate@sha256:{'b' * 64}"
REVIEWED_CONFIG_URI = f"s3://axonllm-launch-config-{ACCOUNT}/reviewed/rehearsal.json"
REVIEWED_CONFIG_VERSION_ID = "version-41"
RUNTIME_ID = "AxonLLMRuntime-ABCDEFGHIJ"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
ENDPOINT_ARN = f"{RUNTIME_ARN}/runtime-endpoint/production"
AGENTCORE_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/AxonLLMAgentCoreStack/11111111-1111-1111-1111-111111111111"
)
CONTROL_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/AxonLLMControlPlaneStack/22222222-2222-2222-2222-222222222222"
)
STATE_TABLE = "axonllm-agentcore-state"
RESTORED_TABLE = f"{STATE_TABLE}-restore-validation-20260812-abcd"
STATE_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STATE_TABLE}"
RESTORED_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{RESTORED_TABLE}"
OUTBOX_NAME = "axonllm-events.fifo"
DLQ_NAME = "axonllm-events-dlq.fifo"
OUTBOX_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{OUTBOX_NAME}"
DLQ_ARN = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{DLQ_NAME}"
OUTBOX_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{OUTBOX_NAME}"
DLQ_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{DLQ_NAME}"
ALARM_ARN = f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:axonllm-security-event-dlq"
SECURITY_LOG_GROUP_ARN = (
    f"arn:aws:logs:{REGION}:{ACCOUNT}:"
    "log-group:/aws/axonllm/security-events"
)
STATE_MACHINE_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:AxonLLMLaunchCoordinator:7"
EXECUTION_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AxonLLMLaunchCoordinatorExecutionRole"
LAUNCH_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/AxonLLMLaunchGatesRole"
LEASE_TABLE = "axonllm-launch-rehearsal-leases"
LEASE_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{LEASE_TABLE}"
WATCHDOG_ALARM_ARN = f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:axonllm-launch-rehearsal-watchdog"
KMS_KEY_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
CLUSTER_NAME = "axonllm-production"
SERVICE_NAME = "axonllm-control-plane"
TASK_DEFINITION_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/axonllm-control-plane:12"
TASK_ARNS = [
    f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER_NAME}/11111111111111111111111111111111",
    f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER_NAME}/22222222222222222222222222222222",
]


def _config_value(now: datetime = NOW) -> dict[str, Any]:
    return {
        "schema": rehearsal.CONFIG_SCHEMA,
        "accountId": ACCOUNT,
        "review": {
            "reviewId": "launch-review-41",
            "reviewer": "release-admin",
            "reviewedAt": (now - timedelta(minutes=5)).isoformat(),
            "expiresAt": (now + timedelta(hours=4)).isoformat(),
        },
        "limits": {
            "requestTimeoutSeconds": 5,
            "operationTimeoutSeconds": 30,
            "pollIntervalSeconds": 0.05,
            "maxAttempts": 3,
            "maxResponseBytes": 128 * 1024,
        },
        "coordinator": {
            "stateMachineVersionArn": STATE_MACHINE_ARN,
            "executionRoleArn": EXECUTION_ROLE_ARN,
            "launchRoleArn": LAUNCH_ROLE_ARN,
            "leaseTableArn": LEASE_TABLE_ARN,
            "watchdogAlarmArn": WATCHDOG_ALARM_ARN,
            "kmsKeyArn": KMS_KEY_ARN,
            "cleanupDeadlineSeconds": 900,
        },
        "resources": {
            "runtimeArn": RUNTIME_ARN,
            "runtimeEndpointArn": ENDPOINT_ARN,
            "runtimeEndpointName": "production",
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
        },
        "scenario": {
            "tenantId": "tenant-launch",
            "projectId": "project-launch",
            "datasourceId": "launch-datasource",
            "selectSql": "SELECT value FROM launch_canary",
            "model": "launch-model",
            "primaryProvider": "openai",
            "fallbackProvider": "anthropic",
            "controlPlaneFault": "dynamodb",
            "startupDeadlineSeconds": 60,
            "maxRows": 100,
            "scanLimitBytes": 1024 * 1024,
            "faultTtlSeconds": 300,
        },
    }


def _parse_config(
    value: dict[str, Any] | None = None,
    *,
    digest: str = "c" * 64,
    now: datetime = NOW,
) -> rehearsal.OperationConfig:
    return rehearsal.parse_config(
        _config_value(now) if value is None else value,
        region=REGION,
        now=now,
        sha256=digest,
    )


def _binding(
    config: rehearsal.OperationConfig,
    **overrides: str,
) -> rehearsal.ReleaseBinding:
    values = {
        "release_commit": RELEASE_COMMIT,
        "agentcore_image": AGENTCORE_IMAGE,
        "control_plane_image": CONTROL_PLANE_IMAGE,
        "region": REGION,
        "repository": REPOSITORY,
        "workflow_ref": WORKFLOW_REF,
        "workflow_commit": WORKFLOW_COMMIT,
        "run_id": "41",
        "run_attempt": "1",
        "account_id": config.account_id,
        "config_sha256": config.sha256,
        "reviewed_config_uri": REVIEWED_CONFIG_URI,
        "reviewed_config_version_id": REVIEWED_CONFIG_VERSION_ID,
        "reviewed_config_sha256": config.sha256,
    }
    values.update(overrides)
    return rehearsal.build_release_binding(**values)


def _all_observations() -> dict[str, dict[str, Any]]:
    return {
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
            "primaryTableArn": STATE_TABLE_ARN,
            "restoredTableArn": RESTORED_TABLE_ARN,
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
            "cutoverSelectedTableArn": RESTORED_TABLE_ARN,
            "rollbackSelectedTableArn": STATE_TABLE_ARN,
            "finalSelectedTableArn": STATE_TABLE_ARN,
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
            "strategiesExercised": list(rehearsal.ROUTING_STRATEGIES),
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


class FakeAws:
    def __init__(
        self,
        config: rehearsal.OperationConfig,
        binding: rehearsal.ReleaseBinding,
        *,
        account: str = ACCOUNT,
        primary: bool = True,
    ) -> None:
        self.config = config
        self.binding = binding
        self.account = account
        self.primary = primary
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    @staticmethod
    def _outputs(values: Mapping[str, str]) -> list[dict[str, str]]:
        return [{"OutputKey": key, "OutputValue": value} for key, value in values.items()]

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        assert region == REGION
        assert 0 < timeout_seconds <= 60
        params = dict(parameters)
        self.calls.append((service, operation, params))
        resources = self.config.resources
        coordinator = self.config.coordinator
        if (service, operation) == ("sts", "get_caller_identity"):
            session_name = "AxonLLMLaunchGates-41-1"
            role_name = coordinator.launch_role_arn.rsplit("/", 1)[-1]
            return {
                "Account": self.account,
                "Arn": (f"arn:aws:sts::{self.account}:assumed-role/{role_name}/{session_name}"),
                "UserId": f"AROATEST:{session_name}",
            }
        if (service, operation) == (
            "cloudformation",
            "describe_stacks",
        ):
            stack = params["StackName"]
            if stack == resources.agentcore_stack_arn:
                selected = resources.state_table_name if self.primary else resources.restored_state_table_name
                return {
                    "Stacks": [
                        {
                            "StackId": stack,
                            "Outputs": self._outputs(
                                {
                                    "RuntimeArn": resources.runtime_arn,
                                    "RuntimeEndpointName": (resources.runtime_endpoint_name),
                                    "StateTableName": (resources.state_table_name),
                                    "SelectedRuntimeStateTableName": selected,
                                    "RecoveryCutoverMode": ("normal" if self.primary else "validation"),
                                    "SecurityEventOutboxQueueArn": (resources.outbox_queue_arn),
                                    "SecurityEventOutboxQueueUrl": (resources.outbox_queue_url),
                                    "SecurityEventDeadLetterQueueUrl": (resources.dead_letter_queue_url),
                                    "SecurityEventLogGroupArn": (
                                        resources.security_event_log_group_arn
                                    ),
                                    "RuntimeImageUri": (self.binding.release["agentcoreImage"]),
                                }
                            ),
                        }
                    ]
                }
            if stack == resources.control_plane_stack_arn:
                selected = resources.state_table_name if self.primary else resources.restored_state_table_name
                return {
                    "Stacks": [
                        {
                            "StackId": stack,
                            "Outputs": self._outputs(
                                {
                                    "AgentCoreStackName": (resources.agentcore_stack_name),
                                    "PrimaryStateTableName": (resources.state_table_name),
                                    "SelectedRuntimeStateTableName": selected,
                                    "RecoveryCutoverMode": ("normal" if self.primary else "validation"),
                                    "ControlPlaneImageUri": (self.binding.release["controlPlaneImage"]),
                                    "ClusterName": CLUSTER_NAME,
                                    "ServiceName": SERVICE_NAME,
                                    "TaskDefinitionArn": TASK_DEFINITION_ARN,
                                }
                            ),
                        }
                    ]
                }
            raise AssertionError(f"foreign stack request: {stack}")
        if (service, operation) == ("dynamodb", "describe_table"):
            if params["TableName"] == coordinator.lease_table_name:
                return {
                    "Table": {
                        "TableArn": coordinator.lease_table_arn,
                        "TableStatus": "ACTIVE",
                        "DeletionProtectionEnabled": True,
                        "SSEDescription": {
                            "Status": "ENABLED",
                            "SSEType": "KMS",
                            "KMSMasterKeyArn": coordinator.kms_key_arn,
                        },
                    }
                }
            assert params["TableName"] == resources.state_table_name
            return {
                "Table": {
                    "TableArn": resources.state_table_arn,
                    "TableStatus": "ACTIVE",
                    "DeletionProtectionEnabled": True,
                }
            }
        if (service, operation) == ("dynamodb", "describe_time_to_live"):
            assert params["TableName"] == coordinator.lease_table_name
            return {
                "TimeToLiveDescription": {
                    "TimeToLiveStatus": "ENABLED",
                    "AttributeName": "expiresAtEpoch",
                }
            }
        if (service, operation) == (
            "stepfunctions",
            "describe_state_machine",
        ):
            assert params["stateMachineArn"] == coordinator.state_machine_version_arn
            return {
                "stateMachineArn": coordinator.state_machine_version_arn,
                "status": "ACTIVE",
                "type": "STANDARD",
                "roleArn": coordinator.execution_role_arn,
                "revisionId": "revision-7",
                "loggingConfiguration": {
                    "level": "ALL",
                    "includeExecutionData": False,
                    "destinations": [
                        {
                            "cloudWatchLogsLogGroup": {
                                "logGroupArn": (
                                    f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/vendedlogs/states/axonllm-launch:*"
                                )
                            }
                        }
                    ],
                },
                "tracingConfiguration": {"enabled": True},
                "encryptionConfiguration": {
                    "type": "CUSTOMER_MANAGED_KMS_KEY",
                    "kmsKeyId": coordinator.kms_key_arn,
                },
            }
        if (service, operation) == (
            "stepfunctions",
            "list_tags_for_resource",
        ):
            assert params["resourceArn"] == coordinator.state_machine_base_arn
            return {
                "tags": [
                    {"key": "Application", "value": "AxonLLM"},
                    {"key": "Environment", "value": "production"},
                    {
                        "key": "Purpose",
                        "value": "agentcore-launch-rehearsal",
                    },
                ]
            }
        if (service, operation) == ("sqs", "get_queue_attributes"):
            queue_arn = {
                resources.outbox_queue_url: resources.outbox_queue_arn,
                resources.dead_letter_queue_url: (resources.dead_letter_queue_arn),
            }[params["QueueUrl"]]
            return {"Attributes": {"QueueArn": queue_arn}}
        if (service, operation) == ("cloudwatch", "describe_alarms"):
            alarm_name = params["AlarmNames"][0]
            if alarm_name == coordinator.watchdog_alarm_arn.rsplit(":", 1)[-1]:
                return {
                    "MetricAlarms": [
                        {
                            "AlarmArn": coordinator.watchdog_alarm_arn,
                            "ActionsEnabled": True,
                            "AlarmActions": [f"arn:aws:sns:{REGION}:{ACCOUNT}:launch-alerts"],
                            "TreatMissingData": "breaching",
                            "StateValue": "OK",
                        }
                    ]
                }
            assert alarm_name == resources.dead_letter_alarm_arn.rsplit(":", 1)[-1]
            return {"MetricAlarms": [{"AlarmArn": resources.dead_letter_alarm_arn}]}
        if (service, operation) == (
            "bedrock-agentcore-control",
            "get_agent_runtime_endpoint",
        ):
            return {
                "agentRuntimeArn": resources.runtime_arn,
                "agentRuntimeEndpointArn": resources.runtime_endpoint_arn,
                "name": resources.runtime_endpoint_name,
                "status": "READY",
                "liveVersion": "7",
                "targetVersion": "7",
            }
        if (service, operation) == (
            "bedrock-agentcore-control",
            "get_agent_runtime",
        ):
            return {
                "agentRuntimeArn": resources.runtime_arn,
                "agentRuntimeVersion": "7",
                "status": "READY",
                "agentRuntimeArtifact": {
                    "containerConfiguration": {"containerUri": self.binding.release["agentcoreImage"]}
                },
            }
        if (service, operation) == ("ecs", "describe_services"):
            return {
                "services": [
                    {
                        "taskDefinition": TASK_DEFINITION_ARN,
                        "desiredCount": len(TASK_ARNS),
                        "runningCount": len(TASK_ARNS),
                    }
                ],
                "failures": [],
            }
        if (service, operation) == ("ecs", "describe_task_definition"):
            return {
                "taskDefinition": {
                    "taskDefinitionArn": TASK_DEFINITION_ARN,
                    "containerDefinitions": [
                        {
                            "name": "control-plane",
                            "image": self.binding.release["controlPlaneImage"],
                        }
                    ],
                }
            }
        if (service, operation) == ("ecs", "list_tasks"):
            return {"taskArns": list(TASK_ARNS)}
        if (service, operation) == ("ecs", "describe_tasks"):
            assert params["tasks"] == TASK_ARNS
            return {
                "tasks": [
                    {
                        "taskArn": task_arn,
                        "lastStatus": "RUNNING",
                        "taskDefinitionArn": TASK_DEFINITION_ARN,
                    }
                    for task_arn in TASK_ARNS
                ],
                "failures": [],
            }
        raise AssertionError(f"unexpected AWS call: {service} {operation}")


class FakeCoordinator:
    def __init__(
        self,
        config: rehearsal.OperationConfig,
        binding: rehearsal.ReleaseBinding,
    ) -> None:
        self.config = config
        self.binding = binding
        self.calls: list[dict[str, Any]] = []
        self.failures = 0
        self.invalid_owner = False
        self.cleanup_leaves_resources = False

    def invoke(
        self,
        payload: Mapping[str, Any],
        *,
        config: rehearsal.OperationConfig,
        binding: rehearsal.ReleaseBinding,
    ) -> Mapping[str, Any]:
        assert config == self.config
        assert binding == self.binding
        payload = deepcopy(dict(payload))
        self.calls.append(payload)
        if self.failures:
            self.failures -= 1
            raise rehearsal.LaunchOperationError("launch coordinator operation did not complete")
        owner_id = payload["owner"]["id"]
        ownership = deepcopy(
            payload["parameters"].get("ownership")
            or {
                "ownerId": owner_id,
                "expiresAt": payload["owner"]["expiresAt"],
                "faultIds": [],
                "fixtureIds": [],
                "dlqCorrelationIds": [],
                "snapshots": {
                    "model": None,
                    "tenantConfig": None,
                },
            }
        )
        if self.invalid_owner:
            ownership["faultIds"] = ["foreign:fault"]
        operation = payload["operation"]
        if operation == "cleanup":
            prior = payload["parameters"]["ownership"]
            snapshots = sorted(item["ref"] for item in prior["snapshots"].values() if item is not None)
            evidence_value = {
                "restoredSnapshotRefs": snapshots,
                "clearedFaultIds": list(prior["faultIds"]),
                "clearedFixtureIds": list(prior["fixtureIds"]),
                "redrivenDlqCorrelationIds": list(prior["dlqCorrelationIds"]),
                "removedDlqCorrelationIds": [],
                "primaryStateSelected": True,
                "productionEndpointStatus": "READY",
                "faultsRemaining": 0,
                "fixturesRemaining": 0,
                "correlatedDlqMessagesRemaining": 0,
            }
            if self.cleanup_leaves_resources:
                evidence_value["faultsRemaining"] = 1
            ownership = {
                "ownerId": owner_id,
                "expiresAt": payload["owner"]["expiresAt"],
                "faultIds": (list(prior["faultIds"]) if self.cleanup_leaves_resources else []),
                "fixtureIds": [],
                "dlqCorrelationIds": [],
                "snapshots": {
                    "model": None,
                    "tenantConfig": None,
                },
            }
        else:
            full = _all_observations()[payload["gate"]]
            evidence_value = {name: deepcopy(full[name]) for name in rehearsal.ACTION_EVIDENCE_FIELDS[operation]}
        result = {
            "schema": rehearsal.ACTION_RESULT_SCHEMA,
            "gate": payload["gate"],
            "operation": operation,
            "ownerId": owner_id,
            "correlationId": payload["correlationId"],
            "idempotencyKey": payload["idempotencyKey"],
            "status": "SUCCEEDED",
            "binding": deepcopy(payload["binding"]),
            "evidence": evidence_value,
            "ownership": ownership,
        }
        return result


def _coordinator_payload(
    config: rehearsal.OperationConfig,
    binding: rehearsal.ReleaseBinding,
) -> dict[str, Any]:
    gate = "initializationTimeoutReplacement"
    action = "induce-initialization-timeout"
    return rehearsal._request_payload(
        gate=gate,
        action=action,
        correlation_id=rehearsal._correlation_id(binding, gate, action),
        idempotency_key=rehearsal._idempotency_key(
            binding,
            gate,
            action,
        ),
        config=config,
        binding=binding,
        ownership={
            "ownerId": binding.owner_id,
            "expiresAt": config.review.expires_at.isoformat(),
            "faultIds": [],
            "fixtureIds": [],
            "dlqCorrelationIds": [],
            "snapshots": {"model": None, "tenantConfig": None},
        },
    )


class StepFunctionsAws:
    def __init__(
        self,
        payload: Mapping[str, Any],
        config: rehearsal.OperationConfig,
        *,
        retry_outputs: int = 0,
        running_polls: int = 0,
        ambiguous_start: bool = False,
    ) -> None:
        self.payload = payload
        self.config = config
        self.retry_outputs = retry_outputs
        self.running_polls = running_polls
        self.ambiguous_start = ambiguous_start
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.executions: dict[str, dict[str, Any]] = {}

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        assert service == "stepfunctions"
        assert region == REGION
        assert timeout_seconds > 0
        params = dict(parameters)
        self.calls.append((operation, params))
        if operation == "describe_execution":
            execution = self.executions.get(params["executionArn"])
            if execution is None:
                raise rehearsal.AwsCallError(
                    "stepfunctions",
                    "ExecutionDoesNotExist",
                )
            if execution["runningPolls"]:
                execution["runningPolls"] -= 1
                status = "RUNNING"
                output = None
            else:
                status = execution["status"]
                output = execution.get("output")
            result = {
                "executionArn": execution["executionArn"],
                "name": execution["name"],
                "stateMachineArn": (
                    self.config.coordinator.state_machine_base_arn
                ),
                "stateMachineVersionArn": (
                    self.config.coordinator.state_machine_version_arn
                ),
                "input": execution["input"],
                "status": status,
            }
            if output is not None and status == "SUCCEEDED":
                result["output"] = json.dumps(
                    output,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            return result
        if operation == "start_execution":
            if self.ambiguous_start:
                raise rehearsal.AwsCallError(
                    "stepfunctions",
                    "RequestTimeout",
                )
            execution_arn = next(
                arn
                for retry in range(self.config.limits.max_attempts)
                for name, arn in [
                    rehearsal.StepFunctionsCoordinator._execution_identity(
                        self.payload,
                        self.config.coordinator,
                        retry_number=retry,
                    )
                ]
                if name == params["name"]
            )
            retry_number = len(self.executions)
            if retry_number < self.retry_outputs:
                output = {
                    "schema": rehearsal.ACTION_RETRY_SCHEMA,
                    "status": "RETRY",
                    "gate": self.payload["gate"],
                    "operation": self.payload["operation"],
                    "ownerId": self.payload["owner"]["id"],
                    "correlationId": self.payload["correlationId"],
                    "idempotencyKey": self.payload["idempotencyKey"],
                    "code": "ProviderFixtureUnavailable",
                    "retryable": True,
                }
            else:
                output = {"converged": True}
            self.executions[execution_arn] = {
                "executionArn": execution_arn,
                "name": params["name"],
                "input": params["input"],
                "runningPolls": self.running_polls,
                "status": "SUCCEEDED",
                "output": output,
            }
            return {"executionArn": execution_arn}
        if operation == "stop_execution":
            execution = self.executions[params["executionArn"]]
            execution["runningPolls"] = 0
            execution["status"] = "ABORTED"
            execution.pop("output", None)
            return {}
        raise AssertionError(operation)


def test_step_functions_retry_output_converges_across_multiple_polls() -> None:
    config = _parse_config()
    binding = _binding(config)
    payload = _coordinator_payload(config, binding)
    aws = StepFunctionsAws(
        payload,
        config,
        retry_outputs=1,
        running_polls=2,
    )
    coordinator = rehearsal.StepFunctionsCoordinator(
        aws,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    result = coordinator.invoke(
        payload,
        config=config,
        binding=binding,
    )

    assert result == {"converged": True}
    starts = [
        parameters["name"]
        for operation, parameters in aws.calls
        if operation == "start_execution"
    ]
    assert len(starts) == 2
    assert not starts[0].endswith("-r01")
    assert starts[1].endswith("-r01")
    assert sum(
        operation == "describe_execution"
        for operation, _parameters in aws.calls
    ) >= 8


def test_step_functions_retry_output_stops_at_reviewed_attempt_bound() -> None:
    config = _parse_config()
    binding = _binding(config)
    payload = _coordinator_payload(config, binding)
    aws = StepFunctionsAws(
        payload,
        config,
        retry_outputs=config.limits.max_attempts,
    )
    coordinator = rehearsal.StepFunctionsCoordinator(
        aws,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(
        rehearsal.LaunchOperationError,
        match="retry budget is exhausted",
    ):
        coordinator.invoke(
            payload,
            config=config,
            binding=binding,
        )

    assert (
        sum(
            operation == "start_execution"
            for operation, _parameters in aws.calls
        )
        == config.limits.max_attempts
    )


def test_ambiguous_start_with_absent_describe_is_not_treated_as_drained() -> None:
    config = _parse_config()
    binding = _binding(config)
    payload = _coordinator_payload(config, binding)
    aws = StepFunctionsAws(
        payload,
        config,
        ambiguous_start=True,
    )
    coordinator = rehearsal.StepFunctionsCoordinator(
        aws,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(
        rehearsal.CoordinatorNotDrainedError,
        match="could not be drained",
    ):
        coordinator.invoke(
            payload,
            config=config,
            binding=binding,
        )

    assert any(
        operation == "start_execution"
        for operation, _parameters in aws.calls
    )
    assert not any(
        operation == "stop_execution"
        for operation, _parameters in aws.calls
    )


def test_stop_and_drain_aborts_a_bound_running_execution() -> None:
    config = _parse_config()
    binding = _binding(config)
    payload = _coordinator_payload(config, binding)
    aws = StepFunctionsAws(payload, config)
    name, execution_arn = (
        rehearsal.StepFunctionsCoordinator._execution_identity(
            payload,
            config.coordinator,
        )
    )
    input_text = rehearsal._canonical_bytes(payload).decode().rstrip("\n")
    aws.executions[execution_arn] = {
        "executionArn": execution_arn,
        "name": name,
        "input": input_text,
        "runningPolls": 1,
        "status": "RUNNING",
    }
    coordinator = rehearsal.StepFunctionsCoordinator(
        aws,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    assert coordinator._stop_and_drain(
        execution_name=name,
        execution_arn=execution_arn,
        input_text=input_text,
        config=config,
        binding=binding,
    )
    assert any(
        operation == "stop_execution"
        for operation, _parameters in aws.calls
    )
    assert aws.executions[execution_arn]["status"] == "ABORTED"


def test_stop_and_drain_waits_for_ambiguous_execution_visibility() -> None:
    config = _parse_config()
    binding = _binding(config)
    payload = _coordinator_payload(config, binding)
    name, execution_arn = (
        rehearsal.StepFunctionsCoordinator._execution_identity(
            payload,
            config.coordinator,
        )
    )
    input_text = rehearsal._canonical_bytes(payload).decode().rstrip("\n")

    class EventuallyVisibleAws(StepFunctionsAws):
        hidden_descriptions = 2

        def call(
            self,
            service: str,
            operation: str,
            *,
            region: str,
            parameters: Mapping[str, Any],
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            if (
                operation == "describe_execution"
                and self.hidden_descriptions > 0
            ):
                self.hidden_descriptions -= 1
                self.calls.append((operation, dict(parameters)))
                raise rehearsal.AwsCallError(
                    "stepfunctions",
                    "ExecutionDoesNotExist",
                )
            return super().call(
                service,
                operation,
                region=region,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
            )

    aws = EventuallyVisibleAws(payload, config)
    aws.executions[execution_arn] = {
        "executionArn": execution_arn,
        "name": name,
        "input": input_text,
        "runningPolls": 1,
        "status": "RUNNING",
    }
    coordinator = rehearsal.StepFunctionsCoordinator(
        aws,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    assert coordinator._stop_and_drain(
        execution_name=name,
        execution_arn=execution_arn,
        input_text=input_text,
        config=config,
        binding=binding,
    )
    assert sum(
        operation == "describe_execution"
        for operation, _parameters in aws.calls
    ) >= 4
    assert any(
        operation == "stop_execution"
        for operation, _parameters in aws.calls
    )
    assert aws.executions[execution_arn]["status"] == "ABORTED"


@contextmanager
def _runner(
    tmp_path: Path,
    *,
    config: rehearsal.OperationConfig | None = None,
    binding: rehearsal.ReleaseBinding | None = None,
    aws: FakeAws | None = None,
    coordinator: FakeCoordinator | None = None,
    now: datetime = NOW,
) -> Iterator[
    tuple[
        rehearsal.LaunchRehearsal,
        rehearsal.StateDirectory,
        FakeAws,
        FakeCoordinator,
    ]
]:
    resolved_config = config or _parse_config()
    resolved_binding = binding or _binding(resolved_config)
    resolved_aws = aws or FakeAws(resolved_config, resolved_binding)
    resolved_coordinator = coordinator or FakeCoordinator(
        resolved_config,
        resolved_binding,
    )
    state = rehearsal.StateDirectory(tmp_path / "state")
    with state:
        yield (
            rehearsal.LaunchRehearsal(
                config=resolved_config,
                binding=resolved_binding,
                state=state,
                aws=resolved_aws,
                coordinator=resolved_coordinator,
                now=lambda: now,
            ),
            state,
            resolved_aws,
            resolved_coordinator,
        )


def _write_config(tmp_path: Path, value: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "rehearsal.json"
    path.write_text(
        json.dumps(_config_value() if value is None else value),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _cli_args(
    config_path: Path,
    state_dir: Path,
    *,
    action: str = "induce-initialization-timeout",
    gate: str | None = "initializationTimeoutReplacement",
) -> list[str]:
    args = [
        action,
        "--reviewed-config",
        str(config_path),
        "--state-dir",
        str(state_dir),
        "--release-commit",
        RELEASE_COMMIT,
        "--agentcore-image",
        AGENTCORE_IMAGE,
        "--control-plane-image",
        CONTROL_PLANE_IMAGE,
        "--region",
        REGION,
        "--repository",
        REPOSITORY,
        "--workflow-ref",
        WORKFLOW_REF,
        "--workflow-commit",
        WORKFLOW_COMMIT,
        "--run-id",
        "41",
        "--run-attempt",
        "1",
        "--reviewed-config-s3-uri",
        REVIEWED_CONFIG_URI,
        "--reviewed-config-version-id",
        REVIEWED_CONFIG_VERSION_ID,
        "--reviewed-config-sha256",
        hashlib.sha256(config_path.read_bytes()).hexdigest(),
    ]
    if gate is not None:
        args.extend(["--gate", gate])
    return args


def test_exposes_every_evidence_action_and_v2_coordinator_config() -> None:
    expected = {action for commands in evidence.EXPECTED_COMMANDS.values() for action in commands}
    assert rehearsal.ALL_ACTIONS == expected
    config = _parse_config()
    assert config.coordinator == rehearsal.Coordinator(
        state_machine_version_arn=STATE_MACHINE_ARN,
        execution_role_arn=EXECUTION_ROLE_ARN,
        launch_role_arn=LAUNCH_ROLE_ARN,
        lease_table_arn=LEASE_TABLE_ARN,
        watchdog_alarm_arn=WATCHDOG_ALARM_ARN,
        kms_key_arn=KMS_KEY_ARN,
        cleanup_deadline_seconds=900,
    )
    assert config.coordinator.state_machine_base_arn == (STATE_MACHINE_ARN.rsplit(":", 1)[0])
    assert config.coordinator.state_machine_name == ("AxonLLMLaunchCoordinator")
    assert config.coordinator.lease_table_name == LEASE_TABLE


def test_all_gate_sequences_emit_only_final_valid_observations(
    tmp_path: Path,
) -> None:
    with _runner(tmp_path) as (runner, state, aws, coordinator):
        for gate, commands in rehearsal.EXPECTED_COMMANDS.items():
            for index, action in enumerate(commands):
                output = runner.run(gate, action)
                assert output == {
                    "schema": evidence.COMMAND_OUTPUT_SCHEMA,
                    "gate": gate,
                    "action": action,
                    "release": runner.binding.release,
                    "execution": runner.binding.execution,
                    "observations": (_all_observations()[gate] if index == len(commands) - 1 else None),
                }
        journal = state.read()
        assert journal is not None
        assert journal["activeGate"] is None
        assert set(journal["gates"]) == set(rehearsal.ALL_GATES)
        assert {call["operation"] for call in coordinator.calls} == (rehearsal.ALL_ACTIONS)
        first_payload = coordinator.calls[0]
        assert first_payload["schema"] == rehearsal.ACTION_SCHEMA
        assert first_payload["binding"]["coordinatorStateMachineVersionArn"] == STATE_MACHINE_ARN
        assert first_payload["binding"]["reviewedConfigS3Uri"] == (REVIEWED_CONFIG_URI)
        assert first_payload["binding"]["reviewedConfigVersionId"] == (REVIEWED_CONFIG_VERSION_ID)
        assert first_payload["binding"]["controlPlaneImage"] == (CONTROL_PLANE_IMAGE)
        assert {
            (service, operation)
            for service, operation, _parameters in aws.calls
        } == {("sts", "get_caller_identity")}


def test_worker_side_preflight_verifies_live_release_without_launch_identity() -> None:
    config = _parse_config()
    binding = _binding(config)
    aws = FakeAws(config, binding, account="999999999999")

    rehearsal.verify_deployment_binding(
        aws,
        config,
        binding,
        require_primary=True,
        require_launch_identity=False,
    )

    operations = {
        (service, operation)
        for service, operation, _parameters in aws.calls
    }
    assert ("sts", "get_caller_identity") not in operations
    assert {
        ("cloudformation", "describe_stacks"),
        ("dynamodb", "describe_table"),
        ("stepfunctions", "describe_state_machine"),
        ("bedrock-agentcore-control", "get_agent_runtime_endpoint"),
        ("bedrock-agentcore-control", "get_agent_runtime"),
        ("ecs", "describe_task_definition"),
        ("ecs", "describe_tasks"),
    } <= operations


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"password": "stored-value"}),
        lambda value: value["coordinator"].update({"apiKey": "stored-value"}),
        lambda value: value["coordinator"].update({"stateMachineVersionArn": (STATE_MACHINE_ARN.rsplit(":", 1)[0])}),
        lambda value: value["coordinator"].update(
            {"launchRoleArn": ("arn:aws:iam::999999999999:role/ForeignLaunchRole")}
        ),
        lambda value: value["coordinator"].update({"leaseTableArn": STATE_TABLE_ARN}),
        lambda value: value["coordinator"].update({"watchdogAlarmArn": ALARM_ARN}),
        lambda value: value["resources"].update(
            {"runtimeArn": (f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/*")}
        ),
        lambda value: value["resources"].update(
            {"stateTableArn": (f"arn:aws:dynamodb:{REGION}:999999999999:table/{STATE_TABLE}")}
        ),
        lambda value: value["resources"].update(
            {"restoredStateTableArn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foreign")}
        ),
    ],
)
def test_config_rejects_invalid_coordinator_and_resource_bindings(
    mutation,
) -> None:
    value = _config_value()
    mutation(value)
    with pytest.raises(rehearsal.LaunchOperationError):
        _parse_config(value)


def test_strict_config_rejects_duplicate_fields_and_symlinks(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"one","schema":"two"}',
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    with pytest.raises(
        rehearsal.LaunchOperationError,
        match="duplicate JSON field",
    ):
        rehearsal.load_config(duplicate, region=REGION, now=NOW)

    source = _write_config(tmp_path)
    link = tmp_path / "linked.json"
    link.symlink_to(source)
    with pytest.raises(
        rehearsal.LaunchOperationError,
        match="path contains a symlink",
    ):
        rehearsal.load_config(link, region=REGION, now=NOW)

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    nested_config = real_directory / "reviewed.json"
    nested_config.write_text(json.dumps(_config_value()), encoding="utf-8")
    nested_config.chmod(0o600)
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(
        rehearsal.LaunchOperationError,
        match="path contains a symlink",
    ):
        rehearsal.load_config(
            linked_directory / "reviewed.json",
            region=REGION,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agentcore_image", AGENTCORE_IMAGE.split("@", 1)[0] + ":latest"),
        (
            "control_plane_image",
            CONTROL_PLANE_IMAGE.split("@", 1)[0] + ":release",
        ),
        (
            "agentcore_image",
            AGENTCORE_IMAGE.replace(ACCOUNT, "999999999999", 1),
        ),
        (
            "workflow_ref",
            f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/main",
        ),
        ("release_commit", "main"),
        ("reviewed_config_uri", f"{REVIEWED_CONFIG_URI}?mutable=true"),
        ("reviewed_config_version_id", "null"),
        ("reviewed_config_sha256", "d" * 64),
    ],
)
def test_release_binding_rejects_mutable_foreign_or_unprotected_inputs(
    field: str,
    value: str,
) -> None:
    config = _parse_config()
    with pytest.raises(rehearsal.LaunchOperationError):
        _binding(config, **{field: value})


def test_release_binding_includes_immutable_config_and_both_images() -> None:
    config = _parse_config()
    binding = _binding(config)
    assert binding.release == {
        "commit": RELEASE_COMMIT,
        "region": REGION,
        "agentcoreImage": AGENTCORE_IMAGE,
        "controlPlaneImage": CONTROL_PLANE_IMAGE,
    }
    assert binding.execution["reviewedConfigS3Uri"] == REVIEWED_CONFIG_URI
    assert binding.execution["reviewedConfigVersionId"] == (REVIEWED_CONFIG_VERSION_ID)
    assert binding.execution["reviewedConfigSha256"] == config.sha256


def test_review_window_and_state_directory_permissions_are_enforced(
    tmp_path: Path,
) -> None:
    stale = _config_value()
    stale["review"]["expiresAt"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(rehearsal.LaunchOperationError, match="stale"):
        _parse_config(stale)

    state_path = tmp_path / "state"
    state_path.mkdir(mode=0o755)
    state_path.chmod(0o755)
    with pytest.raises(rehearsal.LaunchOperationError, match="owner-only"):
        with rehearsal.StateDirectory(state_path):
            pass

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked-state"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(rehearsal.LaunchOperationError, match="symlink"):
        with rehearsal.StateDirectory(linked):
            pass


def test_state_directory_rejects_concurrent_owner(tmp_path: Path) -> None:
    path = tmp_path / "state"
    with rehearsal.StateDirectory(path):
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="concurrent",
        ):
            with rehearsal.StateDirectory(path):
                pass


def test_command_order_and_cross_gate_overlap_are_rejected(
    tmp_path: Path,
) -> None:
    with _runner(tmp_path) as (runner, _state, _aws, _coordinator):
        with pytest.raises(rehearsal.LaunchOperationError):
            runner.run(
                "initializationTimeoutReplacement",
                "observe-exit-124",
            )
        runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="another gate",
        ):
            runner.run(
                "providerRoutingStrategies",
                "exercise-routing-strategies",
            )
        with pytest.raises(rehearsal.LaunchOperationError, match="order"):
            runner.run(
                "initializationTimeoutReplacement",
                "observe-runtime-replacement",
            )


def test_completed_action_resume_revalidates_same_idempotent_result(
    tmp_path: Path,
) -> None:
    with _runner(tmp_path) as (runner, _state, _aws, coordinator):
        first = runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )
        call_count = len(coordinator.calls)
        resumed = runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )
        assert resumed == first
        assert len(coordinator.calls) == call_count + 1
        assert coordinator.calls[-1] == coordinator.calls[0]


def test_transport_failure_resumes_same_idempotent_operation(
    tmp_path: Path,
) -> None:
    config = _parse_config()
    binding = _binding(config)
    coordinator = FakeCoordinator(config, binding)
    coordinator.failures = 1
    with _runner(
        tmp_path,
        config=config,
        binding=binding,
        coordinator=coordinator,
    ) as (
        runner,
        state,
        _aws,
        _coordinator,
    ):
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="did not complete",
        ):
            runner.run(
                "initializationTimeoutReplacement",
                "induce-initialization-timeout",
            )
        journal = state.read()
        assert journal is not None
        record = journal["gates"]["initializationTimeoutReplacement"]["actions"]["induce-initialization-timeout"]
        assert record["status"] == "in_progress"
        first_call = deepcopy(coordinator.calls[0])

        output = runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )
        assert output["observations"] is None
        assert coordinator.calls[-1] == first_call


def test_foreign_runtime_ownership_is_rejected_without_success(
    tmp_path: Path,
) -> None:
    config = _parse_config()
    binding = _binding(config)
    coordinator = FakeCoordinator(config, binding)
    coordinator.invalid_owner = True
    with _runner(
        tmp_path,
        config=config,
        binding=binding,
        coordinator=coordinator,
    ) as (
        runner,
        state,
        _aws,
        _coordinator,
    ):
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="not owned",
        ):
            runner.run(
                "initializationTimeoutReplacement",
                "induce-initialization-timeout",
            )
        journal = state.read()
        assert journal is not None
        gate = journal["gates"]["initializationTimeoutReplacement"]
        assert gate["nextIndex"] == 0
        assert gate["actions"]["induce-initialization-timeout"]["status"] == "in_progress"


def test_wrong_launch_role_identity_fails_before_coordinator_mutation(
    tmp_path: Path,
) -> None:
    config = _parse_config()
    binding = _binding(config)
    aws = FakeAws(
        config,
        binding,
        account="999999999999",
    )
    coordinator = FakeCoordinator(config, binding)
    with _runner(
        tmp_path,
        config=config,
        binding=binding,
        aws=aws,
        coordinator=coordinator,
    ) as (runner, _state, _aws, _coordinator):
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="launch-gates role session",
        ):
            runner.run(
                "initializationTimeoutReplacement",
                "induce-initialization-timeout",
            )
        assert coordinator.calls == []


def test_mismatched_and_stale_state_are_rejected(tmp_path: Path) -> None:
    config = _parse_config()
    binding = _binding(config)
    with _runner(
        tmp_path,
        config=config,
        binding=binding,
    ) as (runner, _state, _aws, _coordinator):
        runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )

    changed_config = replace(config, sha256="d" * 64)
    changed_binding = _binding(changed_config)
    with _runner(
        tmp_path,
        config=changed_config,
        binding=changed_binding,
    ) as (runner, _state, _aws, _coordinator):
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="does not match",
        ):
            runner.run(
                "initializationTimeoutReplacement",
                "induce-initialization-timeout",
            )

    with _runner(
        tmp_path,
        config=config,
        binding=binding,
        now=NOW + timedelta(hours=5),
    ) as (runner, _state, _aws, _coordinator):
        with pytest.raises(rehearsal.LaunchOperationError, match="stale"):
            runner.run(
                "initializationTimeoutReplacement",
                "induce-initialization-timeout",
            )


def _owned_inventory(
    owner_id: str,
    expires_at: str,
) -> dict[str, Any]:
    return {
        "ownerId": owner_id,
        "expiresAt": expires_at,
        "faultIds": [f"{owner_id}:fault:provider"],
        "fixtureIds": [f"{owner_id}:fixture:restore"],
        "dlqCorrelationIds": [f"{owner_id}:dlq:event-1"],
        "snapshots": {
            "model": {
                "ref": f"{owner_id}:snapshot:model",
                "sha256": "3" * 64,
                "revision": 7,
            },
            "tenantConfig": {
                "ref": f"{owner_id}:snapshot:tenant-config",
                "sha256": "4" * 64,
                "revision": 11,
            },
        },
    }


def test_cleanup_restores_only_owned_state_and_proves_nothing_remains(
    tmp_path: Path,
) -> None:
    with _runner(tmp_path) as (runner, state, _aws, coordinator):
        runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )
        journal = state.read()
        assert journal is not None
        journal["ownership"] = _owned_inventory(
            runner.binding.owner_id,
            journal["reviewExpiresAt"],
        )
        state.write(journal)

        output = runner.cleanup()
        assert output["gate"] == "cleanup"
        assert output["action"] == "cleanup"
        assert output["observations"] == {
            "restoredSnapshotRefs": [
                f"{runner.binding.owner_id}:snapshot:model",
                f"{runner.binding.owner_id}:snapshot:tenant-config",
            ],
            "clearedFaultIds": [f"{runner.binding.owner_id}:fault:provider"],
            "clearedFixtureIds": [f"{runner.binding.owner_id}:fixture:restore"],
            "redrivenDlqCorrelationIds": [f"{runner.binding.owner_id}:dlq:event-1"],
            "removedDlqCorrelationIds": [],
            "primaryStateSelected": True,
            "productionEndpointStatus": "READY",
            "faultsRemaining": 0,
            "fixturesRemaining": 0,
            "correlatedDlqMessagesRemaining": 0,
        }
        cleanup_call = coordinator.calls[-1]
        assert cleanup_call["operation"] == "cleanup"
        assert cleanup_call["parameters"]["ownership"] == (journal["ownership"])
        assert cleanup_call["parameters"]["ownership"]["faultIds"] == [f"{runner.binding.owner_id}:fault:provider"]
        final = state.read()
        assert final is not None
        assert final["cleanup"]["status"] == "complete"
        assert final["ownership"]["faultIds"] == []
        assert final["ownership"]["fixtureIds"] == []
        assert final["ownership"]["dlqCorrelationIds"] == []
        assert final["ownership"]["snapshots"] == {
            "model": None,
            "tenantConfig": None,
        }
        call_count = len(coordinator.calls)
        assert runner.cleanup() == output
        assert len(coordinator.calls) == call_count + 1
        assert coordinator.calls[-1] == cleanup_call


def test_cleanup_fails_closed_when_runtime_reports_a_remaining_effect(
    tmp_path: Path,
) -> None:
    config = _parse_config()
    binding = _binding(config)
    coordinator = FakeCoordinator(config, binding)
    coordinator.cleanup_leaves_resources = True
    with _runner(
        tmp_path,
        config=config,
        binding=binding,
        coordinator=coordinator,
    ) as (
        runner,
        state,
        _aws,
        _coordinator,
    ):
        runner.run(
            "initializationTimeoutReplacement",
            "induce-initialization-timeout",
        )
        journal = state.read()
        assert journal is not None
        journal["ownership"] = _owned_inventory(
            runner.binding.owner_id,
            journal["reviewExpiresAt"],
        )
        state.write(journal)
        with pytest.raises(
            rehearsal.LaunchOperationError,
            match="cleanup did not restore",
        ):
            runner.cleanup()
        failed = state.read()
        assert failed is not None
        assert failed["cleanup"]["status"] == "in_progress"
        assert failed["ownership"] == journal["ownership"]


def test_worker_cleanup_preflight_fails_when_aws_is_not_back_on_primary() -> None:
    config = _parse_config()
    binding = _binding(config)
    aws = FakeAws(config, binding, primary=False)

    with pytest.raises(
        rehearsal.LaunchOperationError,
        match="healthy primary",
    ):
        rehearsal.verify_deployment_binding(
            aws,
            config,
            binding,
            require_primary=True,
            require_launch_identity=False,
        )


def test_cli_uses_positional_action_and_emits_reviewed_binding(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(tmp_path)
    config = rehearsal.load_config(path, region=REGION, now=NOW)
    binding = _binding(config)
    aws = FakeAws(config, binding)
    coordinator = FakeCoordinator(config, binding)
    args = _cli_args(path, tmp_path / "state")
    assert args[0] == "induce-initialization-timeout"
    assert "--action" not in args
    assert "--reviewed-config" in args
    assert "--control-plane-image" in args
    result = rehearsal.main(
        args,
        aws=aws,
        coordinator=coordinator,
        now=lambda: NOW,
    )
    captured = capfd.readouterr()
    assert result == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    output = json.loads(captured.out)
    assert output["schema"] == evidence.COMMAND_OUTPUT_SCHEMA
    assert output["observations"] is None
    assert output["release"]["controlPlaneImage"] == CONTROL_PLANE_IMAGE
    assert output["execution"]["reviewedConfigS3Uri"] == (REVIEWED_CONFIG_URI)
    assert output["execution"]["reviewedConfigVersionId"] == (REVIEWED_CONFIG_VERSION_ID)
    assert output["execution"]["reviewedConfigSha256"] == config.sha256
    assert coordinator.calls[0]["binding"]["reviewedConfigSha256"] == (config.sha256)


def test_cli_failure_is_silent_and_does_not_emit_false_success(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(tmp_path)
    args = _cli_args(path, tmp_path / "state")
    args[args.index("--agentcore-image") + 1] = AGENTCORE_IMAGE.split("@", 1)[0] + ":latest"
    config = rehearsal.load_config(path, region=REGION, now=NOW)
    binding = _binding(config)
    result = rehearsal.main(
        args,
        aws=FakeAws(config, binding),
        coordinator=FakeCoordinator(config, binding),
        now=lambda: NOW,
    )
    captured = capfd.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == ""


def test_cli_cleanup_omits_gate_and_prints_cleanup_evidence(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(tmp_path)
    state_dir = tmp_path / "state"
    config = rehearsal.load_config(path, region=REGION, now=NOW)
    binding = _binding(config)
    aws = FakeAws(config, binding)
    coordinator = FakeCoordinator(config, binding)
    assert (
        rehearsal.main(
            _cli_args(path, state_dir),
            aws=aws,
            coordinator=coordinator,
            now=lambda: NOW,
        )
        == 0
    )
    capfd.readouterr()

    args = _cli_args(path, state_dir, action="cleanup", gate=None)
    assert args[0] == "cleanup"
    assert "--gate" not in args
    result = rehearsal.main(
        args,
        aws=aws,
        coordinator=coordinator,
        now=lambda: NOW,
    )
    captured = capfd.readouterr()
    assert result == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    output = json.loads(captured.out)
    assert output["gate"] == "cleanup"
    assert output["action"] == "cleanup"
    assert output["observations"] == {
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
    assert coordinator.calls[-1]["gate"] == "cleanup"
    assert coordinator.calls[-1]["operation"] == "cleanup"
