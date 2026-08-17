from __future__ import annotations

import copy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.gateway.deployment.config_contract import (
    DeploymentConfigError,
    deployment_config_schema,
    load_deployment_config,
    validate_deployment_config,
)

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = sorted((_REPO / "config" / "deployment").glob("*.yaml"))


def _existing_config() -> dict:
    return load_deployment_config(_REPO / "config" / "deployment" / "agentcore-existing-vpc.yaml")


def _managed_external_config() -> dict:
    return load_deployment_config(_REPO / "config" / "deployment" / "agentcore-managed-external.yaml")


def test_packaged_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(deployment_config_schema())


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.stem)
def test_documented_deployment_examples_validate(path: Path) -> None:
    config = load_deployment_config(path)

    assert config["schema_version"] == 1
    assert config["target"] == "agentcore"


def test_validator_returns_a_copy() -> None:
    config = _existing_config()

    validated = validate_deployment_config(config)
    validated["network"]["private_subnet_ids"].append("subnet-aaaaaaaaaaaaaaaaa")

    assert len(config["network"]["private_subnet_ids"]) == 2


def test_unknown_top_level_field_fails_closed() -> None:
    config = _existing_config()
    config["unexpected"] = True

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_unknown_nested_field_fails_closed() -> None:
    config = _existing_config()
    config["network"]["modify_route_tables"] = True

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_plaintext_provider_credential_field_is_not_in_contract() -> None:
    config = _existing_config()
    config["runtime"]["openai_api_key"] = "not-a-real-key"

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_production_rejects_public_runtime_networking() -> None:
    config = _existing_config()
    config["network"] = {"mode": "public"}

    with pytest.raises(
        DeploymentConfigError,
        match="public networking is development-only",
    ):
        validate_deployment_config(config)


def test_public_networking_is_valid_for_development() -> None:
    config = _existing_config()
    config["deployment_profile"] = "development"
    config["network"] = {"mode": "public"}

    assert validate_deployment_config(config)["network"] == {"mode": "public"}


def test_managed_nat_requires_explicit_cost_acknowledgement() -> None:
    config = _managed_external_config()
    del config["network"]["egress"]["cost_acknowledgement"]

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_managed_nat_rejects_false_cost_acknowledgement() -> None:
    config = _managed_external_config()
    config["network"]["egress"]["cost_acknowledgement"] = False

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


@pytest.mark.parametrize(
    "cidr",
    [
        "10.42.0.1/16",
        "999.42.0.0/16",
        "2001:db8::/64",
    ],
)
def test_managed_network_requires_canonical_ipv4_cidr(cidr: str) -> None:
    config = _managed_external_config()
    config["network"]["vpc_cidr"] = cidr

    with pytest.raises(
        DeploymentConfigError,
        match="must be a canonical IPv4 CIDR",
    ):
        validate_deployment_config(config)


def test_existing_network_requires_two_unique_private_subnets() -> None:
    config = _existing_config()
    config["network"]["private_subnet_ids"] = ["subnet-0123456789abcdef0"]

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_existing_network_allows_axon_owned_runtime_security_group() -> None:
    config = _existing_config()

    assert config["network"]["security_group_ids"] == []
    validate_deployment_config(config)


def test_endpoints_only_rejects_external_providers() -> None:
    config = _existing_config()
    config["network"]["egress"] = {"mode": "endpoints-only"}

    with pytest.raises(
        DeploymentConfigError,
        match="endpoints-only cannot reach external providers: anthropic",
    ):
        validate_deployment_config(config)


def test_endpoints_only_accepts_aws_providers() -> None:
    config = _existing_config()
    config["network"]["egress"] = {"mode": "endpoints-only"}
    config["runtime"]["providers"] = ["bedrock"]

    validate_deployment_config(config)


def test_endpoints_only_rejects_bedrock_mantle_without_private_path() -> None:
    config = _existing_config()
    config["network"]["egress"] = {"mode": "endpoints-only"}
    config["runtime"]["providers"] = ["bedrock-mantle"]

    with pytest.raises(
        DeploymentConfigError,
        match="endpoints-only cannot reach external providers: bedrock-mantle",
    ):
        validate_deployment_config(config)


def test_managed_endpoints_only_requires_complete_service_set() -> None:
    config = load_deployment_config(_REPO / "config" / "deployment" / "agentcore-managed-bedrock.yaml")
    config["network"]["egress"]["services"].remove("kms")

    with pytest.raises(
        DeploymentConfigError,
        match="missing required services: kms",
    ):
        validate_deployment_config(config)


def test_managed_network_rejects_unsupported_agentcore_az_id() -> None:
    config = _managed_external_config()
    config["network"]["availability_zone_ids"][1] = "use1-az6"

    with pytest.raises(
        DeploymentConfigError,
        match="unsupported AgentCore Availability Zone IDs: use1-az6",
    ):
        validate_deployment_config(config)


def test_custom_hostname_requires_certificate() -> None:
    config = _existing_config()
    config["control_plane"]["hostname"] = {
        "mode": "custom",
        "domain_name": "axon.example.com",
    }

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_existing_oidc_requires_https_issuer() -> None:
    config = _existing_config()
    config["identity"] = {
        "mode": "existing-oidc",
        "issuer": "http://identity.example.com",
        "audiences": ["axon-runtime"],
        "browser_client_id": "axon-browser",
    }

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    source = (_REPO / "config" / "deployment" / "agentcore-public-development.yaml").read_text(encoding="utf-8")
    path = tmp_path / "duplicate.yaml"
    path.write_text(
        source.replace(
            "region: us-east-1",
            "region: us-east-1\nregion: us-west-2",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentConfigError, match="duplicate key 'region'"):
        load_deployment_config(path)


def test_non_mapping_yaml_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- agentcore\n", encoding="utf-8")

    with pytest.raises(
        DeploymentConfigError,
        match="root must be a mapping",
    ):
        load_deployment_config(path)


def test_input_is_not_modified_when_validation_fails() -> None:
    config = _existing_config()
    config["network"]["private_subnet_ids"] = []
    original = copy.deepcopy(config)

    with pytest.raises(DeploymentConfigError):
        validate_deployment_config(config)

    assert config == original
