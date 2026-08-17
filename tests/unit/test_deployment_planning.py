from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from src.gateway import cli
from src.gateway.deployment import planning
from src.gateway.deployment.config_contract import load_deployment_config
from src.gateway.deployment.planning import (
    DeploymentPlanError,
    build_deployment_plan,
    deployment_descriptor_schema,
    deployment_plan_context_schema,
    deployment_plan_schema,
    load_deployment_plan_context,
    validate_deployment_plan_context,
    write_deployment_plan,
)

_REPO = Path(__file__).resolve().parents[2]
_CONFIG_DIRECTORY = _REPO / "config" / "deployment"
_CONTEXT_PATH = _CONFIG_DIRECTORY / "agentcore-plan-context.example.json"


def _config(name: str = "agentcore-existing-vpc.yaml") -> dict:
    return load_deployment_config(_CONFIG_DIRECTORY / name)


def _context() -> dict:
    return load_deployment_plan_context(_CONTEXT_PATH)


def _entry(plan: dict, name: str) -> dict:
    return next(item for item in plan["inventory"] if item["name"] == name)


def test_packaged_planning_context_schema_is_valid() -> None:
    Draft202012Validator.check_schema(deployment_plan_context_schema())


def test_packaged_output_schemas_are_valid() -> None:
    Draft202012Validator.check_schema(deployment_descriptor_schema())
    Draft202012Validator.check_schema(deployment_plan_schema())


def test_documented_planning_context_validates() -> None:
    context = _context()

    assert context["schema_version"] == 1
    assert context["images"]["agentcore"].endswith("1" * 64)


def test_plan_is_deterministic_and_content_addressed(tmp_path: Path) -> None:
    plan_a = build_deployment_plan(_config(), _context())
    plan_b = build_deployment_plan(_config(), _context())

    first_plan_path, first_descriptor_path = write_deployment_plan(plan_a, tmp_path)
    first_plan_bytes = first_plan_path.read_bytes()
    first_descriptor_bytes = first_descriptor_path.read_bytes()
    second_plan_path, second_descriptor_path = write_deployment_plan(plan_b, tmp_path)

    assert plan_a == plan_b
    assert plan_a["plan_id"].startswith("sha256:")
    assert plan_a["descriptor"]["descriptor_id"].startswith("sha256:")
    assert second_plan_path == first_plan_path
    assert second_descriptor_path == first_descriptor_path
    assert second_plan_path.read_bytes() == first_plan_bytes
    assert second_descriptor_path.read_bytes() == first_descriptor_bytes


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config, context: context.update(account_id="210987654321"),
        lambda config, context: context["source"].update(
            sha256=f"sha256:{'5' * 64}",
        ),
        lambda config, context: context["images"].update(
            agentcore=(f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm-agentcore@sha256:{'6' * 64}"),
        ),
        lambda config, context: context["stacks"][0].update(
            template_sha256=f"sha256:{'7' * 64}",
        ),
        lambda config, context: context["stacks"][0].update(
            stack_state_sha256=f"sha256:{'8' * 64}",
        ),
        lambda config, context: context["descriptor"]["resources"][0].update(
            identifier="arn:aws:dynamodb:us-east-1:123456789012:table/other-state",
        ),
        lambda config, context: config.update(region="us-west-2"),
    ],
    ids=[
        "account",
        "source",
        "image",
        "template",
        "stack-state",
        "descriptor",
        "configuration",
    ],
)
def test_every_deployment_input_invalidates_plan(mutation) -> None:
    config = _config()
    context = _context()
    baseline = build_deployment_plan(config, context)["plan_id"]

    mutation(config, context)

    assert build_deployment_plan(config, context)["plan_id"] != baseline


def test_descriptor_is_normalized_before_hashing() -> None:
    context_a = _context()
    context_b = _context()
    extra = {
        "identifier": "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012",
        "name": "data-key",
        "ownership": "axonllm",
        "resource_type": "AWS::KMS::Key",
    }
    context_a["descriptor"]["resources"].append(extra)
    context_b["descriptor"]["resources"].insert(0, copy.deepcopy(extra))
    context_a["descriptor"]["release_evidence_ids"].append("github-run:987654321")
    context_b["descriptor"]["release_evidence_ids"].insert(0, "github-run:987654321")

    plan_a = build_deployment_plan(_config(), context_a)
    plan_b = build_deployment_plan(_config(), context_b)

    assert plan_a["descriptor"] == plan_b["descriptor"]
    assert plan_a["plan_id"] == plan_b["plan_id"]


def test_existing_network_is_customer_owned_and_creates_no_nat_or_vpc() -> None:
    plan = build_deployment_plan(_config(), _context())
    by_name = {item["name"]: item for item in plan["inventory"]}

    assert by_name["runtime-vpc"]["ownership"] == "customer"
    assert by_name["runtime-private-subnets"]["ownership"] == "customer"
    assert "runtime-nat-gateways" not in by_name
    assert not any(
        item["ownership"] == "axonllm" and item["kind"] in {"vpc", "private-subnets"} for item in plan["inventory"]
    )


def test_managed_bedrock_network_has_endpoints_and_no_nat() -> None:
    plan = build_deployment_plan(
        _config("agentcore-managed-bedrock.yaml"),
        _context(),
    )
    by_name = {item["name"]: item for item in plan["inventory"]}

    assert by_name["runtime-vpc"]["ownership"] == "axonllm"
    assert by_name["runtime-interface-endpoints"]["quantity"] == 9
    assert by_name["runtime-gateway-endpoints"]["quantity"] == 2
    assert "runtime-nat-gateways" not in by_name
    assert plan["summary"]["chargeable_networking"] is True


def test_managed_external_network_exposes_nat_cost_and_quantity() -> None:
    plan = build_deployment_plan(
        _config("agentcore-managed-external.yaml"),
        _context(),
    )
    nat = _entry(plan, "runtime-nat-gateways")

    assert nat["quantity"] == 2
    assert nat["cost_classes"] == ["hourly", "request-based"]
    assert plan["summary"]["chargeable_networking"] is True


def test_parked_runtime_marks_disposable_network_absent() -> None:
    config = _config("agentcore-managed-external.yaml")
    config["runtime"]["state"] = "parked"

    plan = build_deployment_plan(config, _context())

    assert _entry(plan, "agentcore-runtime")["desired"] == "absent"
    assert _entry(plan, "runtime-vpc")["desired"] == "absent"
    assert _entry(plan, "runtime-nat-gateways")["desired"] == "absent"
    assert _entry(plan, "application-state")["desired"] == "present"
    assert plan["summary"]["chargeable_networking"] is False


def test_public_development_plan_contains_no_runtime_network_resources() -> None:
    plan = build_deployment_plan(
        _config("agentcore-public-development.yaml"),
        _context(),
    )
    names = {item["name"] for item in plan["inventory"]}

    assert "runtime-vpc" not in names
    assert "runtime-private-subnets" not in names
    assert "runtime-nat-gateways" not in names


def test_change_summary_surfaces_replacements() -> None:
    context = _context()
    context["stacks"][0]["changes"] = [
        {
            "logical_id": "PrimaryTable",
            "resource_type": "AWS::DynamoDB::Table",
            "action": "Modify",
            "replacement": "Conditional",
        }
    ]

    plan = build_deployment_plan(_config(), context)

    assert plan["summary"]["replacement_review_required"] is True
    assert plan["summary"]["replacement_count"] == 1
    assert plan["summary"]["change_counts_by_action"]["Modify"] == 1


def test_non_modify_change_cannot_claim_replacement() -> None:
    context = _context()
    context["stacks"][0]["changes"] = [
        {
            "logical_id": "Runtime",
            "resource_type": "AWS::BedrockAgentCore::Runtime",
            "action": "Add",
            "replacement": "True",
        }
    ]

    with pytest.raises(
        DeploymentPlanError,
        match="replacement must be NotApplicable for Add",
    ):
        validate_deployment_plan_context(context)


def test_duplicate_stack_names_fail_closed() -> None:
    context = _context()
    context["stacks"].append(copy.deepcopy(context["stacks"][0]))

    with pytest.raises(DeploymentPlanError, match="duplicate stack name"):
        validate_deployment_plan_context(context)


def test_duplicate_change_logical_ids_fail_closed() -> None:
    context = _context()
    change = {
        "logical_id": "Runtime",
        "resource_type": "AWS::BedrockAgentCore::Runtime",
        "action": "Add",
        "replacement": "NotApplicable",
    }
    context["stacks"][0]["changes"] = [change, copy.deepcopy(change)]

    with pytest.raises(DeploymentPlanError, match="duplicate logical ID"):
        validate_deployment_plan_context(context)


def test_duplicate_descriptor_bindings_fail_closed() -> None:
    context = _context()
    context["descriptor"]["resources"].append(copy.deepcopy(context["descriptor"]["resources"][0]))

    with pytest.raises(DeploymentPlanError, match="duplicate binding name"):
        validate_deployment_plan_context(context)


@pytest.mark.parametrize(
    "identifier",
    [
        "https://example.com/resource?token=not-a-real-secret",
        "https://user:password@example.com/resource",
        "arn:aws:s3:::bucket/object#fragment",
    ],
)
def test_descriptor_rejects_secret_bearing_identifier_shapes(identifier: str) -> None:
    context = _context()
    context["descriptor"]["resources"][0]["identifier"] = identifier

    with pytest.raises(DeploymentPlanError):
        validate_deployment_plan_context(context)


def test_image_must_be_digest_pinned() -> None:
    context = _context()
    context["images"]["agentcore"] = "123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm-agentcore:v0.3.1"

    with pytest.raises(DeploymentPlanError):
        validate_deployment_plan_context(context)


def test_context_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    raw = _CONTEXT_PATH.read_text(encoding="utf-8")
    path = tmp_path / "duplicate.json"
    path.write_text(
        raw.replace(
            '"account_id": "123456789012",',
            '"account_id": "123456789012",\n  "account_id": "210987654321",',
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentPlanError, match="duplicate JSON field"):
        load_deployment_plan_context(path)


def test_tampered_plan_is_not_written(tmp_path: Path) -> None:
    plan = build_deployment_plan(_config(), _context())
    plan["summary"]["chargeable_networking"] = not plan["summary"]["chargeable_networking"]

    with pytest.raises(DeploymentPlanError, match="plan hash does not match"):
        write_deployment_plan(plan, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_cli_writes_plan_and_machine_readable_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = SimpleNamespace(
        config=_CONFIG_DIRECTORY / "agentcore-existing-vpc.yaml",
        context=_CONTEXT_PATH,
        output_dir=tmp_path,
    )

    cli.cmd_deploy_plan(args)

    result = json.loads(capsys.readouterr().out)
    assert result["mutating"] is False
    assert Path(result["plan_path"]).is_file()
    assert Path(result["descriptor_path"]).is_file()


def test_cli_has_no_apply_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["axon", "deploy", "apply"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2


def test_planner_has_no_aws_or_process_dependency() -> None:
    source = Path(planning.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.partition(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported_roots.update(
        node.module.partition(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imported_roots.isdisjoint({"boto3", "botocore", "subprocess"})
    assert not hasattr(planning, "apply_deployment_plan")
    assert not hasattr(planning, "execute_change_set")
