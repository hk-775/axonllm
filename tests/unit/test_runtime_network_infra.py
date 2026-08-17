"""CDK contracts for AgentCore runtime network ownership modes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_FORBIDDEN_CUSTOMER_NETWORK_TYPES = {
    "AWS::EC2::EIP",
    "AWS::EC2::InternetGateway",
    "AWS::EC2::NatGateway",
    "AWS::EC2::NetworkAcl",
    "AWS::EC2::Route",
    "AWS::EC2::RouteTable",
    "AWS::EC2::Subnet",
    "AWS::EC2::SubnetRouteTableAssociation",
    "AWS::EC2::VPC",
    "AWS::EC2::VPCGatewayAttachment",
    "AWS::EC2::VPCEndpoint",
}
_EXISTING_CONTEXT = {
    "runtime_network_mode": "existing",
    "runtime_network_egress_mode": "existing-egress",
    "runtime_network_vpc_id": "vpc-0123456789abcdef0",
    "runtime_network_vpc_cidr": "10.20.0.0/16",
    "runtime_network_private_subnet_ids": [
        "subnet-0123456789abcdef0",
        "subnet-0fedcba9876543210",
    ],
    "runtime_network_availability_zones": [
        "us-east-1a",
        "us-east-1c",
    ],
    "runtime_network_security_group_ids": [],
}


def _synth(
    tmp_path: Path,
    *,
    context: dict[str, object],
    expected_stack: str = "AxonLLMAgentCoreStack",
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    out_dir = tmp_path / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_target": "agentcore",
                    "region": "us-east-1",
                    **context,
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(tmp_path / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout)
    return json.loads((out_dir / f"{expected_stack}.template.json").read_text(encoding="utf-8"))


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


def _runtime(template: dict) -> dict:
    runtimes = _resources(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )
    assert len(runtimes) == 1
    return runtimes[0]["Properties"]


def _routing_seeder(template: dict) -> dict:
    matches = [
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("RoutingConfigSeederHandler") and resource["Type"] == "AWS::Lambda::Function"
    ]
    assert len(matches) == 1
    return matches[0]["Properties"]


def _resource_types(template: dict) -> set[str]:
    return {resource["Type"] for resource in template["Resources"].values()}


def test_explicit_legacy_mode_is_model_identical(
    tmp_path: Path,
) -> None:
    default = _synth(tmp_path / "default", context={})
    explicit = _synth(
        tmp_path / "explicit",
        context={"runtime_network_mode": "legacy"},
    )
    assert explicit == default


def test_existing_mode_does_not_create_customer_networking(
    tmp_path: Path,
) -> None:
    template = _synth(
        tmp_path,
        context=_EXISTING_CONTEXT,
    )
    assert _FORBIDDEN_CUSTOMER_NETWORK_TYPES.isdisjoint(_resource_types(template))
    assert len(_resources(template, "AWS::EC2::SecurityGroup")) == 1

    network = _runtime(template)["NetworkConfiguration"]
    assert network["NetworkMode"] == "VPC"
    assert network["NetworkModeConfig"]["Subnets"] == (_EXISTING_CONTEXT["runtime_network_private_subnet_ids"])
    assert len(network["NetworkModeConfig"]["SecurityGroups"]) == 1
    assert (
        _routing_seeder(template)["VpcConfig"]["SubnetIds"] == (_EXISTING_CONTEXT["runtime_network_private_subnet_ids"])
    )


def test_existing_supplied_security_groups_are_imported_read_only(
    tmp_path: Path,
) -> None:
    security_groups = [
        "sg-0123456789abcdef0",
        "sg-0fedcba9876543210",
    ]
    template = _synth(
        tmp_path,
        context={
            **_EXISTING_CONTEXT,
            "runtime_network_egress_mode": "endpoints-only",
            "runtime_network_security_group_ids": security_groups,
        },
    )
    assert not any(resource_type.startswith("AWS::EC2::") for resource_type in _resource_types(template))

    network = _runtime(template)["NetworkConfiguration"]
    assert network["NetworkModeConfig"]["SecurityGroups"] == security_groups
    assert _routing_seeder(template)["VpcConfig"]["SecurityGroupIds"] == security_groups
    assert "ApprovedHttpsPrefixListId" not in template["Parameters"]
    assert "ApprovedHttpsPrefixListId" not in template["Outputs"]


def test_managed_descriptor_is_consumed_without_recreating_network(
    tmp_path: Path,
) -> None:
    security_groups = ["sg-0123456789abcdef0"]
    template = _synth(
        tmp_path,
        context={
            **_EXISTING_CONTEXT,
            "runtime_network_mode": "managed",
            "runtime_network_egress_mode": "endpoints-only",
            "runtime_network_security_group_ids": security_groups,
        },
    )
    assert not any(resource_type.startswith("AWS::EC2::") for resource_type in _resource_types(template))
    network = _runtime(template)["NetworkConfiguration"]
    assert network["NetworkModeConfig"]["SecurityGroups"] == security_groups


def test_public_development_mode_has_no_ec2_resources(
    tmp_path: Path,
) -> None:
    template = _synth(
        tmp_path,
        context={
            "deployment_namespace": "dev",
            "deployment_profile": "development",
            "runtime_network_mode": "public",
        },
        expected_stack="AxonLLMAgentCoreStack-dev",
    )
    assert not any(resource_type.startswith("AWS::EC2::") for resource_type in _resource_types(template))
    assert _runtime(template)["NetworkConfiguration"] == {"NetworkMode": "PUBLIC"}
    assert "VpcConfig" not in _routing_seeder(template)
    assert "ApprovedHttpsPrefixListId" not in template["Parameters"]
    assert "ApprovedHttpsPrefixListId" not in template["Outputs"]
    assert "AuthorizerConfiguration" in _runtime(template)


@pytest.mark.parametrize(
    "context, expected",
    [
        (
            {"runtime_network_mode": "public"},
            "deployment_profile=development",
        ),
        (
            {
                **_EXISTING_CONTEXT,
                "runtime_network_egress_mode": "managed-nat",
            },
            "existing runtime networking cannot use managed-nat",
        ),
        (
            {
                **_EXISTING_CONTEXT,
                "runtime_network_mode": "managed",
                "runtime_network_egress_mode": "existing-egress",
                "runtime_network_security_group_ids": ["sg-0123456789abcdef0"],
            },
            "managed runtime networking cannot use existing-egress",
        ),
    ],
)
def test_unsafe_network_context_fails_synthesis(
    tmp_path: Path,
    context: dict[str, object],
    expected: str,
) -> None:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    out_dir = tmp_path / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_target": "agentcore",
                    "region": "us-east-1",
                    **context,
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(tmp_path / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode != 0
    assert expected in completed.stdout
