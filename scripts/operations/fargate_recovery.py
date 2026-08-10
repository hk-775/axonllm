#!/usr/bin/env python3
"""Safely quiesce, validate, resume, and clean up Fargate recovery cutovers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aws_support import AwsCli, AwsError


class FargateRecoveryError(RuntimeError):
    """Raised when a recovery phase cannot prove its safety preconditions."""


_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)


def _stack_outputs(aws: AwsCli, stack_name: str) -> dict[str, str]:
    stacks = aws.json(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        stack_name,
    ).get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise FargateRecoveryError("could not resolve exactly one Fargate stack")
    outputs = stacks[0].get("Outputs", [])
    if not isinstance(outputs, list):
        raise FargateRecoveryError("Fargate stack outputs are missing")
    resolved = {
        output["OutputKey"]: output["OutputValue"]
        for output in outputs
        if isinstance(output, dict)
        and isinstance(output.get("OutputKey"), str)
        and isinstance(output.get("OutputValue"), str)
    }
    required = {
        "ClusterName",
        "RecoveryCutoverMode",
        "ServiceName",
        "StateTableName",
        "SelectedRuntimeStateTableName",
        "TargetGroupArn",
    }
    missing = sorted(required.difference(resolved))
    if missing:
        raise FargateRecoveryError(
            "Fargate stack outputs are missing: " + ", ".join(missing)
        )
    return resolved


def _resource_id(outputs: dict[str, str]) -> str:
    return f"service/{outputs['ClusterName']}/{outputs['ServiceName']}"


def _scalable_target(
    aws: AwsCli,
    outputs: dict[str, str],
) -> dict[str, Any]:
    targets = aws.json(
        "application-autoscaling",
        "describe-scalable-targets",
        "--service-namespace",
        "ecs",
        "--resource-ids",
        _resource_id(outputs),
        "--scalable-dimension",
        "ecs:service:DesiredCount",
    ).get("ScalableTargets")
    if not isinstance(targets, list) or len(targets) != 1:
        raise FargateRecoveryError(
            "recovery requires exactly one ECS scalable target"
        )
    target = targets[0]
    if not isinstance(target, dict):
        raise FargateRecoveryError("ECS scalable target is malformed")
    for field in ("MinCapacity", "MaxCapacity"):
        if (
            not isinstance(target.get(field), int)
            or isinstance(target.get(field), bool)
            or target[field] < 0
        ):
            raise FargateRecoveryError(
                f"ECS scalable target has invalid {field}"
            )
    suspended = target.get("SuspendedState") or {}
    if not isinstance(suspended, dict):
        raise FargateRecoveryError(
            "ECS scalable target suspension state is malformed"
        )
    target["SuspendedState"] = {
        key: suspended.get(key) is True
        for key in _SUSPENSION_KEYS
    }
    return target


def _service(
    aws: AwsCli,
    outputs: dict[str, str],
) -> dict[str, Any]:
    response = aws.json(
        "ecs",
        "describe-services",
        "--cluster",
        outputs["ClusterName"],
        "--services",
        outputs["ServiceName"],
    )
    failures = response.get("failures") or []
    services = response.get("services")
    if failures or not isinstance(services, list) or len(services) != 1:
        raise FargateRecoveryError("could not resolve exactly one ECS service")
    service = services[0]
    if not isinstance(service, dict):
        raise FargateRecoveryError("ECS service response is malformed")
    counts = {
        name: service.get(name)
        for name in ("desiredCount", "pendingCount", "runningCount")
    }
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for value in counts.values()
    ):
        raise FargateRecoveryError("ECS service counts are malformed")
    return service


def _target_health(
    aws: AwsCli,
    outputs: dict[str, str],
) -> dict[str, Any]:
    descriptions = aws.json(
        "elbv2",
        "describe-target-health",
        "--target-group-arn",
        outputs["TargetGroupArn"],
    ).get("TargetHealthDescriptions")
    if not isinstance(descriptions, list):
        raise FargateRecoveryError("ALB target-health response is malformed")
    states: dict[str, int] = {}
    for description in descriptions:
        if not isinstance(description, dict):
            raise FargateRecoveryError(
                "ALB target-health entry is malformed"
            )
        state = (description.get("TargetHealth") or {}).get("State")
        if not isinstance(state, str) or not state:
            raise FargateRecoveryError(
                "ALB target-health state is missing"
            )
        states[state] = states.get(state, 0) + 1
    return {
        "registered": len(descriptions),
        "healthy": states.get("healthy", 0),
        "states": dict(sorted(states.items())),
    }


def _wait_healthy_targets(
    aws: AwsCli,
    outputs: dict[str, str],
    *,
    minimum: int,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        health = _target_health(aws, outputs)
        if health["healthy"] >= minimum:
            return health
        sleep(poll_interval)
    raise FargateRecoveryError(
        f"ALB did not reach {minimum} healthy targets"
    )


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
    outputs: dict[str, str],
    *,
    desired: int,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        service = _service(aws, outputs)
        if _service_stable(service, desired):
            return service
        sleep(poll_interval)
    raise FargateRecoveryError(
        f"ECS service did not stabilize at {desired} tasks"
    )


def _suspended_argument(state: dict[str, bool]) -> str:
    return ",".join(
        f"{key}={'true' if state.get(key) is True else 'false'}"
        for key in _SUSPENSION_KEYS
    )


def _register_target(
    aws: AwsCli,
    outputs: dict[str, str],
    *,
    minimum: int,
    maximum: int,
    suspended: dict[str, bool],
) -> None:
    if minimum < 0 or maximum < minimum:
        raise FargateRecoveryError("invalid scalable-target capacity bounds")
    aws.json(
        "application-autoscaling",
        "register-scalable-target",
        "--service-namespace",
        "ecs",
        "--resource-id",
        _resource_id(outputs),
        "--scalable-dimension",
        "ecs:service:DesiredCount",
        "--min-capacity",
        str(minimum),
        "--max-capacity",
        str(maximum),
        "--suspended-state",
        _suspended_argument(suspended),
    )


def _update_desired(
    aws: AwsCli,
    outputs: dict[str, str],
    desired: int,
) -> None:
    aws.json(
        "ecs",
        "update-service",
        "--cluster",
        outputs["ClusterName"],
        "--service",
        outputs["ServiceName"],
        "--desired-count",
        str(desired),
    )


def _write_state(path: Path, state: dict[str, Any], *, exclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    try:
        with path.open(mode, encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise FargateRecoveryError(
            f"recovery state file already exists: {path}"
        ) from exc


def _load_state(path: Path, region: str, stack_name: str) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FargateRecoveryError(
            f"could not read recovery state file: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FargateRecoveryError("recovery state file is invalid JSON") from exc
    if not isinstance(state, dict) or state.get("schemaVersion") != 1:
        raise FargateRecoveryError("unsupported recovery state file")
    if state.get("region") != region or state.get("stackName") != stack_name:
        raise FargateRecoveryError(
            "recovery state file does not match the selected stack and region"
        )
    return state


def _verify_state_ownership(
    state: dict[str, Any],
    outputs: dict[str, str],
) -> None:
    expected = {
        "clusterName": outputs["ClusterName"],
        "serviceName": outputs["ServiceName"],
        "resourceId": _resource_id(outputs),
    }
    if any(state.get(name) != value for name, value in expected.items()):
        raise FargateRecoveryError(
            "recovery state file does not match the deployed ECS service"
        )


def _verify_selected_table(
    outputs: dict[str, str],
    expected_table: str,
) -> None:
    if outputs["SelectedRuntimeStateTableName"] != expected_table:
        raise FargateRecoveryError(
            "selected runtime table does not match the approved recovery target"
        )


def _verify_cutover_mode(outputs: dict[str, str]) -> None:
    if outputs["RecoveryCutoverMode"] != "true":
        raise FargateRecoveryError(
            "stack RecoveryCutoverMode must remain true during canary validation"
        )


def quiesce(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    outputs = _stack_outputs(aws, stack_name)
    target = _scalable_target(aws, outputs)
    service = _service(aws, outputs)
    state = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "region": region,
        "stackName": stack_name,
        "clusterName": outputs["ClusterName"],
        "serviceName": outputs["ServiceName"],
        "resourceId": _resource_id(outputs),
        "cutoverModeBefore": outputs["RecoveryCutoverMode"],
        "selectedTableBefore": outputs["SelectedRuntimeStateTableName"],
        "primaryTable": outputs["StateTableName"],
        "desiredCount": service["desiredCount"],
        "minCapacity": target["MinCapacity"],
        "maxCapacity": target["MaxCapacity"],
        "suspendedState": target["SuspendedState"],
    }
    _write_state(state_file, state, exclusive=True)

    suspended = {key: True for key in _SUSPENSION_KEYS}
    _register_target(
        aws,
        outputs,
        minimum=0,
        maximum=target["MaxCapacity"],
        suspended=suspended,
    )
    _update_desired(aws, outputs, 0)
    stable = _wait_service(
        aws,
        outputs,
        desired=0,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )
    return {
        "phase": "quiesced",
        "stateFile": str(state_file),
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
        "desiredCount": stable["desiredCount"],
        "runningCount": stable["runningCount"],
        "autoscalingSuspended": True,
    }


def start(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    expected_table: str,
    desired_count: int | None,
    timeout_seconds: int,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    state = _load_state(state_file, region, stack_name)
    outputs = _stack_outputs(aws, stack_name)
    _verify_state_ownership(state, outputs)
    _verify_selected_table(outputs, expected_table)
    _verify_cutover_mode(outputs)

    target = _scalable_target(aws, outputs)
    if target["MinCapacity"] != 0 or not all(
        target["SuspendedState"].values()
    ):
        raise FargateRecoveryError(
            "autoscaling must remain fully suspended before starting canary tasks"
        )
    service = _service(aws, outputs)
    if not _service_stable(service, 0):
        raise FargateRecoveryError(
            "ECS service must be fully quiesced before starting canary tasks"
        )

    resolved_desired = (
        desired_count
        if desired_count is not None
        else state.get("desiredCount")
    )
    if (
        not isinstance(resolved_desired, int)
        or isinstance(resolved_desired, bool)
        or resolved_desired < 1
        or resolved_desired > target["MaxCapacity"]
    ):
        raise FargateRecoveryError(
            "canary desired count must be between one and max capacity"
        )
    _update_desired(aws, outputs, resolved_desired)
    stable = _wait_service(
        aws,
        outputs,
        desired=resolved_desired,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )
    target_health = _wait_healthy_targets(
        aws,
        outputs,
        minimum=resolved_desired,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        sleep=sleep,
    )
    state["startedDesiredCount"] = resolved_desired
    state["selectedTableAfter"] = expected_table
    state["startedAt"] = datetime.now(timezone.utc).isoformat()
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "canary-ready",
        "stateFile": str(state_file),
        "selectedTable": expected_table,
        "desiredCount": stable["desiredCount"],
        "runningCount": stable["runningCount"],
        "healthyTargetCount": target_health["healthy"],
        "autoscalingSuspended": True,
    }


def resume(
    aws: AwsCli,
    *,
    region: str,
    stack_name: str,
    state_file: Path,
    expected_table: str,
) -> dict[str, Any]:
    state = _load_state(state_file, region, stack_name)
    outputs = _stack_outputs(aws, stack_name)
    _verify_state_ownership(state, outputs)
    _verify_selected_table(outputs, expected_table)
    _verify_cutover_mode(outputs)
    started_desired = state.get("startedDesiredCount")
    if not isinstance(started_desired, int) or started_desired < 1:
        raise FargateRecoveryError(
            "start phase has not recorded stable canary tasks"
        )
    service = _service(aws, outputs)
    if not _service_stable(service, started_desired):
        raise FargateRecoveryError(
            "canary tasks are not stable; autoscaling will remain suspended"
        )
    target_health = _target_health(aws, outputs)
    if target_health["healthy"] < started_desired:
        raise FargateRecoveryError(
            "not every canary task is healthy; autoscaling will remain suspended"
        )
    target = _scalable_target(aws, outputs)
    if target["MinCapacity"] != 0 or not all(
        target["SuspendedState"].values()
    ):
        raise FargateRecoveryError(
            "autoscaling suspension changed before resume"
        )

    suspended = state.get("suspendedState")
    if not isinstance(suspended, dict) or set(suspended) != set(
        _SUSPENSION_KEYS
    ):
        raise FargateRecoveryError(
            "recorded autoscaling suspension state is malformed"
        )
    minimum = state.get("minCapacity")
    maximum = state.get("maxCapacity")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
    ):
        raise FargateRecoveryError(
            "recorded autoscaling capacity bounds are malformed"
        )
    _register_target(
        aws,
        outputs,
        minimum=minimum,
        maximum=maximum,
        suspended=suspended,
    )
    state["resumedAt"] = datetime.now(timezone.utc).isoformat()
    state["completed"] = True
    _write_state(state_file, state, exclusive=False)
    return {
        "phase": "resumed",
        "stateFile": str(state_file),
        "selectedTable": expected_table,
        "desiredCount": service["desiredCount"],
        "runningCount": service["runningCount"],
        "healthyTargetCount": target_health["healthy"],
        "minCapacity": minimum,
        "maxCapacity": maximum,
        "suspendedState": suspended,
    }


def status(
    aws: AwsCli,
    *,
    stack_name: str,
    minimum_healthy_targets: int = 0,
) -> dict[str, Any]:
    outputs = _stack_outputs(aws, stack_name)
    target = _scalable_target(aws, outputs)
    service = _service(aws, outputs)
    target_health = _target_health(aws, outputs)
    if target_health["healthy"] < minimum_healthy_targets:
        raise FargateRecoveryError(
            f"ALB has {target_health['healthy']} healthy targets; "
            f"{minimum_healthy_targets} required"
        )
    return {
        "phase": "status",
        "clusterName": outputs["ClusterName"],
        "serviceName": outputs["ServiceName"],
        "primaryTable": outputs["StateTableName"],
        "recoveryCutoverMode": outputs["RecoveryCutoverMode"],
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
        "desiredCount": service["desiredCount"],
        "pendingCount": service["pendingCount"],
        "runningCount": service["runningCount"],
        "targetHealth": target_health,
        "minCapacity": target["MinCapacity"],
        "maxCapacity": target["MaxCapacity"],
        "suspendedState": target["SuspendedState"],
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
    outputs = _stack_outputs(aws, stack_name)
    expected_prefix = f"{outputs['StateTableName']}-restore-validation-"
    if not table_name.startswith(expected_prefix):
        raise FargateRecoveryError(
            "cleanup target is outside the stack restore-validation namespace"
        )
    if outputs["SelectedRuntimeStateTableName"] == table_name:
        raise FargateRecoveryError(
            "cannot delete the table currently selected by the runtime"
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
        raise FargateRecoveryError(
            "cleanup target is not the expected ACTIVE DynamoDB table"
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
            raise FargateRecoveryError(
                "restore table did not disable deletion protection"
            )
    aws.json("dynamodb", "delete-table", "--table-name", table_name)
    return {
        "phase": "cleanup-started",
        "tableName": table_name,
        "selectedTable": outputs["SelectedRuntimeStateTableName"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="AxonLLMStack")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-interval", type=float, default=10)
    subparsers = parser.add_subparsers(dest="command", required=True)

    state_help = "0600 JSON evidence/state file for this recovery rehearsal"
    quiesce_parser = subparsers.add_parser("quiesce")
    quiesce_parser.add_argument("--state-file", required=True, type=Path, help=state_help)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--state-file", required=True, type=Path, help=state_help)
    start_parser.add_argument("--expected-table", required=True)
    start_parser.add_argument("--desired-count", type=int)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--state-file", required=True, type=Path, help=state_help)
    resume_parser.add_argument("--expected-table", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument(
        "--minimum-healthy-targets",
        type=int,
        default=0,
    )
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--table-name", required=True)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.timeout_seconds < 1 or args.poll_interval < 0:
        parser.error("timeouts must be positive and poll interval non-negative")
    if (
        args.command == "status"
        and args.minimum_healthy_targets < 0
    ):
        parser.error("--minimum-healthy-targets must not be negative")
    aws = AwsCli(args.region)
    try:
        if args.command == "quiesce":
            result = quiesce(
                aws,
                region=args.region,
                stack_name=args.stack_name,
                state_file=args.state_file,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        elif args.command == "start":
            result = start(
                aws,
                region=args.region,
                stack_name=args.stack_name,
                state_file=args.state_file,
                expected_table=args.expected_table,
                desired_count=args.desired_count,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        elif args.command == "resume":
            result = resume(
                aws,
                region=args.region,
                stack_name=args.stack_name,
                state_file=args.state_file,
                expected_table=args.expected_table,
            )
        elif args.command == "status":
            result = status(
                aws,
                stack_name=args.stack_name,
                minimum_healthy_targets=args.minimum_healthy_targets,
            )
        else:
            result = cleanup_restore(
                aws,
                stack_name=args.stack_name,
                table_name=args.table_name,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
    except (AwsError, FargateRecoveryError) as exc:
        print(f"Fargate recovery operation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
