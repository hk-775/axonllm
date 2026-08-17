"""Post-operation verification for AgentCore park and resume."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from src.gateway import cli
from src.gateway.deployment import runtime_lifecycle_status
from src.gateway.deployment.runtime_lifecycle import (
    build_runtime_lifecycle_plan,
    load_runtime_lifecycle_context,
)
from src.gateway.deployment.runtime_lifecycle_status import (
    RuntimeLifecycleStatusError,
    build_runtime_lifecycle_receipt,
    load_runtime_lifecycle_status,
    runtime_lifecycle_receipt_schema,
    runtime_lifecycle_status_schema,
    write_runtime_lifecycle_receipt,
)


_REPO = Path(__file__).resolve().parents[2]
_CONTEXT = _REPO / "config" / "deployment" / "agentcore-runtime-lifecycle.example.json"
_STATUS = _REPO / "config" / "deployment" / "agentcore-runtime-lifecycle-status.example.json"


def _plan() -> dict:
    return build_runtime_lifecycle_plan(load_runtime_lifecycle_context(_CONTEXT))


def _status() -> dict:
    status = load_runtime_lifecycle_status(_STATUS)
    status["plan_id"] = _plan()["plan_id"]
    return status


def _resume() -> tuple[dict, dict]:
    context = load_runtime_lifecycle_context(_CONTEXT)
    context["operation"] = "resume"
    context["current_state"] = "parked"
    context["desired_state"] = "active"
    for name in ("runtime", "managed_network"):
        for change in context[name]["changes"]:
            change["action"] = "Add"
    plan = build_runtime_lifecycle_plan(context)
    status = _status()
    status.update(
        {
            "plan_id": plan["plan_id"],
            "operation": "resume",
            "desired_state": "active",
        }
    )
    status["runtime"].update(
        {
            "template_sha256": context["runtime"]["active_template_sha256"],
            "runtime_count": 1,
            "ready_runtime_count": 1,
            "endpoint_count": 1,
            "ready_endpoint_count": 1,
        }
    )
    status["managed_network"].update(
        {
            "template_sha256": context["managed_network"]["active_template_sha256"],
            "vpc_count": 1,
            "subnet_count": 2,
            "vpc_endpoint_count": 11,
        }
    )
    status["control_plane"]["runtime_health"] = "passed"
    return plan, status


def test_packaged_status_and_receipt_schemas_are_valid() -> None:
    Draft202012Validator.check_schema(runtime_lifecycle_status_schema())
    Draft202012Validator.check_schema(runtime_lifecycle_receipt_schema())


def test_documented_status_is_valid_and_non_secret() -> None:
    status = _status()
    encoded = json.dumps(status).casefold()

    assert status["desired_state"] == "parked"
    assert "secretstring" not in encoded
    assert "password" not in encoded


def test_park_receipt_is_deterministic_and_content_addressed(
    tmp_path: Path,
) -> None:
    first = build_runtime_lifecycle_receipt(_plan(), _status())
    second = build_runtime_lifecycle_receipt(_plan(), _status())
    first_path = write_runtime_lifecycle_receipt(first, tmp_path)
    second_path = write_runtime_lifecycle_receipt(second, tmp_path)

    assert first == second
    assert first["receipt_id"].startswith("sha256:")
    assert first["final_state"] == "parked"
    assert first["execution_order"] == [
        "agentcore-runtime",
        "managed-network",
    ]
    assert first["verification"] == {
        "administration_probe_passed": True,
        "control_plane_available": True,
        "desired_template_active": True,
        "lifecycle_plan_bound": True,
        "network_state_verified": True,
        "retained_stacks_unchanged": True,
        "runtime_state_verified": True,
    }
    assert first_path == second_path
    assert json.loads(first_path.read_text(encoding="ascii")) == first


def test_resume_receipt_requires_ready_runtime_and_network() -> None:
    plan, status = _resume()

    receipt = build_runtime_lifecycle_receipt(plan, status)

    assert receipt["operation"] == "resume"
    assert receipt["final_state"] == "active"
    assert {item["component"] for item in receipt["affected_stacks"]} == {"agentcore-runtime", "managed-network"}


def test_status_must_match_plan_identity() -> None:
    status = _status()
    status["account_id"] = "999999999999"

    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="account_id does not match",
    ):
        build_runtime_lifecycle_receipt(_plan(), status)


def test_park_rejects_remaining_runtime_or_network_resources() -> None:
    runtime = _status()
    runtime["runtime"]["endpoint_count"] = 1
    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="still contains runtime or endpoint",
    ):
        build_runtime_lifecycle_receipt(_plan(), runtime)

    network = _status()
    network["managed_network"]["subnet_count"] = 1
    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="still contains network resources",
    ):
        build_runtime_lifecycle_receipt(_plan(), network)


def test_resume_rejects_partial_readiness() -> None:
    plan, status = _resume()
    status["runtime"]["ready_endpoint_count"] = 0

    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="not fully ready",
    ):
        build_runtime_lifecycle_receipt(plan, status)


def test_observed_template_must_match_desired_template() -> None:
    status = _status()
    status["runtime"]["template_sha256"] = f"sha256:{'f' * 64}"

    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="template does not match",
    ):
        build_runtime_lifecycle_receipt(_plan(), status)


def test_retained_stack_drift_is_rejected() -> None:
    status = _status()
    status["retained_stacks"][0]["stack_state_sha256"] = f"sha256:{'f' * 64}"

    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="retained application-state stack changed",
    ):
        build_runtime_lifecycle_receipt(_plan(), status)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("available", "control plane is unavailable"),
        (
            "administration_probe_passed",
            "administration probe did not pass",
        ),
    ],
)
def test_control_plane_must_remain_available(
    field: str,
    message: str,
) -> None:
    status = _status()
    status["control_plane"][field] = False

    with pytest.raises(RuntimeLifecycleStatusError, match=message):
        build_runtime_lifecycle_receipt(_plan(), status)


def test_customer_owned_network_cannot_report_managed_resources() -> None:
    context = load_runtime_lifecycle_context(_CONTEXT)
    context["network_mode"] = "existing"
    context["managed_network"] = None
    plan = build_runtime_lifecycle_plan(context)
    status = _status()
    status["plan_id"] = plan["plan_id"]

    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="cannot report a managed network",
    ):
        build_runtime_lifecycle_receipt(plan, status)


def test_tampered_receipt_is_rejected(tmp_path: Path) -> None:
    receipt = build_runtime_lifecycle_receipt(_plan(), _status())
    tampered = copy.deepcopy(receipt)
    tampered["final_state"] = "active"

    with pytest.raises(
        RuntimeLifecycleStatusError,
        match="hash does not match",
    ):
        write_runtime_lifecycle_receipt(tampered, tmp_path)


def test_module_has_no_aws_or_execution_path() -> None:
    source = Path(runtime_lifecycle_status.__file__).read_text(encoding="utf-8").casefold()

    assert "import boto3" not in source
    assert "import botocore" not in source
    assert "import subprocess" not in source
    assert "execute-change-set" not in source


def test_cli_writes_machine_readable_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _plan()
    plan_path = tmp_path / "plan.json"
    status_path = tmp_path / "status.json"
    plan_path.write_text(json.dumps(plan), encoding="ascii")
    status = _status()
    status["plan_id"] = plan["plan_id"]
    status_path.write_text(json.dumps(status), encoding="ascii")

    cli.cmd_deploy_lifecycle_receipt(
        SimpleNamespace(
            plan=plan_path,
            status=status_path,
            output_dir=tmp_path / "receipts",
        )
    )

    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "park"
    assert result["final_state"] == "parked"
    assert result["verified"] is True
    assert Path(result["receipt_path"]).is_file()
