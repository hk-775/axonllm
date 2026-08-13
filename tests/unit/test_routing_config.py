"""Control-plane routing snapshot contract."""

from __future__ import annotations

import pytest

from src.gateway.model_registry import ModelRegistry
from src.gateway.routing_config import (
    ROUTING_CONFIG_SCHEMA,
    RoutingConfigSnapshot,
)


CONFIG = {
    "models": [
        {
            "name": "balanced",
            "description": "Multi-provider model",
            "routing_strategy": "weighted",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "gpt-test",
                    "weight": 0.5,
                },
                {
                    "provider": "anthropic",
                    "model_id": "claude-test",
                    "weight": 0.5,
                },
            ],
        }
    ]
}


def test_snapshot_is_deterministic_and_credential_free() -> None:
    left = RoutingConfigSnapshot.from_config(CONFIG, revision=7)
    right = RoutingConfigSnapshot.from_config(
        {"models": list(CONFIG["models"])},
        revision=7,
    )

    assert left.sha256 == right.sha256
    assert left.as_dict()["schema"] == ROUTING_CONFIG_SCHEMA
    assert left.as_dict()["revision"] == 7
    assert "credential" not in left.document
    assert "api_key" not in left.document


def test_snapshot_returns_detached_config_and_applies_atomically() -> None:
    snapshot = RoutingConfigSnapshot.from_config(CONFIG, revision=3)
    detached = snapshot.config
    detached["models"].clear()

    registry = ModelRegistry.from_config(CONFIG)
    snapshot.apply(registry)

    assert registry.revision == 3
    assert set(registry.models) == {"balanced"}
    assert snapshot.config["models"]


def test_snapshot_rejects_partial_or_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="invalid model registry snapshot"):
        RoutingConfigSnapshot.from_config(
            {
                "models": [
                    {
                        "name": "broken",
                        "description": "Missing providers",
                    }
                ]
            },
            revision=1,
        )
