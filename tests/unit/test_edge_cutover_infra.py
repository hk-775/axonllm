"""CDK contracts for reversible Fargate/serverless edge selection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"


def _synth(
    work_dir: Path,
    *,
    target: str,
    extra_context: dict[str, object] | None = None,
) -> dict:
    context: dict[str, object] = {
        "deployment_target": target,
        "region": "us-east-1",
    }
    context.update(extra_context or {})
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(context),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(work_dir / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    stack_name = {
        "control-plane": "AxonLLMControlPlaneStack",
        "serverless-control-plane": ("AxonLLMServerlessControlPlaneStack-contract"),
    }[target]
    return json.loads((out_dir / f"{stack_name}.template.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def edge_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict]:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    root = tmp_path_factory.mktemp("edge-cutover")
    return {
        "legacy": _synth(
            root / "legacy",
            target="control-plane",
        ),
        "ready": _synth(
            root / "ready",
            target="control-plane",
            extra_context={"edge_cutover_enabled": True},
        ),
        "serverless": _synth(
            root / "serverless",
            target="serverless-control-plane",
            extra_context={
                "deployment_namespace": "contract",
                "edge_attachment_enabled": True,
            },
        ),
    }


def _resources(
    template: dict,
    resource_type: str,
) -> list[tuple[str, dict]]:
    return [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    ]


def _one(template: dict, resource_type: str) -> tuple[str, dict]:
    resources = _resources(template, resource_type)
    assert len(resources) == 1
    return resources[0]


def test_legacy_template_has_no_edge_cutover_surface(
    edge_templates: dict[str, dict],
) -> None:
    legacy = edge_templates["legacy"]
    serialized = json.dumps(legacy)

    assert "EdgeBackendMode" not in legacy["Parameters"]
    assert "EdgeMigrationId" not in legacy["Parameters"]
    assert "AxonLLMServerlessApiOrigin" not in serialized
    assert "AxonLLMServerlessStaticOrigin" not in serialized
    assert not _resources(
        legacy,
        "AWS::CloudFront::OriginAccessControl",
    )


def test_prepare_changes_only_three_reviewed_edge_resources(
    edge_templates: dict[str, dict],
) -> None:
    legacy = edge_templates["legacy"]["Resources"]
    ready = edge_templates["ready"]["Resources"]
    changed = {
        logical_id: (
            legacy.get(logical_id, {}).get("Type"),
            ready.get(logical_id, {}).get("Type"),
        )
        for logical_id in set(legacy) | set(ready)
        if legacy.get(logical_id) != ready.get(logical_id)
    }

    assert set(changed.values()) == {
        (
            "AWS::CloudFront::Distribution",
            "AWS::CloudFront::Distribution",
        ),
        (
            "AWS::CloudFront::Function",
            "AWS::CloudFront::Function",
        ),
        (None, "AWS::CloudFront::OriginAccessControl"),
    }
    assert len(changed) == 3


def test_edge_selector_is_fail_closed_and_fargate_by_default(
    edge_templates: dict[str, dict],
) -> None:
    template = edge_templates["ready"]
    parameter = template["Parameters"]["EdgeBackendMode"]
    function_id, function = _one(
        template,
        "AWS::CloudFront::Function",
    )
    code = function["Properties"]["FunctionCode"]
    encoded = json.dumps(code)

    assert parameter["Default"] == "fargate"
    assert parameter["AllowedValues"] == ["fargate", "serverless"]
    assert {"Ref": "EdgeBackendMode"} in code["Fn::Join"][1]
    assert "selectRequestOriginById" in encoded
    assert "AxonLLMServerlessApiOrigin" in encoded
    assert "AxonLLMServerlessStaticOrigin" in encoded
    assert "delete request.headers['x-axon-public-host']" in encoded
    assert function_id


def test_edge_distribution_preserves_vpc_origin_and_adds_exact_origins(
    edge_templates: dict[str, dict],
) -> None:
    template = edge_templates["ready"]
    _, distribution = _one(
        template,
        "AWS::CloudFront::Distribution",
    )
    origins = distribution["Properties"]["DistributionConfig"]["Origins"]
    by_id = {origin["Id"]: origin for origin in origins}
    api = by_id["AxonLLMServerlessApiOrigin"]
    static = by_id["AxonLLMServerlessStaticOrigin"]

    assert len(origins) == 3
    assert any("VpcOriginConfig" in origin for origin in origins)
    assert api["DomainName"] == {"Ref": "ServerlessControlApiDomainName"}
    assert api["OriginPath"] == {"Ref": "ServerlessControlApiOriginPath"}
    assert api["CustomOriginConfig"]["OriginProtocolPolicy"] == ("https-only")
    assert "{{resolve:secretsmanager:" in json.dumps(api["OriginCustomHeaders"])
    assert static["DomainName"] == {"Ref": "ServerlessStaticBucketRegionalDomainName"}
    assert static["S3OriginConfig"] == {"OriginAccessIdentity": ""}


def test_static_oac_is_sigv4_and_outputs_are_cloudfront_scoped(
    edge_templates: dict[str, dict],
) -> None:
    template = edge_templates["ready"]
    _, access_control = _one(
        template,
        "AWS::CloudFront::OriginAccessControl",
    )
    config = access_control["Properties"]["OriginAccessControlConfig"]

    assert config["OriginAccessControlOriginType"] == "s3"
    assert config["SigningBehavior"] == "always"
    assert config["SigningProtocol"] == "sigv4"
    for name in (
        "EdgeBackendMode",
        "EdgeMigrationId",
        "ProductionDistributionArn",
        "ServerlessControlApiDomainName",
        "ServerlessStaticBucketRegionalDomainName",
    ):
        assert template["Outputs"][name]["Condition"] == ("CloudFrontEndpoint")


def test_serverless_attachment_authorizes_exact_production_edge(
    edge_templates: dict[str, dict],
) -> None:
    template = edge_templates["serverless"]
    parameters = template["Parameters"]
    deployment = _one(
        template,
        "Custom::AxonLLMStaticAssets",
    )[1]["Properties"]
    _, browser_client = _one(
        template,
        "AWS::Cognito::UserPoolClient",
    )
    callbacks = json.dumps(browser_client["Properties"]["CallbackURLs"])
    logouts = json.dumps(browser_client["Properties"]["LogoutURLs"])

    assert {
        "ProductionDistributionArn",
        "ProductionDistributionId",
        "ProductionControlPlaneHostname",
    } <= set(parameters)
    assert deployment["AdditionalDistributionId"] == {"Ref": "ProductionDistributionId"}
    assert "ProductionControlPlaneHostname" in callbacks
    assert "ProductionControlPlaneHostname" in logouts
    assert {
        "ControlApiOriginDomainName",
        "ControlApiOriginPath",
        "OriginCredentialSecretArn",
        "ProductionDistributionArn",
        "ProductionDistributionId",
        "StaticSiteBucketRegionalDomainName",
    } <= set(template["Outputs"])


def test_serverless_attachment_bucket_policy_binds_both_distributions(
    edge_templates: dict[str, dict],
) -> None:
    template = edge_templates["serverless"]
    statements = [
        statement
        for _, policy in _resources(
            template,
            "AWS::S3::BucketPolicy",
        )
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Principal") == {"Service": "cloudfront.amazonaws.com"}
    ]
    encoded = json.dumps(statements)
    forbidden_prefixes = (
        "AWS::EC2::",
        "AWS::ECS::",
        "AWS::ElasticLoadBalancing",
    )

    assert len(statements) == 2
    assert "ProductionDistributionArn" in encoded
    assert not [
        resource["Type"]
        for resource in template["Resources"].values()
        if resource["Type"].startswith(forbidden_prefixes)
    ]
