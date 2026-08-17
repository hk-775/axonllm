"""Fail-closed planning for the Fargate to serverless edge transition."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from src.gateway import cli
from src.gateway.deployment import edge_transition
from src.gateway.deployment.edge_transition import (
    EdgeTransitionError,
    build_edge_transition_plan,
    edge_transition_context_schema,
    edge_transition_plan_schema,
    load_edge_transition_context,
    write_edge_transition_plan,
)


_REPO = Path(__file__).resolve().parents[2]
_CONTEXT = _REPO / "config" / "deployment" / "agentcore-edge-transition.example.json"
_PRODUCTION_URL = "https://d111111abcdef8.cloudfront.net"
_QUALIFICATION_URL = "https://d222222abcdef8.cloudfront.net"


def _context() -> dict:
    return load_edge_transition_context(_CONTEXT)


def _report(target: str, endpoint: str) -> dict:
    outcomes = [
        (
            "member-read",
            "authenticated_read_allowed",
            "GET",
            "/admin/projects/project-launch",
            [200],
            200,
            None,
        ),
        (
            "viewer-mutation-denied",
            "viewer_mutation_denied",
            "PUT",
            "/admin/projects/project-launch",
            [403],
            403,
            True,
        ),
        (
            "tenant-admin-mutation-round-trip",
            "tenant_admin_mutation_round_trip",
            "PUT",
            "/admin/projects/project-launch",
            [200],
            200,
            None,
        ),
        (
            "cross-tenant-denied",
            "cross_tenant_denied",
            "GET",
            "/admin/projects/project-launch",
            [404],
            404,
            None,
        ),
        (
            "ungranted-project-denied",
            "ungranted_project_denied",
            "GET",
            "/admin/projects/project-launch",
            [404],
            404,
            None,
        ),
    ]
    results = [
        {
            "name": name,
            "category": category,
            "baseUrl": endpoint,
            "method": method,
            "path": path,
            "expectedStatuses": statuses,
            "statusCode": status,
            "queryResponseValidated": None,
            "errorCodeValidated": error_validated,
            "roundTrip": ({"status": "PASS"} if category == "tenant_admin_mutation_round_trip" else None),
            "passed": True,
        }
        for (
            name,
            category,
            method,
            path,
            statuses,
            status,
            error_validated,
        ) in outcomes
    ]
    return {
        "schemaVersion": "axonllm.production-validation/v1",
        "target": target,
        "startedAt": "2026-08-16T18:00:00+00:00",
        "finishedAt": "2026-08-16T18:05:00+00:00",
        "overallStatus": "PASS",
        "httpEndpoints": [endpoint],
        "canaries": {
            "status": "PASS",
            "results": results,
        },
        "load": {
            "status": "PASS",
            "method": "GET",
            "path": "/admin/projects/project-launch",
            "expectedStatuses": [200],
            "requestCountConfigured": 200,
            "concurrency": 20,
            "thresholds": {
                "maxErrorRate": 0.01,
                "maxP95LatencyMs": 750,
            },
        },
        "launchGates": {"status": "PASS"},
    }


def _plan(context: dict | None = None) -> dict:
    return build_edge_transition_plan(
        context or _context(),
        _report("fargate", _PRODUCTION_URL),
        _report("serverless-control", _QUALIFICATION_URL),
    )


def test_packaged_edge_schemas_are_valid() -> None:
    Draft202012Validator.check_schema(edge_transition_context_schema())
    Draft202012Validator.check_schema(edge_transition_plan_schema())


def test_documented_context_is_valid_and_non_secret() -> None:
    context = _context()
    encoded = json.dumps(context).casefold()

    assert context["operation"] == "prepare"
    assert "secretstring" not in encoded
    assert "dynamicreference" not in encoded
    assert "password" not in encoded


def test_plan_is_deterministic_content_addressed_and_non_mutating(
    tmp_path: Path,
) -> None:
    first = _plan()
    second = _plan()
    first_path = write_edge_transition_plan(first, tmp_path)
    second_path = write_edge_transition_plan(second, tmp_path)

    assert first == second
    assert first["plan_id"].startswith("sha256:")
    assert first["mutating"] is False
    assert first["approval_required"] is True
    assert first_path == second_path
    assert json.loads(first_path.read_text(encoding="ascii")) == first


@pytest.mark.parametrize(
    ("operation", "current", "desired"),
    [
        ("prepare", "fargate", "fargate"),
        ("cutover", "fargate", "serverless"),
        ("rollback", "serverless", "fargate"),
    ],
)
def test_legal_edge_transitions(
    operation: str,
    current: str,
    desired: str,
) -> None:
    context = _context()
    context["operation"] = operation
    context["production"]["current_backend"] = current
    context["production"]["desired_backend"] = desired
    if operation != "prepare":
        context["change_set"]["changes"] = [
            {
                "logical_id": "StripUntrustedIdentityHeaders",
                "resource_type": "AWS::CloudFront::Function",
                "action": "Modify",
                "replacement": "False",
            }
        ]

    plan = _plan(context)

    assert plan["operation"] == operation
    assert plan["production"]["desired_backend"] == desired


def test_illegal_backend_transition_is_rejected() -> None:
    context = _context()
    context["operation"] = "cutover"
    context["production"]["desired_backend"] = "fargate"

    with pytest.raises(
        EdgeTransitionError,
        match="illegal edge backend transition",
    ):
        _plan(context)


def test_state_or_hostname_drift_is_rejected() -> None:
    state_drift = _context()
    state_drift["serverless"]["state_table_name"] = "other-state"
    with pytest.raises(
        EdgeTransitionError,
        match="share canonical state",
    ):
        _plan(state_drift)

    hostname_drift = _context()
    hostname_drift["serverless"]["production_hostname"] = "different.example.com"
    with pytest.raises(
        EdgeTransitionError,
        match="preserve the production hostname",
    ):
        _plan(hostname_drift)


def test_missing_supplemental_domain_is_rejected() -> None:
    context = _context()
    context["supplemental_evidence"] = context["supplemental_evidence"][:-1]

    with pytest.raises(EdgeTransitionError):
        _plan(context)


def test_non_edge_or_replacement_change_is_rejected() -> None:
    non_edge = _context()
    non_edge["change_set"]["changes"][0].update(
        {
            "action": "Modify",
            "resource_type": "AWS::CloudFront::OriginAccessControl",
        }
    )
    with pytest.raises(
        EdgeTransitionError,
        match="contains a non-edge change",
    ):
        _plan(non_edge)

    replacement = _context()
    replacement["change_set"]["changes"][1]["replacement"] = "True"
    with pytest.raises(EdgeTransitionError):
        _plan(replacement)


def test_failed_or_mismatched_reports_are_rejected() -> None:
    failed = _report("serverless-control", _QUALIFICATION_URL)
    failed["overallStatus"] = "FAIL"
    with pytest.raises(
        EdgeTransitionError,
        match="did not pass",
    ):
        build_edge_transition_plan(
            _context(),
            _report("fargate", _PRODUCTION_URL),
            failed,
        )

    mismatched = _report(
        "serverless-control",
        _QUALIFICATION_URL,
    )
    mismatched["canaries"]["results"][0]["statusCode"] = 202
    with pytest.raises(
        EdgeTransitionError,
        match="canary outcomes differ",
    ):
        build_edge_transition_plan(
            _context(),
            _report("fargate", _PRODUCTION_URL),
            mismatched,
        )


def test_every_bound_input_changes_the_plan_id() -> None:
    baseline = _plan()["plan_id"]
    mutations = [
        lambda value: value.update(deployment_plan_id=f"sha256:{'f' * 64}"),
        lambda value: value["change_set"].update(template_sha256=f"sha256:{'f' * 64}"),
        lambda value: value["rollback"].update(window_hours=48),
        lambda value: value["serverless"].update(control_api_sha256="f" * 64),
    ]

    for mutation in mutations:
        context = _context()
        mutation(context)
        assert _plan(context)["plan_id"] != baseline


def test_module_has_no_aws_or_execution_path() -> None:
    source = Path(edge_transition.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    }

    assert "boto3" not in imports
    assert "botocore" not in imports
    assert "subprocess" not in imports
    assert not hasattr(edge_transition, "apply_edge_transition_plan")


def test_tampered_plan_is_rejected(tmp_path: Path) -> None:
    plan = _plan()
    tampered = copy.deepcopy(plan)
    tampered["production"]["desired_backend"] = "serverless"

    with pytest.raises(
        EdgeTransitionError,
        match="hash does not match",
    ):
        write_edge_transition_plan(tampered, tmp_path)


def test_cli_writes_machine_readable_non_mutating_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_path = tmp_path / "legacy.json"
    serverless_path = tmp_path / "serverless.json"
    legacy_path.write_text(
        json.dumps(_report("fargate", _PRODUCTION_URL)),
        encoding="ascii",
    )
    serverless_path.write_text(
        json.dumps(_report("serverless-control", _QUALIFICATION_URL)),
        encoding="ascii",
    )
    output = tmp_path / "plans"

    cli.cmd_deploy_edge_plan(
        SimpleNamespace(
            context=_CONTEXT,
            legacy_report=legacy_path,
            serverless_report=serverless_path,
            output_dir=output,
        )
    )

    result = json.loads(capsys.readouterr().out)
    assert result["mutating"] is False
    assert result["approval_required"] is True
    assert result["current_backend"] == "fargate"
    assert result["desired_backend"] == "fargate"
    assert Path(result["plan_path"]).is_file()
