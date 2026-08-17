"""CDK contracts for the disposable AgentCore managed-network stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_BASE_CONTEXT = {
    "deployment_target": "managed-network",
    "region": "us-east-1",
    "deployment_profile": "production",
    "managed_network_vpc_cidr": "10.42.0.0/16",
    "managed_network_availability_zones": [
        "us-east-1a",
        "us-east-1c",
    ],
    "managed_network_availability_zone_ids": [
        "use1-az4",
        "use1-az1",
    ],
}


def _synth(
    tmp_path: Path,
    *,
    context: dict[str, object],
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    out_dir = tmp_path / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    **_BASE_CONTEXT,
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
    return json.loads((out_dir / "AxonLLMManagedNetworkStack.template.json").read_text(encoding="utf-8"))


def _synth_failure(
    tmp_path: Path,
    *,
    context: dict[str, object],
) -> str:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    out_dir = tmp_path / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    **_BASE_CONTEXT,
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
    return completed.stdout


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


@pytest.fixture(scope="module")
def endpoints_only_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    return _synth(
        tmp_path_factory.mktemp("managed-endpoints-only"),
        context={"managed_network_egress_mode": "endpoints-only"},
    )


@pytest.fixture(scope="module")
def managed_nat_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    return _synth(
        tmp_path_factory.mktemp("managed-nat"),
        context={
            "managed_network_egress_mode": "managed-nat",
            "managed_network_nat_gateway_count": 2,
            "managed_network_cost_acknowledgement": True,
        },
    )


def test_endpoints_only_has_no_public_or_nat_resources(
    endpoints_only_template: dict,
) -> None:
    forbidden = {
        "AWS::EC2::EIP",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::NatGateway",
        "AWS::EC2::Route",
        "AWS::EC2::VPCGatewayAttachment",
    }
    resource_types = {resource["Type"] for resource in endpoints_only_template["Resources"].values()}
    assert forbidden.isdisjoint(resource_types)

    subnets = _resources(
        endpoints_only_template,
        "AWS::EC2::Subnet",
    )
    assert len(subnets) == 2
    assert all(subnet["Properties"]["MapPublicIpOnLaunch"] is False for subnet in subnets)
    assert "ApprovedHttpsPrefixListId" not in (endpoints_only_template["Parameters"])
    assert endpoints_only_template["Outputs"]["EgressMode"]["Value"] == ("endpoints-only")
    assert endpoints_only_template["Outputs"]["NatGatewayCount"]["Value"] == "0"


def test_endpoints_only_creates_required_private_service_endpoints(
    endpoints_only_template: dict,
) -> None:
    endpoints = _resources(
        endpoints_only_template,
        "AWS::EC2::VPCEndpoint",
    )
    assert len(endpoints) == 11
    service_names = {json.dumps(endpoint["Properties"]["ServiceName"]) for endpoint in endpoints}
    for suffix in {
        ".bedrock-runtime",
        ".cognito-idp",
        ".dynamodb",
        ".ecr.api",
        ".ecr.dkr",
        ".kms",
        ".logs",
        ".s3",
        ".secretsmanager",
        ".sns",
        ".sqs",
    }:
        assert any(suffix in service_name for service_name in service_names)

    security_group_egress = _resources(
        endpoints_only_template,
        "AWS::EC2::SecurityGroupEgress",
    )
    serialized = json.dumps(security_group_egress, sort_keys=True)
    assert "0.0.0.0/0" not in serialized
    assert "::/0" not in serialized


def test_endpoint_policies_are_resource_scoped(
    endpoints_only_template: dict,
) -> None:
    serialized = json.dumps(
        _resources(
            endpoints_only_template,
            "AWS::EC2::VPCEndpoint",
        ),
        sort_keys=True,
    )
    for parameter in {
        "ApplicationStateDataKeyArn",
        "ApplicationStateProviderSecretArn",
        "ApplicationStateRoutingConfigSigningKeyArn",
        "ApplicationStateSecurityEventLogGroupArn",
        "ApplicationStateSecurityEventOutboxQueueArn",
        "ApplicationStateSecurityEventTopicArn",
        "BedrockInvokeResourceArns",
        "SelectedStateTableName",
        "VerifiedImageUri",
    }:
        assert parameter in serialized


def test_managed_nat_requires_and_uses_exact_acknowledged_count(
    managed_nat_template: dict,
) -> None:
    assert len(_resources(managed_nat_template, "AWS::EC2::NatGateway")) == 2
    assert len(_resources(managed_nat_template, "AWS::EC2::EIP")) == 2
    assert len(_resources(managed_nat_template, "AWS::EC2::InternetGateway")) == 1
    assert len(_resources(managed_nat_template, "AWS::EC2::Subnet")) == 4
    assert "ApprovedHttpsPrefixListId" in managed_nat_template["Parameters"]
    assert managed_nat_template["Outputs"]["EgressMode"]["Value"] == ("managed-nat")
    assert managed_nat_template["Outputs"]["NatGatewayCount"]["Value"] == ("2")
    serialized = json.dumps(
        _resources(
            managed_nat_template,
            "AWS::EC2::SecurityGroupEgress",
        ),
        sort_keys=True,
    )
    assert "ApprovedHttpsPrefixListId" in serialized


def test_network_descriptor_outputs_are_complete(
    endpoints_only_template: dict,
) -> None:
    assert {
        "AvailabilityZoneIds",
        "AvailabilityZones",
        "DeploymentNamespace",
        "EgressMode",
        "ManagedNetworkStackName",
        "NatGatewayCount",
        "PrivateSubnetIds",
        "RuntimeSecurityGroupIds",
        "VpcCidr",
        "VpcId",
    }.issubset(endpoints_only_template["Outputs"])


def test_managed_nat_fails_without_cost_acknowledgement(
    tmp_path: Path,
) -> None:
    output = _synth_failure(
        tmp_path,
        context={
            "managed_network_egress_mode": "managed-nat",
            "managed_network_nat_gateway_count": 2,
        },
    )
    assert "managed_network_cost_acknowledgement=true" in output


def test_unsupported_agentcore_az_id_fails_before_template_creation(
    tmp_path: Path,
) -> None:
    output = _synth_failure(
        tmp_path,
        context={
            "managed_network_egress_mode": "endpoints-only",
            "managed_network_availability_zone_ids": [
                "use1-az4",
                "use1-az6",
            ],
        },
    )
    assert "unsupported AgentCore Availability Zone IDs: use1-az6" in (output)
