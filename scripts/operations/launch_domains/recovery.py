"""Bounded, reviewed AgentCore state recovery rehearsal."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker
from launch_domains import common


OPERATIONS = framework.DOMAIN_OPERATIONS["recovery"]
AWS_TIMEOUT_SECONDS = 8.0
_BROKER_VERSION_ENV = "AXON_QUALIFICATION_MUTATION_BROKER_VERSION_ARN"
_MAX_BROKER_RESPONSE_BYTES = 8 * 1024
_TABLE_ARN = re.compile(
    r"^arn:aws:dynamodb:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"table/(?P<name>[A-Za-z0-9_.-]{3,255})$"
)
_STACK_ARN = re.compile(
    r"^arn:aws:cloudformation:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):stack/"
    r"(?P<name>[A-Za-z][A-Za-z0-9-]{0,127})/"
    r"(?P<id>[A-Za-z0-9-]{8,64})$"
)
_BROKER_VERSION_ARN = re.compile(
    r"^arn:aws:lambda:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"function:axonllm-qualification-selector-mutation-broker:"
    r"(?P<version>[1-9][0-9]*)$"
)
_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)


def _call(
    context: worker.HandlerContext,
    service: str,
    operation: str,
    parameters: Mapping[str, Any],
) -> Mapping[str, Any]:
    context.cancellation.raise_if_cancelled()
    return context.aws.call(
        service,
        operation,
        region=context.region,
        parameters=parameters,
        timeout_seconds=AWS_TIMEOUT_SECONDS,
    )


def _resource_binding(
    task: worker.ActionTask,
    context: worker.HandlerContext,
) -> dict[str, str]:
    parameters = task.payload.get("parameters")
    binding = task.payload.get("binding")
    if not isinstance(parameters, Mapping) or not isinstance(binding, Mapping):
        raise worker.HandlerContractError from None
    values = {
        "primaryTableArn": parameters.get("primaryTableArn"),
        "restoredTableArn": parameters.get("restoredTableArn"),
        "agentcoreStackArn": binding.get("agentcoreStackArn"),
        "controlPlaneStackArn": binding.get("controlPlaneStackArn"),
        "runtimeArn": binding.get("runtimeArn"),
    }
    if any(not isinstance(value, str) for value in values.values()):
        raise worker.HandlerContractError from None
    primary = _TABLE_ARN.fullmatch(values["primaryTableArn"])
    restored = _TABLE_ARN.fullmatch(values["restoredTableArn"])
    agent = _STACK_ARN.fullmatch(values["agentcoreStackArn"])
    control = _STACK_ARN.fullmatch(values["controlPlaneStackArn"])
    if (
        primary is None
        or restored is None
        or agent is None
        or control is None
        or {
            primary.group("region"),
            restored.group("region"),
            agent.group("region"),
            control.group("region"),
        }
        != {context.region}
        or len(
            {
                primary.group("account"),
                restored.group("account"),
                agent.group("account"),
                control.group("account"),
            }
        )
        != 1
        or not restored.group("name").startswith(
            f"{primary.group('name')}-restore-validation-"
        )
    ):
        raise worker.HandlerContractError from None
    return {
        **values,
        "primaryTableName": primary.group("name"),
        "restoredTableName": restored.group("name"),
        "agentcoreStackName": agent.group("name"),
        "controlPlaneStackName": control.group("name"),
    }


def _stack(
    context: worker.HandlerContext,
    stack_arn: str,
) -> tuple[Mapping[str, Any], dict[str, str]]:
    response = _call(
        context,
        "cloudformation",
        "describe_stacks",
        {"StackName": stack_arn},
    )
    stacks = response.get("Stacks")
    if type(stacks) is not list or len(stacks) != 1:
        raise worker.DomainTaskFailure("RecoveryStackUnavailable", retryable=True)
    stack = stacks[0]
    if not isinstance(stack, Mapping) or stack.get("StackId") != stack_arn:
        raise worker.DomainTaskFailure("RecoveryStackBindingMismatch")
    outputs: dict[str, str] = {}
    raw_outputs = stack.get("Outputs")
    if type(raw_outputs) is not list:
        raise worker.DomainTaskFailure("RecoveryStackOutputUnavailable", retryable=True)
    for item in raw_outputs:
        if not isinstance(item, Mapping):
            raise worker.DomainTaskFailure("RecoveryStackOutputUnavailable", retryable=True)
        name = item.get("OutputKey")
        value = item.get("OutputValue")
        if not isinstance(name, str) or not isinstance(value, str) or name in outputs:
            raise worker.DomainTaskFailure("RecoveryStackOutputUnavailable", retryable=True)
        outputs[name] = value
    return stack, outputs


def _required_outputs(
    outputs: Mapping[str, str],
    names: set[str],
) -> None:
    if any(name not in outputs for name in names):
        raise worker.DomainTaskFailure("RecoveryStackOutputUnavailable", retryable=True)


def _selector_edge(
    *,
    mode: str,
    selected_table: str,
    primary_table: str,
) -> str:
    suffix = "primary" if selected_table == primary_table else "restored"
    if mode == "quiesced":
        return f"quiesce-{suffix}"
    if mode in {"selected", "validation"}:
        return f"cutover-to-{suffix}"
    if mode == "normal":
        return f"resume-{suffix}"
    raise worker.HandlerContractError from None


def _broker_payload(response: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = response.get("Payload")
    try:
        raw = payload.read(_MAX_BROKER_RESPONSE_BYTES + 1) if hasattr(payload, "read") else payload
    except Exception as exc:
        raise worker.DomainTaskFailure(
            "RecoveryMutationBrokerUnavailable",
            retryable=True,
        ) from exc
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BROKER_RESPONSE_BYTES:
        raise worker.DomainTaskFailure("RecoveryMutationBrokerInvalidResponse")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise worker.DomainTaskFailure("RecoveryMutationBrokerInvalidResponse") from exc
    if type(value) is not dict or set(value) != {"status"}:
        raise worker.DomainTaskFailure("RecoveryMutationBrokerInvalidResponse")
    return value


def _update_selector(
    context: worker.HandlerContext,
    *,
    state: Mapping[str, Any],
    stack_arn: str,
    mode: str,
    selected_table: str,
    primary_table: str,
) -> None:
    stack = _STACK_ARN.fullmatch(stack_arn)
    broker_arn = os.environ.get(_BROKER_VERSION_ENV)
    broker = _BROKER_VERSION_ARN.fullmatch(broker_arn or "")
    owner_id = state.get("authorizationOwnerId")
    stored_fence = state.get("authorizationFenceToken")
    fence_token = context.fence_token if context.fence_token is not None else stored_fence
    if (
        stack is None
        or broker is None
        or broker.group("region") != context.region
        or broker.group("region") != stack.group("region")
        or broker.group("account") != stack.group("account")
        or not isinstance(owner_id, str)
        or worker.SHA256.fullmatch(owner_id) is None
        or isinstance(fence_token, bool)
        or not isinstance(fence_token, int)
        or fence_token < 1
    ):
        raise worker.HandlerContractError from None
    stack_kind = {
        "AxonLLMAgentCoreStack-managed": "agentcore",
        "AxonLLMControlPlaneStack-managed": "control-plane",
    }.get(stack.group("name"))
    if stack_kind is None:
        raise worker.HandlerContractError from None
    legal_edge = _selector_edge(
        mode=mode,
        selected_table=selected_table,
        primary_table=primary_table,
    )
    authorization_id = f"{owner_id}:{fence_token}:{stack_kind}:{legal_edge}"
    event = {
        "schema": "axonllm.qualification-selector-mutation",
        "version": 1,
        "authorizationId": authorization_id,
        "ownerId": owner_id,
        "fenceToken": fence_token,
        "stackKind": stack_kind,
        "legalEdge": legal_edge,
    }
    response = _call(
        context,
        "lambda",
        "invoke",
        {
            "FunctionName": broker_arn,
            "InvocationType": "RequestResponse",
            "Payload": json.dumps(
                event,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii"),
        },
    )
    if (
        response.get("StatusCode") != 200
        or response.get("FunctionError") is not None
        or response.get("ExecutedVersion") != broker.group("version")
    ):
        raise worker.DomainTaskFailure(
            "RecoveryMutationBrokerUnavailable",
            retryable=True,
        )
    result = _broker_payload(response)
    if result["status"] == "COMPLETE":
        return
    if result["status"] == "PENDING":
        raise worker.DomainTaskFailure("RecoveryStackUpdateInProgress", retryable=True)
    raise worker.DomainTaskFailure("RecoveryMutationBrokerInvalidResponse")


def _describe_table(
    context: worker.HandlerContext,
    *,
    name: str,
    arn: str,
) -> Mapping[str, Any]:
    response = _call(
        context,
        "dynamodb",
        "describe_table",
        {"TableName": name},
    )
    table = response.get("Table")
    if (
        not isinstance(table, Mapping)
        or table.get("TableName") != name
        or table.get("TableArn") != arn
    ):
        raise worker.DomainTaskFailure("RecoveryTableBindingMismatch")
    return table


def _validate_restored_table(
    context: worker.HandlerContext,
    *,
    resources: Mapping[str, str],
    data_key_arn: str,
) -> None:
    table = _describe_table(
        context,
        name=resources["restoredTableName"],
        arn=resources["restoredTableArn"],
    )
    if table.get("TableStatus") != "ACTIVE":
        raise worker.DomainTaskFailure("RecoveryRestoreInProgress", retryable=True)
    if table.get("KeySchema") != [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]:
        raise worker.DomainTaskFailure("RecoveryRestoreSchemaMismatch")
    encryption = table.get("SSEDescription")
    if (
        not isinstance(encryption, Mapping)
        or encryption.get("Status") != "ENABLED"
        or encryption.get("KMSMasterKeyArn") != data_key_arn
    ):
        raise worker.DomainTaskFailure("RecoveryRestoreEncryptionMismatch")
    backups = _call(
        context,
        "dynamodb",
        "describe_continuous_backups",
        {"TableName": resources["restoredTableName"]},
    )
    pitr = backups.get("ContinuousBackupsDescription")
    pitr = (
        pitr.get("PointInTimeRecoveryDescription")
        if isinstance(pitr, Mapping)
        else None
    )
    if not isinstance(pitr, Mapping) or pitr.get("PointInTimeRecoveryStatus") != "ENABLED":
        _call(
            context,
            "dynamodb",
            "update_continuous_backups",
            {
                "TableName": resources["restoredTableName"],
                "PointInTimeRecoverySpecification": {
                    "PointInTimeRecoveryEnabled": True
                },
            },
        )
        raise worker.DomainTaskFailure("RecoveryRestoreProtectionInProgress", retryable=True)
    ttl_response = _call(
        context,
        "dynamodb",
        "describe_time_to_live",
        {"TableName": resources["restoredTableName"]},
    )
    ttl = ttl_response.get("TimeToLiveDescription")
    if (
        not isinstance(ttl, Mapping)
        or ttl.get("TimeToLiveStatus") != "ENABLED"
        or ttl.get("AttributeName") != "expires_at"
    ):
        if not isinstance(ttl, Mapping) or ttl.get("TimeToLiveStatus") not in {
            "ENABLING",
            "ENABLED",
        }:
            _call(
                context,
                "dynamodb",
                "update_time_to_live",
                {
                    "TableName": resources["restoredTableName"],
                    "TimeToLiveSpecification": {
                        "Enabled": True,
                        "AttributeName": "expires_at",
                    },
                },
            )
        raise worker.DomainTaskFailure("RecoveryRestoreProtectionInProgress", retryable=True)
    if table.get("DeletionProtectionEnabled") is not True:
        _call(
            context,
            "dynamodb",
            "update_table",
            {
                "TableName": resources["restoredTableName"],
                "DeletionProtectionEnabled": True,
            },
        )
        raise worker.DomainTaskFailure("RecoveryRestoreProtectionInProgress", retryable=True)


def _service(
    context: worker.HandlerContext,
    *,
    cluster: str,
    service: str,
) -> Mapping[str, Any]:
    response = _call(
        context,
        "ecs",
        "describe_services",
        {"cluster": cluster, "services": [service]},
    )
    services = response.get("services")
    if response.get("failures") or type(services) is not list or len(services) != 1:
        raise worker.DomainTaskFailure("RecoveryControlServiceUnavailable", retryable=True)
    value = services[0]
    if not isinstance(value, Mapping):
        raise worker.DomainTaskFailure("RecoveryControlServiceUnavailable", retryable=True)
    for name in ("desiredCount", "pendingCount", "runningCount"):
        count = value.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise worker.DomainTaskFailure("RecoveryControlServiceUnavailable", retryable=True)
    return value


def _service_stable(service: Mapping[str, Any], desired: int) -> bool:
    deployments = service.get("deployments")
    return (
        service.get("desiredCount") == desired
        and service.get("runningCount") == desired
        and service.get("pendingCount") == 0
        and type(deployments) is list
        and len(deployments) == 1
        and isinstance(deployments[0], Mapping)
        and deployments[0].get("status") == "PRIMARY"
        and deployments[0].get("runningCount", desired) == desired
        and deployments[0].get("pendingCount", 0) == 0
        and deployments[0].get("rolloutState", "COMPLETED") == "COMPLETED"
    )


def _scalable_target(
    context: worker.HandlerContext,
    *,
    cluster: str,
    service: str,
) -> Mapping[str, Any]:
    resource_id = f"service/{cluster}/{service}"
    response = _call(
        context,
        "application-autoscaling",
        "describe_scalable_targets",
        {
            "ServiceNamespace": "ecs",
            "ResourceIds": [resource_id],
            "ScalableDimension": "ecs:service:DesiredCount",
        },
    )
    targets = response.get("ScalableTargets")
    if type(targets) is not list or len(targets) != 1 or not isinstance(targets[0], Mapping):
        raise worker.DomainTaskFailure("RecoveryScalingTargetUnavailable", retryable=True)
    return targets[0]


def _baseline(
    task: worker.ActionTask,
    context: worker.HandlerContext,
    resources: Mapping[str, str],
) -> dict[str, worker.JsonValue]:
    if context.fence_token is None:
        raise worker.HandlerContractError from None
    agent_stack, agent = _stack(context, resources["agentcoreStackArn"])
    control_stack, control = _stack(context, resources["controlPlaneStackArn"])
    del agent_stack, control_stack
    _required_outputs(
        agent,
        {
            "DataKeyArn",
            "RecoveryApprovalId",
            "RecoveryCutoverMode",
            "RuntimeArn",
            "SelectedRuntimeStateTableName",
            "StateTableName",
        },
    )
    _required_outputs(
        control,
        {
            "AgentCoreStackName",
            "ClusterName",
            "PrimaryStateTableName",
            "RecoveryApprovalId",
            "RecoveryCutoverMode",
            "SelectedRuntimeStateTableName",
            "ServiceName",
        },
    )
    if (
        agent["RuntimeArn"] != resources["runtimeArn"]
        or agent["StateTableName"] != resources["primaryTableName"]
        or agent["SelectedRuntimeStateTableName"] != resources["primaryTableName"]
        or agent["RecoveryCutoverMode"] != "normal"
        or control["AgentCoreStackName"] != resources["agentcoreStackName"]
        or control["PrimaryStateTableName"] != resources["primaryTableName"]
        or control["SelectedRuntimeStateTableName"] != resources["primaryTableName"]
        or control["RecoveryCutoverMode"] != "normal"
        or control["RecoveryApprovalId"] != agent["RecoveryApprovalId"]
    ):
        raise worker.DomainTaskFailure("RecoveryBaselineNotPrimary")
    service = _service(
        context,
        cluster=control["ClusterName"],
        service=control["ServiceName"],
    )
    target = _scalable_target(
        context,
        cluster=control["ClusterName"],
        service=control["ServiceName"],
    )
    desired = service["desiredCount"]
    minimum = target.get("MinCapacity")
    maximum = target.get("MaxCapacity")
    suspended = target.get("SuspendedState") or {}
    if (
        desired < 2
        or not _service_stable(service, desired)
        or isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum < 1
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < minimum
        or not isinstance(suspended, Mapping)
    ):
        raise worker.DomainTaskFailure("RecoveryControlBaselineUnavailable", retryable=True)
    return {
        "agentcoreStackArn": resources["agentcoreStackArn"],
        "controlPlaneStackArn": resources["controlPlaneStackArn"],
        "runtimeArn": resources["runtimeArn"],
        "dataKeyArn": agent["DataKeyArn"],
        "approvalBefore": agent["RecoveryApprovalId"],
        "approvalId": f"launch/{task.owner_id}",
        "authorizationOwnerId": task.owner_id,
        "authorizationFenceToken": context.fence_token,
        "clusterName": control["ClusterName"],
        "serviceName": control["ServiceName"],
        "desiredCount": desired,
        "minCapacity": minimum,
        "maxCapacity": maximum,
        "suspendedState": {
            key: suspended.get(key) is True for key in _SUSPENSION_KEYS
        },
    }


def _set_scaling(
    context: worker.HandlerContext,
    baseline: Mapping[str, Any],
    *,
    quiesced: bool,
) -> None:
    resource_id = f"service/{baseline['clusterName']}/{baseline['serviceName']}"
    _call(
        context,
        "application-autoscaling",
        "register_scalable_target",
        {
            "ServiceNamespace": "ecs",
            "ResourceId": resource_id,
            "ScalableDimension": "ecs:service:DesiredCount",
            "MinCapacity": 0 if quiesced else baseline["minCapacity"],
            "MaxCapacity": baseline["maxCapacity"],
            "SuspendedState": (
                {key: True for key in _SUSPENSION_KEYS}
                if quiesced
                else dict(baseline["suspendedState"])
            ),
        },
    )


def _quiesce(
    context: worker.HandlerContext,
    baseline: Mapping[str, Any],
) -> None:
    target = _scalable_target(
        context,
        cluster=baseline["clusterName"],
        service=baseline["serviceName"],
    )
    service = _service(
        context,
        cluster=baseline["clusterName"],
        service=baseline["serviceName"],
    )
    suspended = target.get("SuspendedState") or {}
    if (
        target.get("MinCapacity") == 0
        and all(suspended.get(key) is True for key in _SUSPENSION_KEYS)
        and _service_stable(service, 0)
    ):
        return
    _set_scaling(context, baseline, quiesced=True)
    _call(
        context,
        "ecs",
        "update_service",
        {
            "cluster": baseline["clusterName"],
            "service": baseline["serviceName"],
            "desiredCount": 0,
        },
    )
    raise worker.DomainTaskFailure("RecoveryQuiesceInProgress", retryable=True)


def _resume(
    context: worker.HandlerContext,
    baseline: Mapping[str, Any],
) -> tuple[int, int]:
    desired = baseline["desiredCount"]
    service = _service(
        context,
        cluster=baseline["clusterName"],
        service=baseline["serviceName"],
    )
    target = _scalable_target(
        context,
        cluster=baseline["clusterName"],
        service=baseline["serviceName"],
    )
    suspended = target.get("SuspendedState") or {}
    target_matches = (
        target.get("MinCapacity") == baseline["minCapacity"]
        and target.get("MaxCapacity") == baseline["maxCapacity"]
        and all(
            suspended.get(key) is baseline["suspendedState"][key]
            for key in _SUSPENSION_KEYS
        )
    )
    if _service_stable(service, desired) and target_matches:
        return desired, service["runningCount"]
    _call(
        context,
        "ecs",
        "update_service",
        {
            "cluster": baseline["clusterName"],
            "service": baseline["serviceName"],
            "desiredCount": desired,
        },
    )
    _set_scaling(context, baseline, quiesced=False)
    raise worker.DomainTaskFailure("RecoveryResumeInProgress", retryable=True)


def _endpoint_ready(
    context: worker.HandlerContext,
    runtime_arn: str,
    endpoint_name: str,
) -> str:
    runtime_id = runtime_arn.split(":runtime/", 1)[-1]
    if not runtime_id or "/" in runtime_id:
        raise worker.HandlerContractError from None
    endpoint = _call(
        context,
        "bedrock-agentcore-control",
        "get_agent_runtime_endpoint",
        {
            "agentRuntimeId": runtime_id,
            "endpointName": endpoint_name,
        },
    )
    if (
        endpoint.get("status") != "READY"
        or not endpoint.get("liveVersion")
        or endpoint.get("liveVersion") != endpoint.get("targetVersion")
    ):
        raise worker.DomainTaskFailure("RecoveryEndpointNotReady", retryable=True)
    return "READY"


def _selector_state(
    context: worker.HandlerContext,
    state: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    _, agent = _stack(context, state["agentcoreStackArn"])
    _, control = _stack(context, state["controlPlaneStackArn"])
    return agent, control


def _drive_selector(
    context: worker.HandlerContext,
    state: Mapping[str, Any],
    *,
    target_table: str,
    primary_table: str,
) -> tuple[int, int]:
    initial_agent, initial_control = _selector_state(context, state)
    if (
        initial_agent.get("RecoveryCutoverMode") == "normal"
        and initial_agent.get("SelectedRuntimeStateTableName") == target_table
        and initial_control.get("RecoveryCutoverMode") == "normal"
        and initial_control.get("SelectedRuntimeStateTableName") == target_table
    ):
        _endpoint_ready(context, state["runtimeArn"], "production")
        return _resume(context, state)
    _quiesce(context, state)
    agent, control = _selector_state(context, state)

    if control.get("RecoveryCutoverMode") == "normal":
        _update_selector(
            context,
            state=state,
            stack_arn=state["controlPlaneStackArn"],
            mode="quiesced",
            selected_table=control["SelectedRuntimeStateTableName"],
            primary_table=primary_table,
        )
    if (
        agent.get("RecoveryCutoverMode") == "normal"
        and (
            agent.get("SelectedRuntimeStateTableName") != target_table
            or control.get("RecoveryCutoverMode") == "quiesced"
        )
    ):
        _update_selector(
            context,
            state=state,
            stack_arn=state["agentcoreStackArn"],
            mode="quiesced",
            selected_table=agent["SelectedRuntimeStateTableName"],
            primary_table=primary_table,
        )
    agent, control = _selector_state(context, state)
    if control.get("RecoveryCutoverMode") == "quiesced":
        _update_selector(
            context,
            state=state,
            stack_arn=state["controlPlaneStackArn"],
            mode="selected",
            selected_table=target_table,
            primary_table=primary_table,
        )
    if agent.get("RecoveryCutoverMode") == "quiesced":
        _update_selector(
            context,
            state=state,
            stack_arn=state["agentcoreStackArn"],
            mode="selected",
            selected_table=target_table,
            primary_table=primary_table,
        )
    agent, control = _selector_state(context, state)
    if agent.get("RecoveryCutoverMode") == "selected":
        _update_selector(
            context,
            state=state,
            stack_arn=state["agentcoreStackArn"],
            mode="validation",
            selected_table=target_table,
            primary_table=primary_table,
        )
    agent, control = _selector_state(context, state)
    if agent.get("RecoveryCutoverMode") != "normal":
        if (
            agent.get("RecoveryCutoverMode") != "validation"
            or agent.get("SelectedRuntimeStateTableName") != target_table
            or control.get("RecoveryCutoverMode") != "selected"
            or control.get("SelectedRuntimeStateTableName") != target_table
        ):
            raise worker.DomainTaskFailure(
                "RecoverySelectorConvergenceFailed",
                retryable=True,
            )
        _endpoint_ready(context, state["runtimeArn"], "recovery")
        _update_selector(
            context,
            state=state,
            stack_arn=state["agentcoreStackArn"],
            mode="normal",
            selected_table=target_table,
            primary_table=primary_table,
        )
        agent, control = _selector_state(context, state)
    if (
        agent.get("RecoveryCutoverMode") != "normal"
        or agent.get("SelectedRuntimeStateTableName") != target_table
    ):
        raise worker.DomainTaskFailure("RecoverySelectorConvergenceFailed", retryable=True)
    _endpoint_ready(context, state["runtimeArn"], "production")
    _update_selector(
        context,
        state=state,
        stack_arn=state["controlPlaneStackArn"],
        mode="normal",
        selected_table=target_table,
        primary_table=primary_table,
    )
    return _resume(context, state)


def _assert_normal(
    context: worker.HandlerContext,
    state: Mapping[str, Any],
    *,
    selected_table: str,
) -> tuple[int, int]:
    agent, control = _selector_state(context, state)
    if (
        agent.get("RecoveryCutoverMode") != "normal"
        or agent.get("SelectedRuntimeStateTableName") != selected_table
        or control.get("RecoveryCutoverMode") != "normal"
        or control.get("SelectedRuntimeStateTableName") != selected_table
    ):
        raise worker.DomainTaskFailure("RecoverySelectorNotNormal", retryable=True)
    _endpoint_ready(context, state["runtimeArn"], "production")
    return _resume(context, state)


class RecoveryDomain:
    """Restore, select, validate, roll back, and clean one reviewed table."""

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        resources = _resource_binding(task, context)
        fixture_id = common.owned_id(task, "restored-state-table")
        if operation == "restore-state":
            primary = _describe_table(
                context,
                name=resources["primaryTableName"],
                arn=resources["primaryTableArn"],
            )
            if primary.get("TableStatus") != "ACTIVE":
                raise worker.DomainTaskFailure("RecoveryPrimaryTableUnavailable", retryable=True)
            baseline = _baseline(task, context, resources)
            try:
                _describe_table(
                    context,
                    name=resources["restoredTableName"],
                    arn=resources["restoredTableArn"],
                )
            except worker.AwsTransportError as exc:
                if exc.aws_code != "ResourceNotFoundException":
                    raise
                _call(
                    context,
                    "dynamodb",
                    "restore_table_to_point_in_time",
                    {
                        "SourceTableArn": resources["primaryTableArn"],
                        "TargetTableName": resources["restoredTableName"],
                        "UseLatestRestorableTime": True,
                    },
                )
                raise worker.DomainTaskFailure("RecoveryRestoreInProgress", retryable=True) from None
            _validate_restored_table(
                context,
                resources=resources,
                data_key_arn=baseline["dataKeyArn"],
            )
            next_ownership = common.copied_ownership(ownership)
            next_ownership["fixtureIds"] = [fixture_id]
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
                extra={
                    **resources,
                    **baseline,
                    "phase": "restored",
                },
            )
            return framework.DomainActionResult(
                evidence={
                    "primaryTableArn": resources["primaryTableArn"],
                    "restoredTableArn": resources["restoredTableArn"],
                },
                state=next_state,
                ownership=next_ownership,
            )

        common.completed_state(state, operations=OPERATIONS, operation=operation)
        if any(state.get(name) != resources[name] for name in resources):
            raise worker.DomainTaskFailure("RecoveryResourceBindingMismatch")
        _validate_restored_table(
            context,
            resources=resources,
            data_key_arn=state["dataKeyArn"],
        )
        phases = ["quiesced", "selected", "validation", "normal"]
        if operation == "cutover-restored-state":
            _drive_selector(
                context,
                state,
                target_table=resources["restoredTableName"],
                primary_table=resources["primaryTableName"],
            )
            evidence: dict[str, worker.JsonValue] = {
                "cutoverPhases": phases,
                "cutoverSelectedTableArn": resources["restoredTableArn"],
            }
            extra = {"phase": "restored-normal"}
        elif operation == "verify-restored-state":
            _assert_normal(
                context,
                state,
                selected_table=resources["restoredTableName"],
            )
            evidence = {}
            extra = None
        elif operation == "rollback-primary-state":
            _drive_selector(
                context,
                state,
                target_table=resources["primaryTableName"],
                primary_table=resources["primaryTableName"],
            )
            evidence = {
                "rollbackPhases": phases,
                "rollbackSelectedTableArn": resources["primaryTableArn"],
            }
            extra = {"phase": "primary-normal"}
        elif operation == "verify-primary-state":
            desired, running = _assert_normal(
                context,
                state,
                selected_table=resources["primaryTableName"],
            )
            evidence = {
                "finalSelectedTableArn": resources["primaryTableArn"],
                "productionEndpointStatusAfter": "READY",
                "controlPlaneDesiredCountAfter": desired,
                "controlPlaneRunningCountAfter": running,
            }
            extra = {"phase": "verified-primary"}
        else:
            raise worker.HandlerContractError from None
        next_state = common.completed_state(
            state,
            operations=OPERATIONS,
            operation=operation,
            extra=extra,
        )
        return framework.DomainActionResult(
            evidence=evidence,
            state=next_state,
            ownership=dict(ownership),
        )

    def cleanup(
        self,
        *,
        owner: framework.OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainCleanupResult:
        fixture_id = f"{owner.owner_id}:restored-state-table"
        fixtures = ownership.get("fixtureIds")
        if type(fixtures) is not list or any(item != fixture_id for item in fixtures):
            raise worker.HandlerContractError from None
        if not fixtures:
            return common.empty_cleanup(
                state=state,
                ownership=ownership,
                primary_state_selected=True,
                production_endpoint_status="READY",
            )
        _drive_selector(
            context,
            state,
            target_table=state["primaryTableName"],
            primary_table=state["primaryTableName"],
        )
        try:
            table = _describe_table(
                context,
                name=state["restoredTableName"],
                arn=state["restoredTableArn"],
            )
        except worker.AwsTransportError as exc:
            if exc.aws_code != "ResourceNotFoundException":
                raise
            table = None
        if table is not None:
            if table.get("DeletionProtectionEnabled") is True:
                _call(
                    context,
                    "dynamodb",
                    "update_table",
                    {
                        "TableName": state["restoredTableName"],
                        "DeletionProtectionEnabled": False,
                    },
                )
                raise worker.DomainTaskFailure("RecoveryCleanupInProgress", retryable=True)
            if table.get("TableStatus") != "ACTIVE":
                raise worker.DomainTaskFailure("RecoveryCleanupInProgress", retryable=True)
            _call(
                context,
                "dynamodb",
                "delete_table",
                {"TableName": state["restoredTableName"]},
            )
            raise worker.DomainTaskFailure("RecoveryCleanupInProgress", retryable=True)
        next_ownership = common.copied_ownership(ownership)
        next_ownership["fixtureIds"] = []
        return framework.DomainCleanupResult(
            state={**dict(state), "phase": "cleanup-complete"},
            ownership=next_ownership,
            verified_complete=True,
            cleared_fixture_ids=[fixture_id],
            primary_state_selected=True,
            production_endpoint_status="READY",
        )


def create_domain(**_kwargs: Any) -> RecoveryDomain:
    return RecoveryDomain()
