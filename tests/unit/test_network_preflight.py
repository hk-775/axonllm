"""Read-only AWS metadata contracts for deployment network preflight."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from src.gateway.deployment.config_contract import load_deployment_config
from src.gateway.deployment.network_preflight import (
    NetworkPreflightError,
    preflight_deployment_network,
    runtime_network_context,
)


_REPO = Path(__file__).resolve().parents[2]
_ACCOUNT_ID = "123456789012"
_VPC_ID = "vpc-0123456789abcdef0"
_SUBNET_IDS = (
    "subnet-0123456789abcdef0",
    "subnet-0fedcba9876543210",
)
_SECURITY_GROUP_ID = "sg-0123456789abcdef0"
_ROUTE_TABLE_IDS = ("rtb-0123456789abcdef0", "rtb-0fedcba9876543210")


class _Paginator:
    def __init__(self, client: "_Ec2Client", operation: str) -> None:
        self._client = client
        self._operation = operation

    def paginate(self, **kwargs: Any) -> list[dict]:
        self._client.calls.append((self._operation, kwargs))
        if self._operation == "describe_route_tables":
            return [{"RouteTables": copy.deepcopy(self._client.route_tables)}]
        if self._operation == "describe_vpc_endpoints":
            return [{"VpcEndpoints": copy.deepcopy(self._client.endpoints)}]
        raise AssertionError(f"unexpected paginator {self._operation}")


class _Ec2Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.dns_support = True
        self.dns_hostnames = True
        self.vpc = {
            "VpcId": _VPC_ID,
            "State": "available",
            "OwnerId": _ACCOUNT_ID,
            "CidrBlock": "10.20.0.0/16",
        }
        self.subnets = [
            {
                "SubnetId": _SUBNET_IDS[0],
                "VpcId": _VPC_ID,
                "OwnerId": _ACCOUNT_ID,
                "State": "available",
                "MapPublicIpOnLaunch": False,
                "AvailabilityZone": "us-east-1a",
                "AvailabilityZoneId": "use1-az4",
            },
            {
                "SubnetId": _SUBNET_IDS[1],
                "VpcId": _VPC_ID,
                "OwnerId": _ACCOUNT_ID,
                "State": "available",
                "MapPublicIpOnLaunch": False,
                "AvailabilityZone": "us-east-1c",
                "AvailabilityZoneId": "use1-az1",
            },
        ]
        self.security_groups = [
            {
                "GroupId": _SECURITY_GROUP_ID,
                "VpcId": _VPC_ID,
                "OwnerId": _ACCOUNT_ID,
            }
        ]
        self.route_tables = [
            {
                "RouteTableId": _ROUTE_TABLE_IDS[0],
                "Associations": [{"SubnetId": _SUBNET_IDS[0]}],
                "Routes": [
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "NatGatewayId": "nat-0123456789abcdef0",
                        "State": "active",
                    }
                ],
            },
            {
                "RouteTableId": _ROUTE_TABLE_IDS[1],
                "Associations": [{"SubnetId": _SUBNET_IDS[1]}],
                "Routes": [
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "TransitGatewayId": "tgw-0123456789abcdef0",
                        "State": "active",
                    }
                ],
            },
        ]
        self.endpoints: list[dict[str, Any]] = []
        self.availability_zones = [
            {
                "ZoneId": "use1-az4",
                "ZoneName": "us-east-1a",
                "State": "available",
                "ZoneType": "availability-zone",
                "OptInStatus": "opt-in-not-required",
            },
            {
                "ZoneId": "use1-az1",
                "ZoneName": "us-east-1c",
                "State": "available",
                "ZoneType": "availability-zone",
                "OptInStatus": "opt-in-not-required",
            },
        ]

    def get_paginator(self, operation: str) -> _Paginator:
        return _Paginator(self, operation)

    def describe_vpcs(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_vpcs", kwargs))
        return {"Vpcs": [copy.deepcopy(self.vpc)]}

    def describe_vpc_attribute(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_vpc_attribute", kwargs))
        attribute = kwargs["Attribute"]
        value = self.dns_support if attribute == "enableDnsSupport" else self.dns_hostnames
        key = attribute[0].upper() + attribute[1:]
        return {key: {"Value": value}}

    def describe_subnets(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_subnets", kwargs))
        return {"Subnets": copy.deepcopy(self.subnets)}

    def describe_security_groups(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_security_groups", kwargs))
        return {"SecurityGroups": copy.deepcopy(self.security_groups)}

    def describe_route_tables(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_route_tables", kwargs))
        wanted = set(kwargs["RouteTableIds"])
        return {
            "RouteTables": [
                copy.deepcopy(route_table) for route_table in self.route_tables if route_table["RouteTableId"] in wanted
            ]
        }

    def describe_managed_prefix_lists(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_managed_prefix_lists", kwargs))
        return {
            "PrefixLists": [
                {
                    "PrefixListId": kwargs["PrefixListIds"][0],
                    "OwnerId": _ACCOUNT_ID,
                    "AddressFamily": "IPv4",
                    "State": "create-complete",
                }
            ]
        }

    def describe_availability_zones(self, **kwargs: Any) -> dict:
        self.calls.append(("describe_availability_zones", kwargs))
        return {"AvailabilityZones": copy.deepcopy(self.availability_zones)}


class _Session:
    def __init__(self, ec2_client: _Ec2Client) -> None:
        self.ec2_client = ec2_client

    def client(self, service: str, *, region_name: str) -> _Ec2Client:
        assert service == "ec2"
        assert region_name == "us-east-1"
        return self.ec2_client


def _config(name: str) -> dict:
    return load_deployment_config(_REPO / "config" / "deployment" / name)


def _endpoints_only_config() -> dict:
    config = _config("agentcore-existing-vpc.yaml")
    config["network"]["egress"] = {"mode": "endpoints-only"}
    config["network"]["security_group_ids"] = [_SECURITY_GROUP_ID]
    config["runtime"]["providers"] = ["bedrock"]
    return config


def _install_required_endpoints(client: _Ec2Client) -> None:
    services = {
        "bedrock-runtime",
        "cognito-idp",
        "dynamodb",
        "ecr.api",
        "ecr.dkr",
        "kms",
        "logs",
        "s3",
        "secretsmanager",
        "sns",
        "sqs",
    }
    client.endpoints = []
    for service in sorted(services):
        endpoint = {
            "VpcEndpointId": f"vpce-{len(client.endpoints) + 1:017x}",
            "VpcId": _VPC_ID,
            "State": "available",
            "ServiceName": f"com.amazonaws.us-east-1.{service}",
        }
        if service in {"dynamodb", "s3"}:
            endpoint.update(
                {
                    "VpcEndpointType": "Gateway",
                    "RouteTableIds": list(_ROUTE_TABLE_IDS),
                }
            )
        else:
            endpoint.update(
                {
                    "VpcEndpointType": "Interface",
                    "PrivateDnsEnabled": True,
                    "Groups": [{"GroupId": _SECURITY_GROUP_ID}],
                }
            )
        client.endpoints.append(endpoint)


def test_existing_egress_preflight_is_read_only_and_exact() -> None:
    client = _Ec2Client()
    result = preflight_deployment_network(
        _Session(client),
        _config("agentcore-existing-vpc.yaml"),
        account_id=_ACCOUNT_ID,
    )

    assert result.mode == "existing"
    assert result.egress_mode == "existing-egress"
    assert result.managed_stack_context is None
    assert result.required_services == ()
    assert result.runtime_context == {
        "deployment_profile": "production",
        "runtime_network_mode": "existing",
        "runtime_network_egress_mode": "existing-egress",
        "runtime_network_vpc_id": _VPC_ID,
        "runtime_network_vpc_cidr": "10.20.0.0/16",
        "runtime_network_private_subnet_ids": list(_SUBNET_IDS),
        "runtime_network_availability_zones": [
            "us-east-1a",
            "us-east-1c",
        ],
        "runtime_network_security_group_ids": [],
    }
    assert all(operation.startswith("describe_") for operation, _ in client.calls)


def test_endpoints_only_validates_routes_and_every_required_endpoint() -> None:
    client = _Ec2Client()
    for route_table in client.route_tables:
        route_table["Routes"] = []
    _install_required_endpoints(client)

    result = preflight_deployment_network(
        _Session(client),
        _endpoints_only_config(),
        account_id=_ACCOUNT_ID,
    )

    assert result.egress_mode == "endpoints-only"
    assert result.required_services == (
        "bedrock-runtime",
        "cognito-idp",
        "dynamodb",
        "ecr.api",
        "ecr.dkr",
        "kms",
        "logs",
        "s3",
        "secretsmanager",
        "sns",
        "sqs",
    )


def test_endpoints_only_rejects_missing_private_endpoint() -> None:
    client = _Ec2Client()
    for route_table in client.route_tables:
        route_table["Routes"] = []
    _install_required_endpoints(client)
    client.endpoints = [endpoint for endpoint in client.endpoints if not endpoint["ServiceName"].endswith(".kms")]

    with pytest.raises(
        NetworkPreflightError,
        match="missing available endpoint com.amazonaws.us-east-1.kms",
    ):
        preflight_deployment_network(
            _Session(client),
            _endpoints_only_config(),
            account_id=_ACCOUNT_ID,
        )


def test_existing_egress_rejects_direct_internet_gateway() -> None:
    client = _Ec2Client()
    client.route_tables[0]["Routes"][0] = {
        "DestinationCidrBlock": "0.0.0.0/0",
        "GatewayId": "igw-0123456789abcdef0",
        "State": "active",
    }

    with pytest.raises(
        NetworkPreflightError,
        match="must not route directly to an internet gateway",
    ):
        preflight_deployment_network(
            _Session(client),
            _config("agentcore-existing-vpc.yaml"),
            account_id=_ACCOUNT_ID,
        )


def test_existing_network_rejects_disabled_dns() -> None:
    client = _Ec2Client()
    client.dns_hostnames = False

    with pytest.raises(
        NetworkPreflightError,
        match="enableDnsHostnames enabled",
    ):
        preflight_deployment_network(
            _Session(client),
            _config("agentcore-existing-vpc.yaml"),
            account_id=_ACCOUNT_ID,
        )


def test_managed_preflight_maps_zone_ids_without_creating_resources() -> None:
    client = _Ec2Client()
    result = preflight_deployment_network(
        _Session(client),
        _config("agentcore-managed-external.yaml"),
        account_id=_ACCOUNT_ID,
    )

    assert result.mode == "managed"
    assert result.runtime_context is None
    assert result.required_services == ()
    assert result.managed_stack_context == {
        "deployment_profile": "production",
        "managed_network_egress_mode": "managed-nat",
        "managed_network_vpc_cidr": "10.42.0.0/16",
        "managed_network_availability_zones": [
            "us-east-1a",
            "us-east-1c",
        ],
        "managed_network_availability_zone_ids": [
            "use1-az4",
            "use1-az1",
        ],
        "managed_network_nat_gateway_count": 2,
        "managed_network_cost_acknowledgement": True,
    }
    assert [operation for operation, _ in client.calls] == ["describe_availability_zones"]


def test_managed_runtime_context_requires_matching_stack_receipt() -> None:
    client = _Ec2Client()
    preflight = preflight_deployment_network(
        _Session(client),
        _config("agentcore-managed-external.yaml"),
        account_id=_ACCOUNT_ID,
    )
    outputs = {
        "AvailabilityZoneIds": "use1-az4,use1-az1",
        "AvailabilityZones": "us-east-1a,us-east-1c",
        "DeploymentNamespace": "production",
        "EgressMode": "managed-nat",
        "ManagedNetworkStackName": "AxonLLMManagedNetworkStack",
        "NatGatewayCount": "2",
        "PrivateSubnetIds": ",".join(_SUBNET_IDS),
        "RuntimeSecurityGroupIds": _SECURITY_GROUP_ID,
        "VpcCidr": "10.42.0.0/16",
        "VpcId": _VPC_ID,
    }

    assert runtime_network_context(
        preflight,
        managed_outputs=outputs,
        expected_managed_stack_name="AxonLLMManagedNetworkStack",
    ) == {
        "deployment_profile": "production",
        "runtime_network_mode": "managed",
        "runtime_network_egress_mode": "managed-nat",
        "runtime_network_vpc_id": _VPC_ID,
        "runtime_network_vpc_cidr": "10.42.0.0/16",
        "runtime_network_private_subnet_ids": list(_SUBNET_IDS),
        "runtime_network_availability_zones": [
            "us-east-1a",
            "us-east-1c",
        ],
        "runtime_network_security_group_ids": [_SECURITY_GROUP_ID],
    }

    with pytest.raises(
        NetworkPreflightError,
        match="NAT count does not match",
    ):
        runtime_network_context(
            preflight,
            managed_outputs={**outputs, "NatGatewayCount": "1"},
            expected_managed_stack_name=("AxonLLMManagedNetworkStack"),
        )

    with pytest.raises(
        NetworkPreflightError,
        match="VPC CIDR does not match",
    ):
        runtime_network_context(
            preflight,
            managed_outputs={
                **outputs,
                "VpcCidr": "10.43.0.0/16",
            },
            expected_managed_stack_name="AxonLLMManagedNetworkStack",
        )

    with pytest.raises(
        NetworkPreflightError,
        match="Availability Zones do not match",
    ):
        runtime_network_context(
            preflight,
            managed_outputs={
                **outputs,
                "AvailabilityZones": "us-east-1c,us-east-1a",
            },
            expected_managed_stack_name="AxonLLMManagedNetworkStack",
        )


def test_public_preflight_requires_isolated_namespace() -> None:
    config = _config("agentcore-public-development.yaml")
    client = _Ec2Client()

    with pytest.raises(
        NetworkPreflightError,
        match="requires a non-empty deployment namespace",
    ):
        preflight_deployment_network(
            _Session(client),
            config,
            account_id=_ACCOUNT_ID,
        )

    result = preflight_deployment_network(
        _Session(client),
        config,
        account_id=_ACCOUNT_ID,
        deployment_namespace="dev",
    )
    assert result.runtime_context == {
        "deployment_namespace": "dev",
        "deployment_profile": "development",
        "runtime_network_mode": "public",
    }
    assert client.calls == []
