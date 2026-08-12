#!/usr/bin/env python3
"""Operate reviewed AgentCore restored-table cutovers and rollbacks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aws_support import AwsCli, AwsError


class AgentCoreRecoveryError(RuntimeError):
    """Raised when an AgentCore recovery phase cannot prove safety."""


_CONTROL_PLANE_STACK = "AxonLLMControlPlaneStack"
_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
_SUCCESS_STATUS = "UPDATE_COMPLETE"
_IN_PROGRESS_STATUSES = {
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_IN_PROGRESS",
}


def _describe_stack(aws: AwsCli, stack_name: str) -> dict[str, Any]:
    stacks = aws.json(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        stack_name,
    ).get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise AgentCoreRecoveryError(
            f"could not resolve exactly one stack named {stack_name}"
        )
    stack = stacks[0]
    if not isinstance(stack, dict):
        raise AgentCoreRecoveryError(
            f"CloudFormation returned a malformed {stack_name} stack"
        )
    return stack


def _optional_stack(
    aws: AwsCli,
    stack_name: str,
) -> dict[str, Any] | None:
    try:
        return _describe_stack(aws, stack_name)
    except AwsError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def _outputs(stack: dict[str, Any]) -> dict[str, str]:
    values = stack.get("Outputs", [])
    if not isinstance(values, list):
        raise AgentCoreRecoveryError("stack outputs are malformed")
    return {
        item["OutputKey"]: item["OutputValue"]
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("OutputKey"), str)
        and isinstance(item.get("OutputValue"), str)
    }


def _runtime_outputs(
    aws: AwsCli,
    stack_name: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    stack = _describe_stack(aws, stack_name)
    outputs = _outputs(stack)
    required = {
        "DataKeyArn",
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "RecoveryMinimumQuiescenceSeconds",
        "RecoveryQuiescedAt",
        "RuntimeArn",
        "SelectedRuntimeStateTableName",
        "StateTableName",
    }
    missing = sorted(required.difference(outputs))
    if missing:
        raise AgentCoreRecoveryError(
            "AgentCore stack does not contain the recovery selector: "
            + ", ".join(missing)
        )
    return stack, outputs


def _control_plane_runtime_outputs(
    aws: AwsCli,
) -> tuple[dict[str, Any], dict[str, str]] | None:
    stack = _optional_stack(aws, _CONTROL_PLANE_STACK)
    if stack is None:
        return None
    outputs = _outputs(stack)
    required = {
        "AgentCoreStackName",
        "ClusterName",
        "PrimaryStateTableName",
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "SelectedRuntimeStateTableName",
        "ServiceName",
    }
    missing = sorted(required.difference(outputs))
    if missing:
        raise AgentCoreRecoveryError(
            "control-plane stack does not contain the coordinated recovery "
            "selector: "
            + ", ".join(missing)
        )
    return stack, outputs


def _stack_parameters(stack: dict[str, Any]) -> set[str]:
    parameters = stack.get("Parameters", [])
    if not isinstance(parameters, list):
        raise AgentCoreRecoveryError("stack parameters are malformed")
    names = {
        item.get("ParameterKey")
        for item in parameters
        if isinstance(item, dict)
        and isinstance(item.get("ParameterKey"), str)
    }
    return {name for name in names if isinstance(name, str)}


def _updated_at(stack: dict[str, Any]) -> str:
    value = stack.get("LastUpdatedTime", stack.get("CreationTime"))
    if not isinstance(value, str):
        raise AgentCoreRecoveryError("stack update timestamp is missing")
    return value


def _wait_for_stack_update(
    aws: AwsCli,
    *,
    stack_name: str,
    previous_updated_at: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    observed_update = False
    while time.monotonic() < deadline:
        stack = _describe_stack(aws, stack_name)
        status = stack.get("StackStatus")
        changed = _updated_at(stack) != previous_updated_at
        if status in _IN_PROGRESS_STATUSES:
            observed_update = True
        elif status == _SUCCESS_STATUS and (changed or observed_update):
            return stack
        elif (
            isinstance(status, str)
            and status.endswith(("_FAILED", "_ROLLBACK_COMPLETE"))
        ):
            reason = stack.get("StackStatusReason", "no reason returned")
            raise AgentCoreRecoveryError(
                f"{stack_name} update failed in {status}: {reason}"
            )
        sleep(poll_interval)
    raise AgentCoreRecoveryError(
        f"{stack_name} did not complete its update in time"
    )


def _update_selector(
    aws: AwsCli,
    *,
    stack_name: str,
    mode: str,
    target_parameter: str,
    approval_id: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, str]]:
    stack = _describe_stack(aws, stack_name)
    role_arn = stack.get("RoleARN")
    if not isinstance(role_arn, str) or not role_arn:
        raise AgentCoreRecoveryError(
            f"{stack_name} has no CloudFormation execution role; "
            "re-deploy it with a reviewed execution role before recovery"
        )
    parameter_names = _stack_parameters(stack)
    changes = {
        "RecoveryApprovalId": approval_id,
        "RecoveryCutoverMode": mode,
        "RuntimeStateTableName": target_parameter,
    }
    missing = sorted(set(changes).difference(parameter_names))
    if missing:
        raise AgentCoreRecoveryError(
            f"deployed {stack_name} template lacks recovery parameters: "
            + ", ".join(missing)
        )
    parameters = [
        (
            {"ParameterKey": name, "ParameterValue": changes[name]}
            if name in changes
            else {"ParameterKey": name, "UsePreviousValue": True}
        )
        for name in sorted(parameter_names)
    ]
    previous_updated_at = _updated_at(stack)
    aws.json(
        "cloudformation",
        "update-stack",
        "--stack-name",
        stack_name,
        "--use-previous-template",
        "--capabilities",
        "CAPABILITY_NAMED_IAM",
        "--parameters",
        json.dumps(parameters, separators=(",", ":")),
    )
    updated = _wait_for_stack_update(
        aws,
        stack_name=stack_name,
        previous_updated_at=previous_updated_at,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )
    return updated, _outputs(updated)


def _scalable_target(
    aws: AwsCli,
    *,
    cluster_name: str,
    service_name: str,
) -> dict[str, Any]:
    resource_id = f"service/{cluster_name}/{service_name}"
    targets = aws.json(
        "application-autoscaling",
        "describe-scalable-targets",
        "--service-namespace",
        "ecs",
        "--resource-ids",
        resource_id,
        "--scalable-dimension",
        "ecs:service:DesiredCount",
    ).get("ScalableTargets")
    if not isinstance(targets, list) or len(targets) != 1:
        raise AgentCoreRecoveryError(
            "recovery requires exactly one control-plane scalable target"
        )
    target = targets[0]
    if not isinstance(target, dict):
        raise AgentCoreRecoveryError(
            "control-plane scalable target is malformed"
        )
    for field in ("MinCapacity", "MaxCapacity"):
        value = target.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise AgentCoreRecoveryError(
                f"control-plane scalable target has invalid {field}"
            )
    suspended = target.get("SuspendedState") or {}
    if not isinstance(suspended, dict):
        raise AgentCoreRecoveryError(
            "control-plane suspension state is malformed"
        )
    target["SuspendedState"] = {
        key: suspended.get(key) is True for key in _SUSPENSION_KEYS
    }
    return target


def _service(
    aws: AwsCli,
    *,
    cluster_name: str,
    service_name: str,
) -> dict[str, Any]:
    response = aws.json(
        "ecs",
        "describe-services",
        "--cluster",
        cluster_name,
        "--services",
        service_name,
    )
    services = response.get("services")
    if (
        response.get("failures")
        or not isinstance(services, list)
        or len(services) != 1
        or not isinstance(services[0], dict)
    ):
        raise AgentCoreRecoveryError(
            "could not resolve exactly one control-plane service"
        )
    service = services[0]
    for field in ("desiredCount", "pendingCount", "runningCount"):
        value = service.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise AgentCoreRecoveryError(
                f"control-plane service has invalid {field}"
            )
    return service


def _service_stable(service: dict[str, Any], desired: int) -> bool:
    if (
        service.get("desiredCount") != desired
        or service.get("runningCount") != desired
        or service.get("pendingCount") != 0
    ):
        return False
    deployments = service.get("deployments", [])
    if not isinstance(deployments, list) or len(deployments) != 1:
        return False
    deployment = deployments[0]
    return (
        isinstance(deployment, dict)
        and deployment.get("status") == "PRIMARY"
        and deployment.get("runningCount", desired) == desired
        and deployment.get("pendingCount", 0) == 0
        and deployment.get("rolloutState", "COMPLETED") == "COMPLETED"
    )


def _wait_service(
    aws: AwsCli,
    *,
    cluster_name: str,
    service_name: str,
    desired: int,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        service = _service(
            aws,
            cluster_name=cluster_name,
            service_name=service_name,
        )
        if _service_stable(service, desired):
            return service
        sleep(poll_interval)
    raise AgentCoreRecoveryError(
        f"control plane did not stabilize at {desired} tasks"
    )


def _suspension_argument(state: dict[str, bool]) -> str:
    return ",".join(
        f"{key}={'true' if state.get(key) is True else 'false'}"
        for key in _SUSPENSION_KEYS
    )


def _register_scalable_target(
    aws: AwsCli,
    *,
    resource_id: str,
    minimum: int,
    maximum: int,
    suspended: dict[str, bool],
) -> None:
    aws.json(
        "application-autoscaling",
        "register-scalable-target",
        "--service-namespace",
        "ecs",
        "--resource-id",
        resource_id,
        "--scalable-dimension",
        "ecs:service:DesiredCount",
        "--min-capacity",
        str(minimum),
        "--max-capacity",
        str(maximum),
        "--suspended-state",
        _suspension_argument(suspended),
    )


def _update_desired_count(
    aws: AwsCli,
    *,
    cluster_name: str,
    service_name: str,
    desired: int,
) -> None:
    aws.json(
        "ecs",
        "update-service",
        "--cluster",
        cluster_name,
        "--service",
        service_name,
        "--desired-count",
        str(desired),
    )


def _control_plane_snapshot(
    aws: AwsCli,
) -> dict[str, Any] | None:
    resolved = _control_plane_runtime_outputs(aws)
    if resolved is None:
        return None
    stack, outputs = resolved
    cluster_name = outputs["ClusterName"]
    service_name = outputs["ServiceName"]
    target = _scalable_target(
        aws,
        cluster_name=cluster_name,
        service_name=service_name,
    )
    service = _service(
        aws,
        cluster_name=cluster_name,
        service_name=service_name,
    )
    return {
        "stackId": stack.get("StackId"),
        "clusterName": cluster_name,
        "serviceName": service_name,
        "resourceId": f"service/{cluster_name}/{service_name}",
        "desiredCount": service["desiredCount"],
        "pendingCount": service["pendingCount"],
        "runningCount": service["runningCount"],
        "minCapacity": target["MinCapacity"],
        "maxCapacity": target["MaxCapacity"],
        "suspendedState": target["SuspendedState"],
        "agentCoreStackName": outputs["AgentCoreStackName"],
        "primaryTable": outputs["PrimaryStateTableName"],
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
        "recoveryMode": outputs["RecoveryCutoverMode"],
        "approvalId": outputs["RecoveryApprovalId"],
    }


def _quiesce_control_plane(
    aws: AwsCli,
    snapshot: dict[str, Any] | None,
    *,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if snapshot is None:
        return
    _register_scalable_target(
        aws,
        resource_id=snapshot["resourceId"],
        minimum=0,
        maximum=snapshot["maxCapacity"],
        suspended={key: True for key in _SUSPENSION_KEYS},
    )
    _update_desired_count(
        aws,
        cluster_name=snapshot["clusterName"],
        service_name=snapshot["serviceName"],
        desired=0,
    )
    _wait_service(
        aws,
        cluster_name=snapshot["clusterName"],
        service_name=snapshot["serviceName"],
        desired=0,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )


def _assert_control_plane_quiesced(
    aws: AwsCli,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if snapshot is None:
        if _optional_stack(aws, _CONTROL_PLANE_STACK) is not None:
            raise AgentCoreRecoveryError(
                "a control-plane stack appeared after recovery began"
            )
        return None
    current = _control_plane_snapshot(aws)
    if current is None:
        raise AgentCoreRecoveryError(
            "the recorded control-plane stack disappeared"
        )
    for field in ("stackId", "clusterName", "serviceName", "resourceId"):
        if current.get(field) != snapshot.get(field):
            raise AgentCoreRecoveryError(
                "control plane no longer matches recovery evidence"
            )
    if (
        current["desiredCount"] != 0
        or current["pendingCount"] != 0
        or current["runningCount"] != 0
        or current["minCapacity"] != 0
        or not all(current["suspendedState"].values())
    ):
        raise AgentCoreRecoveryError(
            "control plane must remain stopped with scaling suspended"
        )
    return current


def _assert_control_plane_selector(
    aws: AwsCli,
    snapshot: dict[str, Any] | None,
    *,
    agentcore_stack_name: str,
    primary_table: str,
    selected_table: str,
    mode: str,
    approval_id: str,
) -> dict[str, Any] | None:
    current = _assert_control_plane_quiesced(aws, snapshot)
    if current is None:
        return None
    expected = {
        "agentCoreStackName": agentcore_stack_name,
        "primaryTable": primary_table,
        "selectedTable": selected_table,
        "recoveryMode": mode,
        "approvalId": approval_id,
    }
    actual = {name: current.get(name) for name in expected}
    if actual != expected:
        raise AgentCoreRecoveryError(
            "control-plane recovery selector does not match AgentCore: "
            f"expected {expected}, found {actual}"
        )
    return current


def _write_state(
    path: Path,
    state: dict[str, Any],
    *,
    exclusive: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    try:
        with path.open(mode, encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise AgentCoreRecoveryError(
            f"recovery state file already exists: {path}"
        ) from exc


def _load_state(
    path: Path,
    *,
    region: str,
    stack_name: str,
) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentCoreRecoveryError(
            f"could not read recovery state file: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentCoreRecoveryError(
            "recovery state file is invalid JSON"
        ) from exc
    if not isinstance(state, dict) or state.get("schemaVersion") != 2:
        raise AgentCoreRecoveryError("unsupported recovery state file")
    if state.get("region") != region or state.get("stackName") != stack_name:
        raise AgentCoreRecoveryError(
            "recovery state file does not match the selected stack and region"
        )
    return state


def _assert_runtime_ownership(
    state: dict[str, Any],
    stack: dict[str, Any],
    outputs: dict[str, str],
) -> None:
    if (
        state.get("stackId") != stack.get("StackId")
        or state.get("runtimeArn") != outputs["RuntimeArn"]
        or state.get("primaryTable") != outputs["StateTableName"]
        or state.get("dataKeyArn") != outputs["DataKeyArn"]
    ):
        raise AgentCoreRecoveryError(
            "recovery evidence does not match the deployed AgentCore runtime"
        )


def _validate_table(
    aws: AwsCli,
    *,
    primary_table: str,
    table_name: str,
    data_key_arn: str,
) -> None:
    expected_prefix = f"{primary_table}-restore-validation-"
    if table_name != primary_table and not table_name.startswith(
        expected_prefix
    ):
        raise AgentCoreRecoveryError(
            "target table is outside the AgentCore restore-validation namespace"
        )
    response = aws.json(
        "dynamodb",
        "describe-table",
        "--table-name",
        table_name,
    )
    table = response.get("Table")
    if not isinstance(table, dict) or table.get("TableName") != table_name:
        raise AgentCoreRecoveryError("target DynamoDB table is missing")
    if table.get("TableStatus") != "ACTIVE":
        raise AgentCoreRecoveryError("target DynamoDB table is not ACTIVE")
    if table.get("DeletionProtectionEnabled") is not True:
        raise AgentCoreRecoveryError(
            "target DynamoDB table lacks deletion protection"
        )
    encryption = table.get("SSEDescription", {})
    if encryption.get("Status") != "ENABLED":
        raise AgentCoreRecoveryError(
            "target DynamoDB table encryption is not enabled"
        )
    if encryption.get("KMSMasterKeyArn") != data_key_arn:
        raise AgentCoreRecoveryError(
            "target DynamoDB table is not encrypted by the AgentCore data key"
        )
    if table.get("KeySchema") != [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]:
        raise AgentCoreRecoveryError(
            "target DynamoDB table has an unexpected key schema"
        )
    backups = aws.json(
        "dynamodb",
        "describe-continuous-backups",
        "--table-name",
        table_name,
    )
    pitr = (
        backups.get("ContinuousBackupsDescription", {})
        .get("PointInTimeRecoveryDescription", {})
        .get("PointInTimeRecoveryStatus")
    )
    if pitr != "ENABLED":
        raise AgentCoreRecoveryError(
            "target DynamoDB table lacks point-in-time recovery"
        )
    ttl = aws.json(
        "dynamodb",
        "describe-time-to-live",
        "--table-name",
        table_name,
    ).get("TimeToLiveDescription", {})
    if (
        ttl.get("TimeToLiveStatus") != "ENABLED"
        or ttl.get("AttributeName") != "expires_at"
    ):
        raise AgentCoreRecoveryError(
            "target DynamoDB table lacks expires_at TTL"
        )


def _target_parameter(primary_table: str, table_name: str) -> str:
    return "" if table_name == primary_table else table_name


def _runtime_id(runtime_arn: str) -> str:
    marker = ":runtime/"
    if marker not in runtime_arn:
        raise AgentCoreRecoveryError("runtime ARN is malformed")
    runtime_id = runtime_arn.split(marker, 1)[1]
    if not runtime_id or "/" in runtime_id:
        raise AgentCoreRecoveryError("runtime ARN is malformed")
    return runtime_id


def _ready_endpoint(
    aws: AwsCli,
    *,
    runtime_arn: str,
    endpoint_name: str,
) -> dict[str, Any]:
    endpoint = aws.json(
        "bedrock-agentcore-control",
        "get-agent-runtime-endpoint",
        "--agent-runtime-id",
        _runtime_id(runtime_arn),
        "--endpoint-name",
        endpoint_name,
    )
    if endpoint.get("status") != "READY":
        raise AgentCoreRecoveryError(
            f"AgentCore {endpoint_name} endpoint is not READY"
        )
    if (
        not endpoint.get("liveVersion")
        or endpoint.get("liveVersion") != endpoint.get("targetVersion")
    ):
        raise AgentCoreRecoveryError(
            f"AgentCore {endpoint_name} endpoint version is not stable"
        )
    return endpoint


def quiesce(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    approval_id: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if _APPROVAL_ID.fullmatch(approval_id) is None:
        raise AgentCoreRecoveryError("approval ID is invalid")
    stack, outputs = _runtime_outputs(aws, stack_name)
    if state_file.exists():
        state = _load_state(
            state_file,
            region=region,
            stack_name=stack_name,
        )
        if state.get("phase") not in {
            "quiescing",
            "control-plane-quiesced",
            "quiesced",
        }:
            raise AgentCoreRecoveryError(
                "existing recovery state is not a retryable quiesce"
            )
        if state.get("approvalId") != approval_id:
            raise AgentCoreRecoveryError(
                "quiesce retry must use the recorded approval ID"
            )
        _assert_runtime_ownership(state, stack, outputs)
        expected_runtime_approval = {
            "normal": state.get("approvalBefore"),
            "quiesced": approval_id,
        }.get(outputs["RecoveryCutoverMode"])
        if (
            expected_runtime_approval is None
            or outputs["SelectedRuntimeStateTableName"]
            != state.get("selectedTableBefore")
            or outputs["RecoveryApprovalId"]
            != expected_runtime_approval
        ):
            raise AgentCoreRecoveryError(
                "deployed quiesce state does not match recovery evidence"
            )
        control_plane = state.get("controlPlane")
        if control_plane is not None and not isinstance(
            control_plane,
            dict,
        ):
            raise AgentCoreRecoveryError(
                "recorded control-plane recovery state is malformed"
            )
    else:
        if outputs["RecoveryCutoverMode"] != "normal":
            raise AgentCoreRecoveryError(
                "AgentCore must be normal before quiesce"
            )
        if approval_id == outputs["RecoveryApprovalId"]:
            raise AgentCoreRecoveryError(
                "quiesce requires a new approval ID"
            )
        control_plane = _control_plane_snapshot(aws)
        if control_plane is not None:
            expected_control = {
                "agentCoreStackName": stack_name,
                "primaryTable": outputs["StateTableName"],
                "selectedTable": outputs["SelectedRuntimeStateTableName"],
                "recoveryMode": "normal",
                "approvalId": outputs["RecoveryApprovalId"],
            }
            actual_control = {
                name: control_plane.get(name)
                for name in expected_control
            }
            if actual_control != expected_control:
                raise AgentCoreRecoveryError(
                    "control plane is not normal on the AgentCore-selected "
                    f"table: expected {expected_control}, found "
                    f"{actual_control}"
                )
        state = {
            "schemaVersion": 2,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "region": region,
            "stackName": stack_name,
            "stackId": stack.get("StackId"),
            "runtimeArn": outputs["RuntimeArn"],
            "primaryTable": outputs["StateTableName"],
            "dataKeyArn": outputs["DataKeyArn"],
            "selectedTableBefore": outputs[
                "SelectedRuntimeStateTableName"
            ],
            "approvalBefore": outputs["RecoveryApprovalId"],
            "approvalId": approval_id,
            "phase": "quiescing",
            "controlPlane": control_plane,
        }
        _write_state(state_file, state, exclusive=True)

    _quiesce_control_plane(
        aws,
        control_plane,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )
    if control_plane is not None:
        control_current = _assert_control_plane_quiesced(
            aws,
            control_plane,
        )
        if control_current is None:  # pragma: no cover - guarded above
            raise AgentCoreRecoveryError(
                "the recorded control-plane stack disappeared"
            )
        expected_control_approval = {
            "normal": state.get("approvalBefore"),
            "quiesced": approval_id,
        }.get(control_current["recoveryMode"])
        if (
            control_current["agentCoreStackName"] != stack_name
            or control_current["primaryTable"] != state.get("primaryTable")
            or control_current["selectedTable"]
            != state.get("selectedTableBefore")
            or expected_control_approval is None
            or control_current["approvalId"]
            != expected_control_approval
        ):
            raise AgentCoreRecoveryError(
                "deployed control-plane quiesce state does not match "
                "recovery evidence"
            )
        if control_current["recoveryMode"] == "normal":
            _, control_updated = _update_selector(
                aws,
                stack_name=_CONTROL_PLANE_STACK,
                mode="quiesced",
                target_parameter=_target_parameter(
                    control_plane["primaryTable"],
                    control_plane["selectedTable"],
                ),
                approval_id=approval_id,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
                sleep=sleep,
            )
            if (
                control_updated.get("RecoveryCutoverMode") != "quiesced"
                or control_updated.get("SelectedRuntimeStateTableName")
                != state.get("selectedTableBefore")
                or control_updated.get("RecoveryApprovalId") != approval_id
            ):
                raise AgentCoreRecoveryError(
                    "control plane did not enter the expected quiesced state"
                )
        _assert_control_plane_selector(
            aws,
            control_plane,
            agentcore_stack_name=stack_name,
            primary_table=state["primaryTable"],
            selected_table=state["selectedTableBefore"],
            mode="quiesced",
            approval_id=approval_id,
        )
        state["phase"] = "control-plane-quiesced"
        _write_state(state_file, state, exclusive=False)

    stack, current_outputs = _runtime_outputs(aws, stack_name)
    _assert_runtime_ownership(state, stack, current_outputs)
    expected_runtime_approval = {
        "normal": state.get("approvalBefore"),
        "quiesced": approval_id,
    }.get(current_outputs["RecoveryCutoverMode"])
    if (
        expected_runtime_approval is None
        or current_outputs["SelectedRuntimeStateTableName"]
        != state.get("selectedTableBefore")
        or current_outputs["RecoveryApprovalId"]
        != expected_runtime_approval
    ):
        raise AgentCoreRecoveryError(
            "deployed quiesce state does not match recovery evidence"
        )
    if current_outputs["RecoveryCutoverMode"] == "normal":
        _, updated = _update_selector(
            aws,
            stack_name=stack_name,
            mode="quiesced",
            target_parameter=_target_parameter(
                current_outputs["StateTableName"],
                current_outputs["SelectedRuntimeStateTableName"],
            ),
            approval_id=approval_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
    else:
        updated = current_outputs
    if (
        updated.get("RecoveryCutoverMode") != "quiesced"
        or updated.get("SelectedRuntimeStateTableName")
        != state.get("selectedTableBefore")
        or updated.get("RecoveryApprovalId") != approval_id
        or updated.get("RuntimeEndpointArn")
        or updated.get("RecoveryRuntimeEndpointArn")
    ):
        raise AgentCoreRecoveryError(
            "AgentCore did not enter the expected quiesced state"
        )
    state.update(
        phase="quiesced",
        quiescedAt=updated["RecoveryQuiescedAt"],
        minimumQuiescenceSeconds=int(
            updated["RecoveryMinimumQuiescenceSeconds"]
        ),
    )
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "quiesced",
        "stateFile": str(state_file),
        "approvalId": approval_id,
        "selectedTable": updated["SelectedRuntimeStateTableName"],
        "quiescedAt": updated["RecoveryQuiescedAt"],
        "minimumQuiescenceSeconds": state["minimumQuiescenceSeconds"],
        "controlPlaneQuiesced": control_plane is not None,
    }


def select(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    expected_table: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    state = _load_state(
        state_file,
        region=region,
        stack_name=stack_name,
    )
    phase = state.get("phase")
    if phase not in {
        "quiesced",
        "selecting",
        "control-plane-selected",
    }:
        raise AgentCoreRecoveryError(
            "select requires a completed quiesce or retryable selection phase"
        )
    if (
        phase == "quiesced"
        and expected_table == state.get("selectedTableBefore")
    ):
        raise AgentCoreRecoveryError(
            "recovery target must differ from the currently selected table"
        )
    if (
        phase != "quiesced"
        and state.get("targetTable") != expected_table
    ):
        raise AgentCoreRecoveryError(
            "select retry does not match the recorded recovery table"
        )
    stack, outputs = _runtime_outputs(aws, stack_name)
    _assert_runtime_ownership(state, stack, outputs)
    runtime_mode = outputs["RecoveryCutoverMode"]
    runtime_selected = outputs["SelectedRuntimeStateTableName"]
    expected_runtime_table = {
        "quiesced": state.get("selectedTableBefore"),
        "selected": expected_table,
    }.get(runtime_mode)
    if (
        expected_runtime_table is None
        or runtime_selected != expected_runtime_table
        or outputs["RecoveryApprovalId"] != state.get("approvalId")
    ):
        raise AgentCoreRecoveryError(
            "deployed recovery selection does not match recovery evidence"
        )
    _validate_table(
        aws,
        primary_table=outputs["StateTableName"],
        table_name=expected_table,
        data_key_arn=outputs["DataKeyArn"],
    )
    if phase == "quiesced":
        state.update(
            phase="selecting",
            targetTable=expected_table,
            selectionStartedAt=datetime.now(timezone.utc).isoformat(),
        )
        _write_state(state_file, state, exclusive=False)
    control_plane = _assert_control_plane_quiesced(
        aws,
        state.get("controlPlane"),
    )
    if control_plane is not None:
        expected_control_table = {
            "quiesced": state.get("selectedTableBefore"),
            "selected": expected_table,
        }.get(control_plane["recoveryMode"])
        if (
            control_plane["agentCoreStackName"] != stack_name
            or control_plane["primaryTable"] != outputs["StateTableName"]
            or control_plane["approvalId"] != state["approvalId"]
            or expected_control_table is None
            or control_plane["selectedTable"] != expected_control_table
        ):
            raise AgentCoreRecoveryError(
                "control-plane selection does not match recovery evidence"
            )
        if (
            runtime_mode == "selected"
            and control_plane["recoveryMode"] != "selected"
        ):
            raise AgentCoreRecoveryError(
                "AgentCore cannot be selected before the control plane"
            )
    if (
        control_plane is not None
        and control_plane["recoveryMode"] == "quiesced"
    ):
        _, control_updated = _update_selector(
            aws,
            stack_name=_CONTROL_PLANE_STACK,
            mode="selected",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                expected_table,
            ),
            approval_id=state["approvalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
        if (
            control_updated.get("RecoveryCutoverMode") != "selected"
            or control_updated.get("SelectedRuntimeStateTableName")
            != expected_table
            or control_updated.get("RecoveryApprovalId")
            != state["approvalId"]
        ):
            raise AgentCoreRecoveryError(
                "control plane did not enter the expected selected state"
            )
        control_plane = _assert_control_plane_selector(
            aws,
            state["controlPlane"],
            agentcore_stack_name=stack_name,
            primary_table=outputs["StateTableName"],
            selected_table=expected_table,
            mode="selected",
            approval_id=state["approvalId"],
        )
        state.update(
            phase="control-plane-selected",
            controlPlaneSelectedAt=(
                datetime.now(timezone.utc).isoformat()
            ),
        )
        _write_state(state_file, state, exclusive=False)
    if runtime_mode == "quiesced":
        _, updated = _update_selector(
            aws,
            stack_name=stack_name,
            mode="selected",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                expected_table,
            ),
            approval_id=state["approvalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
    else:
        updated = outputs
    if (
        updated.get("RecoveryCutoverMode") != "selected"
        or updated.get("SelectedRuntimeStateTableName") != expected_table
        or updated.get("RuntimeEndpointArn")
        or updated.get("RecoveryRuntimeEndpointArn")
    ):
        raise AgentCoreRecoveryError(
            "AgentCore did not enter the expected selected state"
        )
    state.update(
        phase="selected",
        selectedAt=datetime.now(timezone.utc).isoformat(),
        targetTable=expected_table,
    )
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "selected",
        "stateFile": str(state_file),
        "approvalId": state["approvalId"],
        "selectedTable": expected_table,
        "stateAccessBlocked": True,
    }


def start(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    expected_table: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    state = _load_state(
        state_file,
        region=region,
        stack_name=stack_name,
    )
    if (
        state.get("phase") not in {"selected", "starting-validation"}
        or state.get("targetTable") != expected_table
    ):
        raise AgentCoreRecoveryError(
            "start requires the recorded selected or retryable recovery table"
        )
    stack, outputs = _runtime_outputs(aws, stack_name)
    _assert_runtime_ownership(state, stack, outputs)
    _assert_control_plane_selector(
        aws,
        state.get("controlPlane"),
        agentcore_stack_name=stack_name,
        primary_table=outputs["StateTableName"],
        selected_table=expected_table,
        mode="selected",
        approval_id=state["approvalId"],
    )
    runtime_mode = outputs["RecoveryCutoverMode"]
    if (
        runtime_mode not in {"selected", "validation"}
        or outputs["SelectedRuntimeStateTableName"] != expected_table
        or outputs["RecoveryApprovalId"] != state.get("approvalId")
    ):
        raise AgentCoreRecoveryError(
            "deployed selected state does not match recovery evidence"
        )
    if runtime_mode == "selected":
        state.update(
            phase="starting-validation",
            validationRequestedAt=datetime.now(timezone.utc).isoformat(),
        )
        _write_state(state_file, state, exclusive=False)
        _, updated = _update_selector(
            aws,
            stack_name=stack_name,
            mode="validation",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                expected_table,
            ),
            approval_id=state["approvalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
    else:
        updated = outputs
    if (
        updated.get("RecoveryCutoverMode") != "validation"
        or updated.get("SelectedRuntimeStateTableName") != expected_table
        or not updated.get("RecoveryRuntimeEndpointArn")
        or updated.get("RuntimeEndpointArn")
    ):
        raise AgentCoreRecoveryError(
            "AgentCore did not enter recovery validation mode"
        )
    endpoint = _ready_endpoint(
        aws,
        runtime_arn=updated["RuntimeArn"],
        endpoint_name="recovery",
    )
    state.update(
        phase="validation",
        validationStartedAt=datetime.now(timezone.utc).isoformat(),
        recoveryEndpointArn=updated["RecoveryRuntimeEndpointArn"],
    )
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "validation",
        "stateFile": str(state_file),
        "approvalId": state["approvalId"],
        "selectedTable": expected_table,
        "endpointArn": updated["RecoveryRuntimeEndpointArn"],
        "runtimeVersion": endpoint["liveVersion"],
        "controlPlaneQuiesced": state.get("controlPlane") is not None,
    }


def promote(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    expected_table: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    state = _load_state(
        state_file,
        region=region,
        stack_name=stack_name,
    )
    if (
        state.get("phase") not in {"validation", "promoting"}
        or state.get("targetTable") != expected_table
    ):
        raise AgentCoreRecoveryError(
            "promote requires the recorded validated or retryable table"
        )
    stack, outputs = _runtime_outputs(aws, stack_name)
    _assert_runtime_ownership(state, stack, outputs)
    _assert_control_plane_selector(
        aws,
        state.get("controlPlane"),
        agentcore_stack_name=stack_name,
        primary_table=outputs["StateTableName"],
        selected_table=expected_table,
        mode="selected",
        approval_id=state["approvalId"],
    )
    runtime_mode = outputs["RecoveryCutoverMode"]
    if (
        runtime_mode not in {"validation", "normal"}
        or outputs["SelectedRuntimeStateTableName"] != expected_table
        or outputs["RecoveryApprovalId"] != state.get("approvalId")
    ):
        raise AgentCoreRecoveryError(
            "deployed validation state does not match recovery evidence"
        )
    if runtime_mode == "validation":
        _ready_endpoint(
            aws,
            runtime_arn=outputs["RuntimeArn"],
            endpoint_name="recovery",
        )
        state.update(
            phase="promoting",
            promotionRequestedAt=datetime.now(timezone.utc).isoformat(),
        )
        _write_state(state_file, state, exclusive=False)
        _, updated = _update_selector(
            aws,
            stack_name=stack_name,
            mode="normal",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                expected_table,
            ),
            approval_id=state["approvalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
    else:
        updated = outputs
    if (
        updated.get("RecoveryCutoverMode") != "normal"
        or updated.get("SelectedRuntimeStateTableName") != expected_table
        or not updated.get("RuntimeEndpointArn")
        or updated.get("RecoveryRuntimeEndpointArn")
    ):
        raise AgentCoreRecoveryError(
            "AgentCore did not promote the selected table"
        )
    endpoint = _ready_endpoint(
        aws,
        runtime_arn=updated["RuntimeArn"],
        endpoint_name="production",
    )
    state.update(
        phase="promoted",
        promotedAt=datetime.now(timezone.utc).isoformat(),
        productionEndpointArn=updated["RuntimeEndpointArn"],
    )
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "promoted",
        "stateFile": str(state_file),
        "approvalId": state["approvalId"],
        "selectedTable": expected_table,
        "endpointArn": updated["RuntimeEndpointArn"],
        "runtimeVersion": endpoint["liveVersion"],
        "controlPlaneRemainsQuiesced": state.get("controlPlane") is not None,
    }


def abort(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    state = _load_state(
        state_file,
        region=region,
        stack_name=stack_name,
    )
    state_phase = state.get("phase")
    if state_phase not in {
        "quiescing",
        "control-plane-quiesced",
        "quiesced",
        "selecting",
        "control-plane-selected",
        "selected",
        "starting-validation",
    }:
        raise AgentCoreRecoveryError(
            "abort is allowed only before validation starts"
        )
    if state_phase in {"quiescing", "control-plane-quiesced"}:
        _quiesce_control_plane(
            aws,
            state.get("controlPlane"),
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
    stack, outputs = _runtime_outputs(aws, stack_name)
    _assert_runtime_ownership(state, stack, outputs)
    selected_before = state["selectedTableBefore"]
    target_table = state.get("targetTable")
    runtime_mode = outputs["RecoveryCutoverMode"]
    expected_runtime_table = {
        "normal": selected_before,
        "quiesced": selected_before,
        "selected": target_table,
    }.get(runtime_mode)
    expected_runtime_approval = (
        state["approvalBefore"]
        if runtime_mode == "normal"
        else state["approvalId"]
    )
    if (
        expected_runtime_table is None
        or outputs["SelectedRuntimeStateTableName"]
        != expected_runtime_table
        or outputs["RecoveryApprovalId"]
        != expected_runtime_approval
    ):
        if runtime_mode == "validation":
            raise AgentCoreRecoveryError(
                "validation already started; use the full rollback flow"
            )
        raise AgentCoreRecoveryError(
            "deployed pre-validation state does not match recovery evidence"
        )

    control_plane = _assert_control_plane_quiesced(
        aws,
        state.get("controlPlane"),
    )
    if control_plane is not None:
        expected_control_table = {
            "normal": selected_before,
            "quiesced": selected_before,
            "selected": target_table,
        }.get(control_plane["recoveryMode"])
        expected_control_approval = (
            state["approvalBefore"]
            if control_plane["recoveryMode"] == "normal"
            else state["approvalId"]
        )
        if (
            control_plane["agentCoreStackName"] != stack_name
            or control_plane["primaryTable"] != outputs["StateTableName"]
            or expected_control_table is None
            or control_plane["selectedTable"] != expected_control_table
            or control_plane["approvalId"]
            != expected_control_approval
        ):
            raise AgentCoreRecoveryError(
                "control-plane pre-validation state does not match recovery "
                "evidence"
            )
        safe_orderings = {
            ("normal", "normal"),
            ("normal", "quiesced"),
            ("quiesced", "quiesced"),
            ("quiesced", "selected"),
            ("selected", "selected"),
        }
        if (
            runtime_mode,
            control_plane["recoveryMode"],
        ) not in safe_orderings:
            raise AgentCoreRecoveryError(
                "the two planes are in an unsafe recovery phase ordering"
            )

    if runtime_mode == "selected":
        _, outputs = _update_selector(
            aws,
            stack_name=stack_name,
            mode="quiesced",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                selected_before,
            ),
            approval_id=state["approvalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
        if (
            outputs.get("RecoveryCutoverMode") != "quiesced"
            or outputs.get("SelectedRuntimeStateTableName")
            != selected_before
        ):
            raise AgentCoreRecoveryError(
                "AgentCore did not reverse its selected table while blocked"
            )
        runtime_mode = "quiesced"

    control_plane = _assert_control_plane_quiesced(
        aws,
        state.get("controlPlane"),
    )
    if (
        control_plane is not None
        and control_plane["recoveryMode"] == "selected"
    ):
        _, control_updated = _update_selector(
            aws,
            stack_name=_CONTROL_PLANE_STACK,
            mode="quiesced",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                selected_before,
            ),
            approval_id=state["approvalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
        if (
            control_updated.get("RecoveryCutoverMode") != "quiesced"
            or control_updated.get("SelectedRuntimeStateTableName")
            != selected_before
        ):
            raise AgentCoreRecoveryError(
                "control plane did not reverse its selected table while "
                "blocked"
            )

    if runtime_mode == "quiesced":
        _, updated = _update_selector(
            aws,
            stack_name=stack_name,
            mode="normal",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                selected_before,
            ),
            approval_id=state["approvalBefore"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
    else:
        updated = outputs
    if (
        updated.get("RecoveryCutoverMode") != "normal"
        or updated.get("SelectedRuntimeStateTableName")
        != selected_before
    ):
        raise AgentCoreRecoveryError(
            "AgentCore did not return to its pre-recovery selector"
        )
    state.update(
        phase="aborted",
        abortedAt=datetime.now(timezone.utc).isoformat(),
    )
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "aborted",
        "stateFile": str(state_file),
        "selectedTable": updated["SelectedRuntimeStateTableName"],
        "controlPlaneRemainsQuiesced": state.get("controlPlane") is not None,
    }


def resume_control_plane(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    state = _load_state(
        state_file,
        region=region,
        stack_name=stack_name,
    )
    if state.get("phase") not in {"promoted", "aborted"}:
        raise AgentCoreRecoveryError(
            "control plane may resume only after promote or abort completes"
        )
    stack, outputs = _runtime_outputs(aws, stack_name)
    _assert_runtime_ownership(state, stack, outputs)
    if outputs["RecoveryCutoverMode"] != "normal":
        raise AgentCoreRecoveryError(
            "control plane may resume only after AgentCore is normal"
        )
    snapshot = state.get("controlPlane")
    if snapshot is None:
        return {
            "phase": "control-plane-not-deployed",
            "stateFile": str(state_file),
        }
    current = _control_plane_snapshot(aws)
    if current is None:
        raise AgentCoreRecoveryError(
            "the recorded control-plane stack disappeared"
        )
    for field in ("stackId", "clusterName", "serviceName", "resourceId"):
        if current.get(field) != snapshot.get(field):
            raise AgentCoreRecoveryError(
                "control plane no longer matches recovery evidence"
            )
    if (
        current["agentCoreStackName"] != stack_name
        or current["primaryTable"] != outputs["StateTableName"]
        or current["selectedTable"]
        != outputs["SelectedRuntimeStateTableName"]
        or current["recoveryMode"]
        not in {"normal", "quiesced", "selected"}
    ):
        raise AgentCoreRecoveryError(
            "both planes must use the same selected table and ownership "
            "before the control plane resumes"
        )
    if (
        current["recoveryMode"] in {"normal", "selected"}
        and current["approvalId"] != outputs["RecoveryApprovalId"]
    ):
        raise AgentCoreRecoveryError(
            "AgentCore and control plane have different "
            "recovery approvals"
        )
    if current["recoveryMode"] in {"quiesced", "selected"}:
        _assert_control_plane_quiesced(aws, snapshot)
    desired = snapshot.get("desiredCount")
    if (
        not isinstance(desired, int)
        or isinstance(desired, bool)
        or desired < 1
    ):
        raise AgentCoreRecoveryError(
            "recorded control-plane desired count is invalid"
        )
    if current["recoveryMode"] != "normal":
        _, control_updated = _update_selector(
            aws,
            stack_name=_CONTROL_PLANE_STACK,
            mode="normal",
            target_parameter=_target_parameter(
                outputs["StateTableName"],
                outputs["SelectedRuntimeStateTableName"],
            ),
            approval_id=outputs["RecoveryApprovalId"],
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
        )
        if (
            control_updated.get("RecoveryCutoverMode") != "normal"
            or control_updated.get("SelectedRuntimeStateTableName")
            != outputs["SelectedRuntimeStateTableName"]
            or control_updated.get("RecoveryApprovalId")
            != outputs["RecoveryApprovalId"]
        ):
            raise AgentCoreRecoveryError(
                "control plane did not resume on the AgentCore-selected table"
            )
    _update_desired_count(
        aws,
        cluster_name=snapshot["clusterName"],
        service_name=snapshot["serviceName"],
        desired=desired,
    )
    _wait_service(
        aws,
        cluster_name=snapshot["clusterName"],
        service_name=snapshot["serviceName"],
        desired=desired,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )
    _register_scalable_target(
        aws,
        resource_id=snapshot["resourceId"],
        minimum=snapshot["minCapacity"],
        maximum=snapshot["maxCapacity"],
        suspended=snapshot["suspendedState"],
    )
    state.update(
        controlPlaneResumedAt=datetime.now(timezone.utc).isoformat(),
        phase="complete",
    )
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "control-plane-resumed",
        "stateFile": str(state_file),
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
        "desiredCount": desired,
        "minCapacity": snapshot["minCapacity"],
        "maxCapacity": snapshot["maxCapacity"],
        "suspendedState": snapshot["suspendedState"],
    }


def status(
    aws: AwsCli,
    *,
    stack_name: str,
) -> dict[str, Any]:
    _, outputs = _runtime_outputs(aws, stack_name)
    endpoint_name = {
        "normal": "production",
        "validation": "recovery",
    }.get(outputs["RecoveryCutoverMode"])
    endpoint = None
    if endpoint_name is not None:
        endpoint = _ready_endpoint(
            aws,
            runtime_arn=outputs["RuntimeArn"],
            endpoint_name=endpoint_name,
        )
    control = _control_plane_snapshot(aws)
    return {
        "phase": "status",
        "approvalId": outputs["RecoveryApprovalId"],
        "mode": outputs["RecoveryCutoverMode"],
        "primaryTable": outputs["StateTableName"],
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
        "quiescedAt": outputs["RecoveryQuiescedAt"],
        "minimumQuiescenceSeconds": int(
            outputs["RecoveryMinimumQuiescenceSeconds"]
        ),
        "endpoint": (
            None
            if endpoint is None
            else {
                "name": endpoint_name,
                "arn": endpoint["agentRuntimeEndpointArn"],
                "status": endpoint["status"],
                "version": endpoint["liveVersion"],
            }
        ),
        "controlPlane": control,
    }


def cleanup_restore(
    aws: AwsCli,
    *,
    stack_name: str,
    table_name: str,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    _, outputs = _runtime_outputs(aws, stack_name)
    prefix = f"{outputs['StateTableName']}-restore-validation-"
    if not table_name.startswith(prefix):
        raise AgentCoreRecoveryError(
            "cleanup target is outside the AgentCore restore namespace"
        )
    if outputs["SelectedRuntimeStateTableName"] == table_name:
        raise AgentCoreRecoveryError(
            "cannot delete the table currently selected by AgentCore"
        )
    if outputs["RecoveryCutoverMode"] != "normal":
        raise AgentCoreRecoveryError(
            "cleanup requires AgentCore normal mode"
        )
    control_plane = _control_plane_snapshot(aws)
    if control_plane is not None:
        if control_plane["selectedTable"] == table_name:
            raise AgentCoreRecoveryError(
                "cannot delete the table selected by the control plane"
            )
        if (
            control_plane["recoveryMode"] != "normal"
            or control_plane["selectedTable"]
            != outputs["SelectedRuntimeStateTableName"]
        ):
            raise AgentCoreRecoveryError(
                "cleanup requires both planes normal on the same table"
            )
    table = aws.json(
        "dynamodb",
        "describe-table",
        "--table-name",
        table_name,
    ).get("Table")
    if (
        not isinstance(table, dict)
        or table.get("TableName") != table_name
        or table.get("TableStatus") != "ACTIVE"
    ):
        raise AgentCoreRecoveryError(
            "cleanup target is not the expected ACTIVE table"
        )
    if table.get("DeletionProtectionEnabled") is True:
        aws.json(
            "dynamodb",
            "update-table",
            "--table-name",
            table_name,
            "--no-deletion-protection-enabled",
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            table = aws.json(
                "dynamodb",
                "describe-table",
                "--table-name",
                table_name,
            ).get("Table")
            if (
                isinstance(table, dict)
                and table.get("TableStatus") == "ACTIVE"
                and table.get("DeletionProtectionEnabled") is False
            ):
                break
            sleep(poll_interval)
        else:
            raise AgentCoreRecoveryError(
                "restore table did not disable deletion protection"
            )
    aws.json(
        "dynamodb",
        "delete-table",
        "--table-name",
        table_name,
    )
    return {
        "phase": "cleanup-started",
        "tableName": table_name,
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--stack-name",
        default="AxonLLMAgentCoreStack",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-interval", type=float, default=10)
    subparsers = parser.add_subparsers(dest="command", required=True)
    state_help = "0600 JSON evidence/state file for this recovery"

    quiesce_parser = subparsers.add_parser("quiesce")
    quiesce_parser.add_argument(
        "--state-file",
        required=True,
        type=Path,
        help=state_help,
    )
    quiesce_parser.add_argument("--approval-id", required=True)

    for name in ("select", "start", "promote"):
        phase_parser = subparsers.add_parser(name)
        phase_parser.add_argument(
            "--state-file",
            required=True,
            type=Path,
            help=state_help,
        )
        phase_parser.add_argument("--expected-table", required=True)

    abort_parser = subparsers.add_parser("abort")
    abort_parser.add_argument(
        "--state-file",
        required=True,
        type=Path,
        help=state_help,
    )

    resume_parser = subparsers.add_parser("resume-control-plane")
    resume_parser.add_argument(
        "--state-file",
        required=True,
        type=Path,
        help=state_help,
    )

    subparsers.add_parser("status")
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--table-name", required=True)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.timeout_seconds < 1 or args.poll_interval < 0:
        parser.error(
            "timeout must be positive and poll interval non-negative"
        )
    aws = AwsCli(args.region)
    common = {
        "region": args.region,
        "stack_name": args.stack_name,
        "timeout_seconds": args.timeout_seconds,
        "poll_interval": args.poll_interval,
    }
    try:
        if args.command == "quiesce":
            result = quiesce(
                aws,
                state_file=args.state_file,
                approval_id=args.approval_id,
                **common,
            )
        elif args.command == "select":
            result = select(
                aws,
                state_file=args.state_file,
                expected_table=args.expected_table,
                **common,
            )
        elif args.command == "start":
            result = start(
                aws,
                state_file=args.state_file,
                expected_table=args.expected_table,
                **common,
            )
        elif args.command == "promote":
            result = promote(
                aws,
                state_file=args.state_file,
                expected_table=args.expected_table,
                **common,
            )
        elif args.command == "abort":
            result = abort(
                aws,
                state_file=args.state_file,
                **common,
            )
        elif args.command == "resume-control-plane":
            result = resume_control_plane(
                aws,
                state_file=args.state_file,
                **common,
            )
        elif args.command == "status":
            result = status(
                aws,
                stack_name=args.stack_name,
            )
        else:
            result = cleanup_restore(
                aws,
                stack_name=args.stack_name,
                table_name=args.table_name,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
    except (AgentCoreRecoveryError, AwsError) as exc:
        print(f"AgentCore recovery operation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
