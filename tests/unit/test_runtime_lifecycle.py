"""Fail-closed planning for AgentCore park and resume operations."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from src.gateway import cli
from src.gateway.deployment import runtime_lifecycle
from src.gateway.deployment.runtime_lifecycle import (
    RuntimeLifecycleError,
    build_runtime_lifecycle_plan,
    load_runtime_lifecycle_context,
    runtime_lifecycle_context_schema,
    runtime_lifecycle_plan_schema,
    validate_runtime_lifecycle_context,
    validate_runtime_lifecycle_plan,
    write_runtime_lifecycle_plan,
)


_REPO = Path(__file__).resolve().parents[2]
_CONTEXT = _REPO / "config" / "deployment" / "agentcore-runtime-lifecycle.example.json"


def _context() -> dict:
    return load_runtime_lifecycle_context(_CONTEXT)


def _resume_context() -> dict:
    context = _context()
    context["operation"] = "resume"
    context["current_state"] = "parked"
    context["desired_state"] = "active"
    for stack_name in ("runtime", "managed_network"):
        stack = context[stack_name]
        for change in stack["changes"]:
            change["action"] = "Add"
    return context


def _plan(context: dict | None = None) -> dict:
    return build_runtime_lifecycle_plan(context or _context())


def _rehash(plan: dict) -> None:
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    encoded = json.dumps(
        body,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    plan["plan_id"] = f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_packaged_runtime_lifecycle_schemas_are_valid() -> None:
    Draft202012Validator.check_schema(runtime_lifecycle_context_schema())
    Draft202012Validator.check_schema(runtime_lifecycle_plan_schema())


def test_documented_park_context_is_valid_and_non_secret() -> None:
    context = _context()
    encoded = json.dumps(context).casefold()

    assert context["operation"] == "park"
    assert context["runtime"]["external_state_mode"] is True
    assert "secretstring" not in encoded
    assert "dynamicreference" not in encoded
    assert "password" not in encoded


def test_park_plan_is_deterministic_and_content_addressed(
    tmp_path: Path,
) -> None:
    first = _plan()
    second = _plan()
    first_path = write_runtime_lifecycle_plan(first, tmp_path)
    second_path = write_runtime_lifecycle_plan(second, tmp_path)

    assert first == second
    assert first["plan_id"].startswith("sha256:")
    assert first["mutating"] is False
    assert first["approval_required"] is True
    assert first["execution_order"] == [
        "agentcore-runtime",
        "managed-network",
    ]
    assert first["expected_state"] == {
        "administration": "available",
        "agentcore_runtime": "absent",
        "control_plane": "available",
        "durable_state": "retained",
        "managed_network": "absent",
        "runtime_endpoint": "absent",
    }
    assert first_path == second_path
    assert json.loads(first_path.read_text(encoding="ascii")) == first


def test_resume_plan_restores_bound_release_and_configuration() -> None:
    context = _resume_context()

    plan = _plan(context)

    assert plan["operation"] == "resume"
    assert plan["expected_state"]["agentcore_runtime"] == "ready"
    assert plan["expected_state"]["runtime_endpoint"] == "ready"
    assert plan["expected_state"]["managed_network"] == "ready"
    assert plan["execution_order"] == [
        "managed-network",
        "agentcore-runtime",
    ]
    assert plan["rollback"]["operation"] == "park"
    assert plan["inputs"]["agentcore_image"] == context["runtime"]["image_reference"]
    assert plan["inputs"]["last_known_good_configuration_sha256"] == (context["last_known_good_configuration_sha256"])


@pytest.mark.parametrize(
    ("operation", "current", "desired"),
    [
        ("park", "parked", "active"),
        ("resume", "active", "parked"),
    ],
)
def test_illegal_lifecycle_transition_is_rejected(
    operation: str,
    current: str,
    desired: str,
) -> None:
    context = _context()
    context["operation"] = operation
    context["current_state"] = current
    context["desired_state"] = desired

    with pytest.raises(
        RuntimeLifecycleError,
        match="illegal runtime state transition",
    ):
        validate_runtime_lifecycle_context(context)


@pytest.mark.parametrize(
    ("network_mode", "expected"),
    [
        ("existing", "customer-owned"),
        ("public", "public"),
    ],
)
def test_customer_owned_network_modes_do_not_mutate_network_stack(
    network_mode: str,
    expected: str,
) -> None:
    context = _context()
    context["network_mode"] = network_mode
    context["managed_network"] = None

    plan = _plan(context)

    assert len(plan["affected_stacks"]) == 1
    assert plan["execution_order"] == ["agentcore-runtime"]
    assert plan["expected_state"]["managed_network"] == expected


def test_managed_network_evidence_is_required_only_for_managed_mode() -> None:
    missing = _context()
    missing["managed_network"] = None
    with pytest.raises(
        RuntimeLifecycleError,
        match="managed network mode requires",
    ):
        validate_runtime_lifecycle_context(missing)

    unexpected = _context()
    unexpected["network_mode"] = "existing"
    with pytest.raises(
        RuntimeLifecycleError,
        match="cannot mutate a managed-network stack",
    ):
        validate_runtime_lifecycle_context(unexpected)


def test_external_state_is_mandatory() -> None:
    context = _context()
    context["runtime"]["external_state_mode"] = False

    with pytest.raises(RuntimeLifecycleError):
        validate_runtime_lifecycle_context(context)


def test_change_sets_are_account_region_and_operation_bound() -> None:
    wrong_account = _context()
    wrong_account["runtime"]["change_set_arn"] = wrong_account["runtime"]["change_set_arn"].replace(
        "123456789012", "999999999999"
    )
    with pytest.raises(
        RuntimeLifecycleError,
        match="not bound to the deployment account and region",
    ):
        validate_runtime_lifecycle_context(wrong_account)

    wrong_action = _context()
    wrong_action["runtime"]["changes"][0]["action"] = "Add"
    with pytest.raises(
        RuntimeLifecycleError,
        match="must contain only Remove actions",
    ):
        validate_runtime_lifecycle_context(wrong_action)


def test_runtime_and_managed_vpc_must_appear_in_change_sets() -> None:
    missing_runtime = _context()
    missing_runtime["runtime"]["changes"][0]["resource_type"] = "AWS::Logs::LogGroup"
    with pytest.raises(
        RuntimeLifecycleError,
        match="lacks AWS::BedrockAgentCore::Runtime",
    ):
        validate_runtime_lifecycle_context(missing_runtime)

    missing_vpc = _context()
    missing_vpc["managed_network"]["changes"][0]["resource_type"] = "AWS::EC2::Subnet"
    with pytest.raises(
        RuntimeLifecycleError,
        match="lacks AWS::EC2::VPC",
    ):
        validate_runtime_lifecycle_context(missing_vpc)


@pytest.mark.parametrize(
    "resource_type",
    [
        "AWS::DynamoDB::Table",
        "AWS::KMS::Key",
        "AWS::S3::Bucket",
        "AWS::SecretsManager::Secret",
        "AWS::SQS::Queue",
    ],
)
def test_protected_stateful_resources_are_rejected(
    resource_type: str,
) -> None:
    context = _context()
    context["runtime"]["changes"][1]["resource_type"] = resource_type

    with pytest.raises(
        RuntimeLifecycleError,
        match="touches protected resource type",
    ):
        validate_runtime_lifecycle_context(context)


def test_retained_stack_set_is_exact_unique_and_disjoint() -> None:
    duplicate_kind = _context()
    duplicate_kind["retained_stacks"][3]["kind"] = "identity"
    with pytest.raises(
        RuntimeLifecycleError,
        match="retained stack kinds do not match",
    ):
        validate_runtime_lifecycle_context(duplicate_kind)

    duplicate_name = _context()
    duplicate_name["retained_stacks"][3]["stack_name"] = duplicate_name["retained_stacks"][0]["stack_name"]
    with pytest.raises(
        RuntimeLifecycleError,
        match="retained stack names must be unique",
    ):
        validate_runtime_lifecycle_context(duplicate_name)

    overlap = _context()
    overlap["retained_stacks"][0]["stack_name"] = overlap["runtime"]["stack_name"]
    with pytest.raises(
        RuntimeLifecycleError,
        match="cannot mutate retained stacks",
    ):
        validate_runtime_lifecycle_context(overlap)


def test_image_must_be_digest_pinned_to_account_and_region() -> None:
    context = _context()
    context["runtime"]["image_reference"] = context["runtime"]["image_reference"].replace(
        "123456789012", "999999999999"
    )

    with pytest.raises(
        RuntimeLifecycleError,
        match="not digest-pinned",
    ):
        validate_runtime_lifecycle_context(context)


def test_every_bound_input_changes_the_plan_id() -> None:
    baseline = _plan()["plan_id"]
    mutations = [
        lambda value: value.update(deployment_plan_id=f"sha256:{'f' * 64}"),
        lambda value: value["runtime"].update(current_stack_state_sha256=f"sha256:{'f' * 64}"),
        lambda value: value["retained_stacks"][0].update(stack_state_sha256=f"sha256:{'f' * 64}"),
        lambda value: value.update(rollback_window_hours=48),
    ]

    for mutation in mutations:
        context = _context()
        mutation(context)
        assert _plan(context)["plan_id"] != baseline


def test_tampered_plan_is_rejected(tmp_path: Path) -> None:
    plan = _plan()
    tampered = copy.deepcopy(plan)
    tampered["desired_state"] = "active"

    with pytest.raises(
        RuntimeLifecycleError,
        match="hash does not match",
    ):
        write_runtime_lifecycle_plan(tampered, tmp_path)


def test_rehashed_plan_with_unsafe_semantics_is_rejected() -> None:
    wrong_order = _plan()
    wrong_order["execution_order"].reverse()
    _rehash(wrong_order)
    with pytest.raises(
        RuntimeLifecycleError,
        match="execution order is invalid",
    ):
        validate_runtime_lifecycle_plan(wrong_order)

    protected = _plan()
    protected["affected_stacks"][0]["changes"][1]["resource_type"] = "AWS::DynamoDB::Table"
    _rehash(protected)
    with pytest.raises(
        RuntimeLifecycleError,
        match="protected resource type",
    ):
        validate_runtime_lifecycle_plan(protected)


def test_module_has_no_aws_or_execution_path() -> None:
    source = Path(runtime_lifecycle.__file__).read_text(encoding="utf-8").casefold()

    assert "import boto3" not in source
    assert "import botocore" not in source
    assert "import subprocess" not in source
    assert "execute-change-set" not in source
    assert not hasattr(
        runtime_lifecycle,
        "apply_runtime_lifecycle_plan",
    )


def test_cli_writes_machine_readable_non_mutating_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "plans"

    cli.cmd_deploy_lifecycle_plan(
        SimpleNamespace(
            context=_CONTEXT,
            output_dir=output,
        )
    )

    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "park"
    assert result["mutating"] is False
    assert result["approval_required"] is True
    assert result["current_state"] == "active"
    assert result["desired_state"] == "parked"
    assert Path(result["plan_path"]).is_file()
