"""Operational AgentCore recovery selector tests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "operations"))

import agentcore_recovery  # noqa: E402
from aws_support import AwsError  # noqa: E402


PRIMARY = "axonllm-agentcore-state"
RESTORED = f"{PRIMARY}-restore-validation-20260811120000-a1b2c3"
DATA_KEY_ARN = (
    "arn:aws:kms:us-east-1:123456789012:"
    "key/11111111-2222-3333-4444-555555555555"
)


class RecoveryAws:
    def __init__(self, *, control_plane: bool = False) -> None:
        self.mode = "normal"
        self.approval = ""
        self.target_parameter = ""
        self.updated = 1
        self.control_mode = "normal"
        self.control_approval = ""
        self.control_target_parameter = ""
        self.control_updated = 1
        self.deleted = False
        self.control_plane = control_plane
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.target = {
            "MinCapacity": 2,
            "MaxCapacity": 6,
            "SuspendedState": {
                key: False
                for key in agentcore_recovery._SUSPENSION_KEYS
            },
        }
        self.service = {
            "desiredCount": 2,
            "pendingCount": 0,
            "runningCount": 2,
        }
        self.table = {
            "TableName": RESTORED,
            "TableStatus": "ACTIVE",
            "DeletionProtectionEnabled": True,
            "SSEDescription": {
                "Status": "ENABLED",
                "KMSMasterKeyArn": DATA_KEY_ARN,
            },
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
        }

    @property
    def selected(self) -> str:
        return self.target_parameter or PRIMARY

    @property
    def control_selected(self) -> str:
        return self.control_target_parameter or PRIMARY

    def _runtime_stack(self) -> dict:
        outputs = [
            {"OutputKey": "DataKeyArn", "OutputValue": DATA_KEY_ARN},
            {"OutputKey": "RuntimeArn", "OutputValue": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                "runtime/axonllm-abcdefghij"
            )},
            {"OutputKey": "StateTableName", "OutputValue": PRIMARY},
            {
                "OutputKey": "SelectedRuntimeStateTableName",
                "OutputValue": self.selected,
            },
            {
                "OutputKey": "RecoveryCutoverMode",
                "OutputValue": self.mode,
            },
            {
                "OutputKey": "RecoveryApprovalId",
                "OutputValue": self.approval,
            },
            {
                "OutputKey": "RecoveryQuiescedAt",
                "OutputValue": (
                    "2026-08-11T12:00:00+00:00"
                    if self.mode != "normal"
                    else "not-quiesced"
                ),
            },
            {
                "OutputKey": "RecoveryMinimumQuiescenceSeconds",
                "OutputValue": "14700",
            },
        ]
        if self.mode == "normal":
            outputs.append(
                {
                    "OutputKey": "RuntimeEndpointArn",
                    "OutputValue": (
                        "arn:aws:bedrock-agentcore:us-east-1:"
                        "123456789012:runtime/axonllm-abcdefghij/"
                        "runtime-endpoint/production"
                    ),
                }
            )
        if self.mode == "validation":
            outputs.append(
                {
                    "OutputKey": "RecoveryRuntimeEndpointArn",
                    "OutputValue": (
                        "arn:aws:bedrock-agentcore:us-east-1:"
                        "123456789012:runtime/axonllm-abcdefghij/"
                        "runtime-endpoint/recovery"
                    ),
                }
            )
        return {
            "StackId": (
                "arn:aws:cloudformation:us-east-1:123456789012:"
                "stack/AxonLLMAgentCoreStack/stack-id"
            ),
            "RoleARN": (
                "arn:aws:iam::123456789012:"
                "role/axonllm-cloudformation-execution"
            ),
            "StackStatus": "UPDATE_COMPLETE",
            "CreationTime": "2026-08-11T10:00:00+00:00",
            "LastUpdatedTime": (
                f"2026-08-11T10:{self.updated:02d}:00+00:00"
            ),
            "Parameters": [
                {"ParameterKey": "OidcIssuer", "ParameterValue": "issuer"},
                {
                    "ParameterKey": "RecoveryApprovalId",
                    "ParameterValue": self.approval,
                },
                {
                    "ParameterKey": "RecoveryCutoverMode",
                    "ParameterValue": self.mode,
                },
                {
                    "ParameterKey": "RuntimeStateTableName",
                    "ParameterValue": self.target_parameter,
                },
            ],
            "Outputs": outputs,
        }

    def _control_stack(self) -> dict:
        return {
            "StackId": (
                "arn:aws:cloudformation:us-east-1:123456789012:"
                "stack/AxonLLMControlPlaneStack/control-id"
            ),
            "RoleARN": (
                "arn:aws:iam::123456789012:"
                "role/axonllm-cloudformation-execution"
            ),
            "StackStatus": "UPDATE_COMPLETE",
            "CreationTime": "2026-08-11T10:00:00+00:00",
            "LastUpdatedTime": (
                f"2026-08-11T11:{self.control_updated:02d}:00+00:00"
            ),
            "Parameters": [
                {
                    "ParameterKey": "AgentCoreStackName",
                    "ParameterValue": "AxonLLMAgentCoreStack",
                },
                {
                    "ParameterKey": "RecoveryApprovalId",
                    "ParameterValue": self.control_approval,
                },
                {
                    "ParameterKey": "RecoveryCutoverMode",
                    "ParameterValue": self.control_mode,
                },
                {
                    "ParameterKey": "RuntimeStateTableName",
                    "ParameterValue": self.control_target_parameter,
                },
            ],
            "Outputs": [
                {
                    "OutputKey": "AgentCoreStackName",
                    "OutputValue": "AxonLLMAgentCoreStack",
                },
                {"OutputKey": "ClusterName", "OutputValue": "control"},
                {"OutputKey": "ServiceName", "OutputValue": "web"},
                {
                    "OutputKey": "PrimaryStateTableName",
                    "OutputValue": PRIMARY,
                },
                {
                    "OutputKey": "SelectedRuntimeStateTableName",
                    "OutputValue": self.control_selected,
                },
                {
                    "OutputKey": "RecoveryCutoverMode",
                    "OutputValue": self.control_mode,
                },
                {
                    "OutputKey": "RecoveryApprovalId",
                    "OutputValue": self.control_approval,
                },
            ],
        }

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
            name = arguments[arguments.index("--stack-name") + 1]
            if name == "AxonLLMAgentCoreStack":
                return {"Stacks": [copy.deepcopy(self._runtime_stack())]}
            if name == "AxonLLMControlPlaneStack" and self.control_plane:
                return {"Stacks": [copy.deepcopy(self._control_stack())]}
            raise AwsError(f"Stack with id {name} does not exist")
        if (service, operation) == (
            "cloudformation",
            "update-stack",
        ):
            payload = json.loads(
                arguments[arguments.index("--parameters") + 1]
            )
            changes = {
                item["ParameterKey"]: item["ParameterValue"]
                for item in payload
                if "ParameterValue" in item
            }
            name = arguments[arguments.index("--stack-name") + 1]
            if name == "AxonLLMAgentCoreStack":
                assert next(
                    item
                    for item in payload
                    if item["ParameterKey"] == "OidcIssuer"
                )["UsePreviousValue"] is True
                self.mode = changes["RecoveryCutoverMode"]
                self.approval = changes["RecoveryApprovalId"]
                self.target_parameter = changes["RuntimeStateTableName"]
                self.updated += 1
                return {"StackId": self._runtime_stack()["StackId"]}
            if name == "AxonLLMControlPlaneStack":
                assert next(
                    item
                    for item in payload
                    if item["ParameterKey"] == "AgentCoreStackName"
                )["UsePreviousValue"] is True
                self.control_mode = changes["RecoveryCutoverMode"]
                self.control_approval = changes["RecoveryApprovalId"]
                self.control_target_parameter = changes[
                    "RuntimeStateTableName"
                ]
                self.control_updated += 1
                return {"StackId": self._control_stack()["StackId"]}
            raise AssertionError(f"unexpected stack update: {name}")
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
            table = copy.deepcopy(self.table)
            table["TableName"] = arguments[
                arguments.index("--table-name") + 1
            ]
            return {"Table": table}
        if operation == "describe-continuous-backups":
            return {
                "ContinuousBackupsDescription": {
                    "PointInTimeRecoveryDescription": {
                        "PointInTimeRecoveryStatus": "ENABLED"
                    }
                }
            }
        if operation == "describe-time-to-live":
            return {
                "TimeToLiveDescription": {
                    "TimeToLiveStatus": "ENABLED",
                    "AttributeName": "expires_at",
                }
            }
        if operation == "get-agent-runtime-endpoint":
            name = arguments[arguments.index("--endpoint-name") + 1]
            return {
                "agentRuntimeEndpointArn": f"arn:endpoint:{name}",
                "status": "READY",
                "liveVersion": "2",
                "targetVersion": "2",
            }
        if operation == "update-table":
            self.table["DeletionProtectionEnabled"] = False
            return {"TableDescription": copy.deepcopy(self.table)}
        if operation == "delete-table":
            self.deleted = True
            return {"TableDescription": copy.deepcopy(self.table)}
        raise AssertionError(f"unexpected AWS call: {service} {operation}")


def test_forward_cutover_uses_four_reviewed_fail_closed_phases(
    tmp_path,
) -> None:
    aws = RecoveryAws()
    state_file = tmp_path / "agentcore-recovery.json"

    quiesced = agentcore_recovery.quiesce(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        approval_id="INC-2026-001",
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert quiesced["phase"] == "quiesced"
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert aws.mode == "quiesced"
    assert aws.selected == PRIMARY

    selected = agentcore_recovery.select(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        expected_table=RESTORED,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert selected["stateAccessBlocked"] is True
    assert aws.mode == "selected"
    assert aws.selected == RESTORED

    started = agentcore_recovery.start(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        expected_table=RESTORED,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert started["phase"] == "validation"
    assert started["runtimeVersion"] == "2"
    assert aws.mode == "validation"

    promoted = agentcore_recovery.promote(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        expected_table=RESTORED,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert promoted["phase"] == "promoted"
    assert aws.mode == "normal"
    assert aws.selected == RESTORED
    assert json.loads(state_file.read_text())["phase"] == "promoted"


def test_quiesce_preserves_and_stops_shared_control_plane(tmp_path) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "agentcore-recovery.json"

    result = agentcore_recovery.quiesce(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        approval_id="CHG-2026-002",
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )

    state = json.loads(state_file.read_text())
    assert result["controlPlaneQuiesced"] is True
    assert state["controlPlane"]["desiredCount"] == 2
    assert state["controlPlane"]["minCapacity"] == 2
    assert aws.service["runningCount"] == 0
    assert aws.target["MinCapacity"] == 0
    assert all(aws.target["SuspendedState"].values())


def test_quiesce_retries_an_interrupted_control_plane_stop(
    tmp_path,
    monkeypatch,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "interrupted-quiesce.json"
    original_update = agentcore_recovery._update_desired_count
    attempts = 0

    def interrupt_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("operator process interrupted")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(
        agentcore_recovery,
        "_update_desired_count",
        interrupt_once,
    )
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "approval_id": "CHG-2026-022",
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }

    with pytest.raises(RuntimeError, match="interrupted"):
        agentcore_recovery.quiesce(aws, **common)

    state = json.loads(state_file.read_text())
    assert state["phase"] == "quiescing"
    assert state["controlPlane"]["desiredCount"] == 2
    assert aws.target["MinCapacity"] == 0
    assert aws.service["runningCount"] == 2

    result = agentcore_recovery.quiesce(aws, **common)

    assert result["phase"] == "quiesced"
    assert aws.mode == aws.control_mode == "quiesced"
    assert aws.service["runningCount"] == 0
    assert json.loads(state_file.read_text())["phase"] == "quiesced"


def test_quiesce_retries_after_runtime_update_before_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "runtime-updated.json"
    original_write = agentcore_recovery._write_state
    interrupted = False

    def interrupt_before_final_checkpoint(
        path: Path,
        state: dict,
        *,
        exclusive: bool,
    ) -> None:
        nonlocal interrupted
        if state.get("phase") == "quiesced" and not interrupted:
            interrupted = True
            raise RuntimeError("operator process interrupted")
        original_write(path, state, exclusive=exclusive)

    monkeypatch.setattr(
        agentcore_recovery,
        "_write_state",
        interrupt_before_final_checkpoint,
    )
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "approval_id": "CHG-2026-023",
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }

    with pytest.raises(RuntimeError, match="interrupted"):
        agentcore_recovery.quiesce(aws, **common)

    assert aws.mode == aws.control_mode == "quiesced"
    assert (
        json.loads(state_file.read_text())["phase"]
        == "control-plane-quiesced"
    )

    result = agentcore_recovery.quiesce(aws, **common)

    assert result["phase"] == "quiesced"
    assert json.loads(state_file.read_text())["phase"] == "quiesced"


def test_abort_completes_a_partial_control_plane_stop(
    tmp_path,
    monkeypatch,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "partial-quiesce-abort.json"
    original_update = agentcore_recovery._update_desired_count

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("operator process interrupted")

    monkeypatch.setattr(
        agentcore_recovery,
        "_update_desired_count",
        interrupt,
    )
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }
    with pytest.raises(RuntimeError, match="interrupted"):
        agentcore_recovery.quiesce(
            aws,
            approval_id="CHG-2026-024",
            **common,
        )
    monkeypatch.setattr(
        agentcore_recovery,
        "_update_desired_count",
        original_update,
    )

    aborted = agentcore_recovery.abort(aws, **common)
    resumed = _resume(aws, state_file)

    assert aborted["phase"] == "aborted"
    assert resumed["phase"] == "control-plane-resumed"
    assert aws.mode == aws.control_mode == "normal"
    assert aws.service["runningCount"] == 2
    assert aws.target["MinCapacity"] == 2


def test_select_rejects_unprotected_or_out_of_namespace_table(
    tmp_path,
) -> None:
    aws = RecoveryAws()
    state_file = tmp_path / "agentcore-recovery.json"
    agentcore_recovery.quiesce(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        approval_id="INC-2026-003",
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )

    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="outside",
    ):
        agentcore_recovery.select(
            aws,
            region="us-east-1",
            stack_name="AxonLLMAgentCoreStack",
            state_file=state_file,
            expected_table="other-table",
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )

    aws.table["DeletionProtectionEnabled"] = False
    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="deletion protection",
    ):
        agentcore_recovery.select(
            aws,
            region="us-east-1",
            stack_name="AxonLLMAgentCoreStack",
            state_file=state_file,
            expected_table=RESTORED,
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )

    aws.table["DeletionProtectionEnabled"] = True
    aws.table["SSEDescription"]["KMSMasterKeyArn"] = (
        "arn:aws:kms:us-east-1:123456789012:key/other"
    )
    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="AgentCore data key",
    ):
        agentcore_recovery.select(
            aws,
            region="us-east-1",
            stack_name="AxonLLMAgentCoreStack",
            state_file=state_file,
            expected_table=RESTORED,
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )


def test_cleanup_refuses_selected_table_and_deletes_after_rollback() -> None:
    aws = RecoveryAws()
    aws.target_parameter = RESTORED

    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="currently selected",
    ):
        agentcore_recovery.cleanup_restore(
            aws,
            stack_name="AxonLLMAgentCoreStack",
            table_name=RESTORED,
            timeout_seconds=5,
            poll_interval=0,
            sleep=lambda _: None,
        )

    aws.target_parameter = ""
    result = agentcore_recovery.cleanup_restore(
        aws,
        stack_name="AxonLLMAgentCoreStack",
        table_name=RESTORED,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )
    assert result["phase"] == "cleanup-started"
    assert aws.table["DeletionProtectionEnabled"] is False
    assert aws.deleted is True


def _complete_cutover(
    aws: RecoveryAws,
    state_file: Path,
    *,
    target_table: str,
    approval_id: str,
) -> None:
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }
    agentcore_recovery.quiesce(
        aws,
        approval_id=approval_id,
        **common,
    )
    agentcore_recovery.select(
        aws,
        expected_table=target_table,
        **common,
    )
    agentcore_recovery.start(
        aws,
        expected_table=target_table,
        **common,
    )
    agentcore_recovery.promote(
        aws,
        expected_table=target_table,
        **common,
    )


def _resume(aws: RecoveryAws, state_file: Path) -> dict:
    return agentcore_recovery.resume_control_plane(
        aws,
        region="us-east-1",
        stack_name="AxonLLMAgentCoreStack",
        state_file=state_file,
        timeout_seconds=5,
        poll_interval=0,
        sleep=lambda _: None,
    )


def test_promote_resumes_both_planes_on_the_restored_table(
    tmp_path,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "promote.json"

    _complete_cutover(
        aws,
        state_file,
        target_table=RESTORED,
        approval_id="CHG-2026-010",
    )

    assert aws.mode == "normal"
    assert aws.control_mode == "selected"
    assert aws.selected == aws.control_selected == RESTORED
    assert aws.service["runningCount"] == 0

    resumed = _resume(aws, state_file)

    assert resumed["phase"] == "control-plane-resumed"
    assert resumed["selectedTable"] == RESTORED
    assert aws.mode == aws.control_mode == "normal"
    assert aws.selected == aws.control_selected == RESTORED
    assert aws.service["runningCount"] == 2
    assert aws.target["MinCapacity"] == 2
    assert not any(aws.target["SuspendedState"].values())
    assert json.loads(state_file.read_text())["phase"] == "complete"


def test_promoted_restore_rolls_back_to_primary_and_resumes(
    tmp_path,
) -> None:
    aws = RecoveryAws(control_plane=True)
    forward_state = tmp_path / "forward.json"
    rollback_state = tmp_path / "rollback.json"
    _complete_cutover(
        aws,
        forward_state,
        target_table=RESTORED,
        approval_id="CHG-2026-011",
    )
    _resume(aws, forward_state)

    _complete_cutover(
        aws,
        rollback_state,
        target_table=PRIMARY,
        approval_id="CHG-2026-012",
    )
    resumed = _resume(aws, rollback_state)

    assert resumed["selectedTable"] == PRIMARY
    assert aws.mode == aws.control_mode == "normal"
    assert aws.selected == aws.control_selected == PRIMARY


def test_abort_reverses_both_selectors_while_access_is_blocked(
    tmp_path,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "abort.json"
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }
    agentcore_recovery.quiesce(
        aws,
        approval_id="CHG-2026-013",
        **common,
    )
    agentcore_recovery.select(
        aws,
        expected_table=RESTORED,
        **common,
    )

    aborted = agentcore_recovery.abort(aws, **common)

    assert aborted["phase"] == "aborted"
    assert aws.mode == "normal"
    assert aws.control_mode == "quiesced"
    assert aws.selected == aws.control_selected == PRIMARY
    resumed = _resume(aws, state_file)
    assert resumed["selectedTable"] == PRIMARY
    assert aws.control_mode == "normal"


def test_resume_rejects_phase_selector_and_ownership_mismatches(
    tmp_path,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "mismatch.json"
    _complete_cutover(
        aws,
        state_file,
        target_table=RESTORED,
        approval_id="CHG-2026-014",
    )

    state = json.loads(state_file.read_text())
    state["phase"] = "selected"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="only after promote or abort",
    ):
        _resume(aws, state_file)

    state["phase"] = "promoted"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    aws.control_target_parameter = ""
    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="same selected table",
    ):
        _resume(aws, state_file)

    aws.control_target_parameter = RESTORED
    state["stackId"] = "arn:aws:cloudformation:wrong"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(
        agentcore_recovery.AgentCoreRecoveryError,
        match="does not match the deployed AgentCore runtime",
    ):
        _resume(aws, state_file)


def test_retry_checkpoints_finish_start_promote_and_resume(
    tmp_path,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "retry.json"
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }
    agentcore_recovery.quiesce(
        aws,
        approval_id="CHG-2026-020",
        **common,
    )
    agentcore_recovery.select(
        aws,
        expected_table=RESTORED,
        **common,
    )

    state = json.loads(state_file.read_text())
    state["phase"] = "starting-validation"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    aws.mode = "validation"
    started = agentcore_recovery.start(
        aws,
        expected_table=RESTORED,
        **common,
    )
    assert started["phase"] == "validation"

    state = json.loads(state_file.read_text())
    state["phase"] = "promoting"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    aws.mode = "normal"
    promoted = agentcore_recovery.promote(
        aws,
        expected_table=RESTORED,
        **common,
    )
    assert promoted["phase"] == "promoted"

    aws.control_mode = "normal"
    resumed = _resume(aws, state_file)
    assert resumed["phase"] == "control-plane-resumed"
    assert aws.service["runningCount"] == 2
    assert aws.target["MinCapacity"] == 2


def test_abort_recovers_partial_control_plane_selection(
    tmp_path,
) -> None:
    aws = RecoveryAws(control_plane=True)
    state_file = tmp_path / "partial-select.json"
    common = {
        "region": "us-east-1",
        "stack_name": "AxonLLMAgentCoreStack",
        "state_file": state_file,
        "timeout_seconds": 5,
        "poll_interval": 0,
        "sleep": lambda _: None,
    }
    agentcore_recovery.quiesce(
        aws,
        approval_id="CHG-2026-021",
        **common,
    )
    state = json.loads(state_file.read_text())
    state.update(phase="selecting", targetTable=RESTORED)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    aws.control_mode = "selected"
    aws.control_target_parameter = RESTORED

    aborted = agentcore_recovery.abort(aws, **common)

    assert aborted["phase"] == "aborted"
    assert aws.mode == "normal"
    assert aws.control_mode == "quiesced"
    assert aws.selected == aws.control_selected == PRIMARY
    _resume(aws, state_file)
    assert aws.control_mode == "normal"
