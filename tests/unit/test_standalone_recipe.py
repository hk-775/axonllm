"""Contracts for the non-mutating standalone ECS recipe."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from src.gateway import cli
from src.gateway.deployment.standalone_recipe import (
    StandaloneRecipeError,
    build_standalone_ecs_plan,
    load_standalone_ecs_context,
    standalone_ecs_context_schema,
    standalone_ecs_plan_schema,
    validate_standalone_ecs_context,
    write_standalone_ecs_plan,
)

_REPO = Path(__file__).resolve().parents[2]
_CONTEXT_PATH = _REPO / "config" / "deployment" / "standalone-ecs-existing.example.json"
_EVALUATION_COMPOSE = _REPO / "docker-compose.yml"
_PRODUCTION_COMPOSE = _REPO / "deploy" / "standalone" / "compose.production.yml"


def _context() -> dict:
    return load_standalone_ecs_context(_CONTEXT_PATH)


def test_packaged_standalone_schemas_are_valid() -> None:
    Draft202012Validator.check_schema(standalone_ecs_context_schema())
    Draft202012Validator.check_schema(standalone_ecs_plan_schema())


def test_documented_context_builds_a_deterministic_plan() -> None:
    first = build_standalone_ecs_plan(_context())
    second = build_standalone_ecs_plan(_context())

    assert first == second
    assert first["plan_id"].startswith("sha256:")
    assert first["mutating"] is False
    assert first["approval_required"] is True
    assert first["ownership"]["created_network_resources"] == []


def test_task_definition_is_hardened_and_uses_distinct_roles() -> None:
    plan = build_standalone_ecs_plan(_context())
    task = plan["task_definition"]
    container = task["containerDefinitions"][0]

    assert task["networkMode"] == "awsvpc"
    assert task["requiresCompatibilities"] == ["FARGATE"]
    assert task["executionRoleArn"] != task["taskRoleArn"]
    assert task["runtimePlatform"] == {
        "cpuArchitecture": "X86_64",
        "operatingSystemFamily": "LINUX",
    }
    assert container["image"].endswith("2" * 64)
    assert container["user"] == "10001:10001"
    assert container["readonlyRootFilesystem"] is True
    assert container["stopTimeout"] == 45
    assert container["linuxParameters"] == {
        "initProcessEnabled": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["logConfiguration"]["options"]["mode"] == "blocking"
    assert "127.0.0.1:8000/health" in " ".join(container["healthCheck"]["command"])


def test_service_reuses_customer_network_and_rolls_back_failed_deployments() -> None:
    service = build_standalone_ecs_plan(_context())["service"]
    network = service["networkConfiguration"]["awsvpcConfiguration"]

    assert service["desiredCount"] == 2
    assert service["platformVersion"] == "LATEST"
    assert network["assignPublicIp"] == "DISABLED"
    assert len(network["subnets"]) == 2
    assert service["deploymentConfiguration"] == {
        "minimumHealthyPercent": 100,
        "maximumPercent": 200,
        "deploymentCircuitBreaker": {
            "enable": True,
            "rollback": True,
        },
    }
    assert service["loadBalancers"][0]["containerPort"] == 8000
    assert service["enableExecuteCommand"] is False


def test_environment_is_fail_closed_and_secret_values_are_not_embedded() -> None:
    plan = build_standalone_ecs_plan(_context())
    container = plan["task_definition"]["containerDefinitions"][0]
    environment = {item["name"]: item["value"] for item in container["environment"]}

    assert environment["AXON_DEPLOYMENT_PROFILE"] == "production"
    assert environment["AXON_AUTH_MODE"] == "ENFORCE"
    assert environment["AXON_REQUIRE_CANONICAL_IDENTITY"] == "true"
    assert environment["AXON_LOAD_DEMO_DATA"] == "false"
    assert environment["LLM_ROUTER_DYNAMODB_ENABLED"] == "true"
    assert environment["AXON_ROUTING_CONFIG_SIGNING_MODE"] == "sign-verify"
    assert "ANTHROPIC_API_KEY" not in environment
    assert container["secrets"] == [
        {
            "name": "ANTHROPIC_API_KEY",
            "valueFrom": (
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/providers-AbCd12:ANTHROPIC_API_KEY::"
            ),
        }
    ]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda value: value.update(
                image_reference=("123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/standalone:latest")
            ),
            "exact same-account",
        ),
        (
            lambda value: value["task"].update(memory_mib=512),
            "invalid for cpu",
        ),
        (
            lambda value: value["task"].update(task_role_arn=value["task"]["execution_role_arn"]),
            "must be distinct",
        ),
        (
            lambda value: value["state"].update(routing_signing_key_arn=value["state"]["data_key_arn"]),
            "must be distinct",
        ),
        (
            lambda value: value["provider_secrets"][0].update(name="AXON_AUTH_MODE"),
            "not an allowed secret",
        ),
        (
            lambda value: value["provider_secrets"][0].update(value_from="plaintext-secret"),
            "same-account, same-region",
        ),
        (
            lambda value: value.update(providers=["not-a-provider"]),
            "unknown values",
        ),
    ],
)
def test_unsafe_contexts_fail_closed(mutate, message: str) -> None:
    context = _context()
    mutate(context)

    with pytest.raises(StandaloneRecipeError, match=message):
        validate_standalone_ecs_context(context)


def test_artifacts_are_content_addressed_and_owner_only(
    tmp_path: Path,
) -> None:
    plan = build_standalone_ecs_plan(_context())
    first_plan, first_task = write_standalone_ecs_plan(plan, tmp_path)
    second_plan, second_task = write_standalone_ecs_plan(plan, tmp_path)

    assert first_plan == second_plan
    assert first_task == second_task
    assert first_plan.name.startswith("standalone-ecs-plan-")
    assert first_task.name.startswith("task-definition-")
    assert stat.S_IMODE(first_plan.stat().st_mode) == 0o600
    assert stat.S_IMODE(first_task.stat().st_mode) == 0o600
    assert json.loads(first_task.read_text()) == plan["task_definition"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["ownership"].update(created_network_resources=["AWS::EC2::VPC"]),
        lambda plan: plan["service"]["networkConfiguration"]["awsvpcConfiguration"].update(assignPublicIp="ENABLED"),
        lambda plan: plan["task_definition"]["containerDefinitions"][0].update(readonlyRootFilesystem=False),
        lambda plan: plan["task_definition"]["containerDefinitions"][0]["environment"][0].update(
            name="AXON_AUTH_MODE",
            value="LOG_ONLY",
        ),
        lambda plan: plan["task_definition"].update(
            taskRoleArn=("arn:aws:iam::210987654321:role/AxonLLMStandaloneTask")
        ),
        lambda plan: plan["service"].update(cluster=("arn:aws:ecs:us-east-1:210987654321:cluster/customer-production")),
    ],
)
def test_rehashed_plan_cannot_weaken_runtime_safety(
    mutate,
    tmp_path: Path,
) -> None:
    plan = build_standalone_ecs_plan(_context())
    mutate(plan)
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    plan["plan_id"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
    )

    with pytest.raises(StandaloneRecipeError):
        write_standalone_ecs_plan(plan, tmp_path)


def test_cli_writes_only_non_mutating_plan_artifacts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "axon",
            "deploy",
            "standalone-plan",
            "--context",
            str(_CONTEXT_PATH),
            "--output-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["mutating"] is False
    assert output["approval_required"] is True
    assert output["created_network_resources"] == []
    assert Path(output["plan_path"]).is_file()
    assert Path(output["task_definition_path"]).is_file()


def test_planner_has_no_aws_or_subprocess_execution_path() -> None:
    source = (_REPO / "src" / "gateway" / "deployment" / "standalone_recipe.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.partition(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported_roots.update(
        node.module.partition(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    )

    assert "boto3" not in imported_roots
    assert "botocore" not in imported_roots
    assert "subprocess" not in imported_roots


def test_evaluation_compose_explicitly_selects_disposable_behavior() -> None:
    compose = yaml.safe_load(_EVALUATION_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["axonllm"]

    assert "AXON_DEPLOYMENT_PROFILE=${AXON_DEPLOYMENT_PROFILE:-development}" in service["environment"]
    assert "AXON_AUTH_MODE=${AXON_AUTH_MODE:-LOG_ONLY}" in service["environment"]
    assert "AXON_LOAD_DEMO_DATA=${AXON_LOAD_DEMO_DATA:-true}" in service["environment"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]


def test_production_compose_reuses_an_external_network_and_state() -> None:
    compose = yaml.safe_load(_PRODUCTION_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["axonllm"]
    environment = service["environment"]

    assert "build" not in service
    assert service["image"].startswith("${AXON_STANDALONE_IMAGE:?")
    assert environment["AXON_DEPLOYMENT_PROFILE"] == "production"
    assert environment["AXON_AUTH_MODE"] == "ENFORCE"
    assert environment["AXON_LOAD_DEMO_DATA"] == "false"
    assert environment["LLM_ROUTER_DYNAMODB_ENABLED"] == "true"
    assert compose["networks"]["customer"]["external"] is True
    assert "ports" not in service
    assert "127.0.0.1:8000/ready" in service["healthcheck"]["test"][-1]
