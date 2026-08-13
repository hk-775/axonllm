"""Control-plane routing snapshot contract."""

from __future__ import annotations

import pytest

from src.gateway.model_registry import ModelRegistry
from src.gateway.routing_config import (
    ROUTING_CONFIG_SCHEMA,
    RoutingConfigSnapshot,
)
from src.gateway.routing_config_contract import (
    ROUTING_CONFIG_SIGNATURE_SCHEMA,
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


def test_signed_snapshot_binds_revision_digest_and_exact_key() -> None:
    key_arn = (
        "arn:aws:kms:us-west-2:123456789012:"
        "key/11111111-2222-3333-4444-555555555555"
    )
    snapshot = RoutingConfigSnapshot.from_config(
        CONFIG,
        revision=9,
    ).with_signature(
        signing_key_arn=key_arn,
        signature=b"test-signature",
    )

    value = snapshot.as_dict()

    assert snapshot.is_signed is True
    assert snapshot.signing_digest == snapshot.signing_digest
    assert b'"revision":9' in snapshot.signing_payload
    assert snapshot.sha256.encode("ascii") in snapshot.signing_payload
    assert value["signature"] == {
        "schema": ROUTING_CONFIG_SIGNATURE_SCHEMA,
        "key_arn": key_arn,
        "algorithm": "ECDSA_SHA_256",
        "value": "dGVzdC1zaWduYXR1cmU=",
    }


def test_persisted_snapshot_must_be_canonical_and_complete() -> None:
    snapshot = RoutingConfigSnapshot.from_config(CONFIG, revision=2)

    with pytest.raises(ValueError, match="not canonical"):
        RoutingConfigSnapshot.from_document(
            '{"models": []}',
            revision=2,
            sha256=snapshot.sha256,
        )
    with pytest.raises(ValueError, match="incomplete"):
        RoutingConfigSnapshot(
            revision=snapshot.revision,
            document=snapshot.document,
            sha256=snapshot.sha256,
            signing_key_arn=(
                "arn:aws:kms:us-west-2:123456789012:"
                "key/11111111-2222-3333-4444-555555555555"
            ),
        )
