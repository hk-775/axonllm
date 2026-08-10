"""Operational Fargate recovery phase tests."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "operations"))

import fargate_recovery  # noqa: E402


class RecoveryAws:
    def __init__(self) -> None:
        self.selected_table = "axonllm-state"
        self.recovery_cutover_mode = "false"
        self.target = {
            "MinCapacity": 2,
            "MaxCapacity": 10,
            "SuspendedState": {
                key: False
                for key in fargate_recovery._SUSPENSION_KEYS
            },
        }
        self.service = {
            "desiredCount": 2,
            "pendingCount": 0,
            "runningCount": 2,
        }
        self.restore_table = {
            "TableName": (
                "axonllm-state-restore-validation-20260810120000-a1b2c3"
            ),
            "TableStatus": "ACTIVE",
            "DeletionProtectionEnabled": True,
        }
        self.deleted = False
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def _service(self) -> dict:
        desired = self.service["desiredCount"]
        return {
            **self.service,
            "deployments": [
                {
                    "status": "PRIMARY",
                    "runningCount": desired,
                    "pendingCount": 0,
                    "rolloutState": "COMPLETED",
                }
            ],
        }

    def json(self, service: str, operation: str, *arguments: str):
        self.calls.append((service, operation, arguments))
        if (service, operation) == (
            "cloudformation",
            "describe-stacks",
        ):
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "ClusterName",
                                "OutputValue": "axonllm",
                            },
                            {
                                "OutputKey": "RecoveryCutoverMode",
                                "OutputValue": self.recovery_cutover_mode,
                            },
                            {
                                "OutputKey": "ServiceName",
                                "OutputValue": "axonllm",
                            },
                            {
                                "OutputKey": "StateTableName",
                                "OutputValue": "axonllm-state",
                            },
                            {
                                "OutputKey": (
                                    "SelectedRuntimeStateTableName"
                                ),
                                "OutputValue": self.selected_table,
                            },
                            {
                                "OutputKey": "TargetGroupArn",
                                "OutputValue": (
                                    "arn:aws:elasticloadbalancing:"
                                    "us-east-1:123456789012:"
                                    "targetgroup/axonllm/abc"
                                ),
                            },
                        ]
                    }
                ]
            }
        if operation == "describe-scalable-targets":
            return {"ScalableTargets": [copy.deepcopy(self.target)]}
        if operation == "register-scalable-target":
            self.target["MinCapacity"] = int(
                arguments[arguments.index("--min-capacity") + 1]
            )
            self.target["MaxCapacity"] = int(
                arguments[arguments.index("--max-capacity") + 1]
            )
            raw = arguments[arguments.index("--suspended-state") + 1]
            self.target["SuspendedState"] = {
                name: value == "true"
                for name, value in (
                    item.split("=", 1) for item in raw.split(",")
                )
            }
            return {}
        if operation == "describe-services":
            return {"services": [self._service()], "failures": []}
        if operation == "describe-target-health":
            return {
                "TargetHealthDescriptions": [
                    {
                        "Target": {"Id": f"10.0.1.{index + 10}"},
                        "TargetHealth": {"State": "healthy"},
                    }
                    for index in range(self.service["runningCount"])
                ]
            }
        if operation == "update-service":
            desired = int(
                arguments[arguments.index("--desired-count") + 1]
            )
            self.service.update(
                desiredCount=desired,
                pendingCount=0,
                runningCount=desired,
            )
            return {"service": self._service()}
        if operation == "describe-table":
            return {"Table": copy.deepcopy(self.restore_table)}
        if operation == "update-table":
            self.restore_table["DeletionProtectionEnabled"] = False
            return {"TableDescription": copy.deepcopy(self.restore_table)}
        if operation == "delete-table":
            self.deleted = True
            return {"TableDescription": copy.deepcopy(self.restore_table)}
        raise AssertionError(f"unexpected AWS call: {service} {operation}")


def test_cutover_phases_preserve_and_restore_scaling_state(
    tmp_path,
) -> None:
    aws = RecoveryAws()
    state_file = tmp_path / "recovery.json"

    quiesced = fargate_recovery.quiesce(
        aws,
        region="us-east-1",
        stack_name="AxonLLMStack",
        state_file=state_file,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )

    assert quiesced["phase"] == "quiesced"
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert aws.service["runningCount"] == 0
    assert aws.target["MinCapacity"] == 0
    assert all(aws.target["SuspendedState"].values())

    aws.selected_table = aws.restore_table["TableName"]
    aws.recovery_cutover_mode = "true"
    started = fargate_recovery.start(
        aws,
        region="us-east-1",
        stack_name="AxonLLMStack",
        state_file=state_file,
        expected_table=aws.restore_table["TableName"],
        desired_count=None,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )

    assert started["phase"] == "canary-ready"
    assert aws.service["runningCount"] == 2
    assert started["healthyTargetCount"] == 2
    assert all(aws.target["SuspendedState"].values())

    resumed = fargate_recovery.resume(
        aws,
        region="us-east-1",
        stack_name="AxonLLMStack",
        state_file=state_file,
        expected_table=aws.restore_table["TableName"],
    )

    assert resumed["phase"] == "resumed"
    assert resumed["healthyTargetCount"] == 2
    assert aws.target["MinCapacity"] == 2
    assert aws.target["MaxCapacity"] == 10
    assert not any(aws.target["SuspendedState"].values())


def test_start_requires_stack_cutover_mode(tmp_path) -> None:
    aws = RecoveryAws()
    state_file = tmp_path / "recovery.json"
    fargate_recovery.quiesce(
        aws,
        region="us-east-1",
        stack_name="AxonLLMStack",
        state_file=state_file,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    aws.selected_table = aws.restore_table["TableName"]

    with pytest.raises(
        fargate_recovery.FargateRecoveryError,
        match="RecoveryCutoverMode",
    ):
        fargate_recovery.start(
            aws,
            region="us-east-1",
            stack_name="AxonLLMStack",
            state_file=state_file,
            expected_table=aws.restore_table["TableName"],
            desired_count=2,
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )

    assert aws.service["runningCount"] == 0


def test_start_rejects_unexpected_selected_table(tmp_path) -> None:
    aws = RecoveryAws()
    state_file = tmp_path / "recovery.json"
    fargate_recovery.quiesce(
        aws,
        region="us-east-1",
        stack_name="AxonLLMStack",
        state_file=state_file,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    aws.recovery_cutover_mode = "true"

    with pytest.raises(
        fargate_recovery.FargateRecoveryError,
        match="does not match",
    ):
        fargate_recovery.start(
            aws,
            region="us-east-1",
            stack_name="AxonLLMStack",
            state_file=state_file,
            expected_table=aws.restore_table["TableName"],
            desired_count=2,
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )

    assert aws.service["runningCount"] == 0
    assert all(aws.target["SuspendedState"].values())


def test_cleanup_rejects_selected_table_and_deletes_after_rollback() -> None:
    aws = RecoveryAws()
    table_name = aws.restore_table["TableName"]
    aws.selected_table = table_name

    with pytest.raises(
        fargate_recovery.FargateRecoveryError,
        match="currently selected",
    ):
        fargate_recovery.cleanup_restore(
            aws,
            stack_name="AxonLLMStack",
            table_name=table_name,
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )

    aws.selected_table = "axonllm-state"
    result = fargate_recovery.cleanup_restore(
        aws,
        stack_name="AxonLLMStack",
        table_name=table_name,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )

    assert result["phase"] == "cleanup-started"
    assert aws.restore_table["DeletionProtectionEnabled"] is False
    assert aws.deleted is True


def test_cleanup_rejects_table_outside_restore_namespace() -> None:
    aws = RecoveryAws()

    with pytest.raises(
        fargate_recovery.FargateRecoveryError,
        match="outside",
    ):
        fargate_recovery.cleanup_restore(
            aws,
            stack_name="AxonLLMStack",
            table_name="other-table",
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )


def test_status_can_require_two_healthy_backends() -> None:
    aws = RecoveryAws()

    result = fargate_recovery.status(
        aws,
        stack_name="AxonLLMStack",
        minimum_healthy_targets=2,
    )

    assert result["targetHealth"]["healthy"] == 2
    aws.service.update(desiredCount=1, runningCount=1)
    with pytest.raises(
        fargate_recovery.FargateRecoveryError,
        match="2 required",
    ):
        fargate_recovery.status(
            aws,
            stack_name="AxonLLMStack",
            minimum_healthy_targets=2,
        )
