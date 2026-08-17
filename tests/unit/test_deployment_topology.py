"""Deployment topology contracts shared by standalone and Ostiari modes."""

from __future__ import annotations

import pytest

from src.gateway.bootstrap import build_starlette_app
from src.gateway.config import AppConfig
from src.gateway.config_loader import load_app_config
from src.gateway.deployment.topology import (
    DEPLOYMENT_PROFILES,
    OSTIARI_AGENTCORE,
    OSTIARI_EMBEDDED,
    STANDALONE,
    STANDALONE_AGENTCORE,
    DeploymentTopology,
)


@pytest.mark.parametrize(
    ("profile", "experience", "execution"),
    [
        (STANDALONE, "axonllm", "container"),
        (STANDALONE_AGENTCORE, "axonllm", "agentcore"),
        (OSTIARI_EMBEDDED, "ostiari", "container"),
        (OSTIARI_AGENTCORE, "ostiari", "agentcore"),
    ],
)
def test_profile_aliases_round_trip(
    profile: str,
    experience: str,
    execution: str,
) -> None:
    topology = DeploymentTopology.from_profile(profile)

    assert topology.experience == experience
    assert topology.execution == execution
    assert topology.profile == profile
    assert DeploymentTopology.from_mapping(topology.to_dict()) == topology


def test_profile_matrix_is_complete_and_rejects_unknown_values() -> None:
    assert DEPLOYMENT_PROFILES == (
        "standalone",
        "standalone-agentcore",
        "ostiari-embedded",
        "ostiari-agentcore",
    )
    with pytest.raises(ValueError, match="deployment profile"):
        DeploymentTopology.from_profile("standalone-ecs")
    with pytest.raises(ValueError, match="supported deployment profile"):
        AppConfig(
            experience_owner="ostiari",
            execution_target="lambda",
        )


def test_runtime_environment_loads_topology_without_reusing_security_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("AXON_EXPERIENCE_OWNER", "ostiari")
    monkeypatch.setenv("AXON_EXECUTION_TARGET", "agentcore")
    monkeypatch.setenv("AXON_AUTH_MODE", "ENFORCE")
    monkeypatch.setenv("AXON_REQUIRE_CANONICAL_IDENTITY", "true")
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "true")

    config = load_app_config()

    assert config.deployment_profile == "production"
    assert config.topology_profile == OSTIARI_AGENTCORE


def test_ostiari_owned_runtime_cannot_expose_axonllm_control_plane() -> None:
    config = AppConfig(
        experience_owner="ostiari",
        execution_target="container",
    )

    with pytest.raises(
        RuntimeError,
        match=r"use build_gateway_agent\(\)",
    ):
        build_starlette_app(config)
