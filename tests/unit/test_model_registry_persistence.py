"""Durability, CAS, and fleet convergence for model administration."""

from __future__ import annotations

import asyncio
import time

from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.bootstrap import _load_runtime_model_registry
from src.gateway.config_sync import ConfigSyncService
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.persistence import (
    DynamoPersistence,
    PersistenceConflictError,
)
from src.gateway.router import Router
from tests.unit.test_persistence_cas_foundations import (
    _CasDynamoClient,
    _CasTable,
)


BASE_CONFIG = {
    "models": [
        {
            "name": "default-model",
            "description": "Checked-in default",
            "routing_strategy": "round-robin",
            "providers": [
                {
                    "provider": "openai",
                    "model_id": "default-provider-id",
                    "weight": 1.0,
                    "fallback_order": 0,
                }
            ],
        }
    ]
}


class _Persistence(DynamoPersistence):
    def __init__(self, client: _CasDynamoClient) -> None:
        super().__init__(table_name="model-registry-test")
        self._enabled = True
        self._table = _CasTable(client)


def _candidate(name: str, model_id: str) -> dict:
    return {
        "models": [
            {
                "name": name,
                "description": name,
                "routing_strategy": "round-robin",
                "providers": [
                    {
                        "provider": "openai",
                        "model_id": model_id,
                        "weight": 1.0,
                        "fallback_order": 0,
                    }
                ],
            }
        ]
    }


def _admin_client(
    persistence: DynamoPersistence,
    registry: ModelRegistry,
) -> TestClient:
    api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=registry,
        persistence=persistence,
    )
    return TestClient(
        Starlette(routes=create_admin_routes(api)),
        raise_server_exceptions=False,
    )


async def test_model_registry_document_is_revisioned_and_cas_protected() -> None:
    client = _CasDynamoClient()
    first = _Persistence(client)
    second = _Persistence(client)

    assert await first.load_model_registry_snapshot() is None
    assert (
        await first.save_model_registry(
            BASE_CONFIG,
            expected_revision=0,
        )
        == 1
    )
    assert await first.load_model_registry_snapshot() == (BASE_CONFIG, 1)

    assert (
        await first.save_model_registry(
            _candidate("winner", "winner-id"),
            expected_revision=1,
        )
        == 2
    )
    try:
        await second.save_model_registry(
            _candidate("stale", "stale-id"),
            expected_revision=1,
        )
    except PersistenceConflictError:
        pass
    else:
        raise AssertionError("a stale registry write was accepted")

    assert await first.load_model_registry_snapshot() == (
        _candidate("winner", "winner-id"),
        2,
    )


async def test_fleet_refresh_atomically_rebuilds_live_router_mappings() -> None:
    client = _CasDynamoClient()
    persistence = _Persistence(client)
    registry = ModelRegistry.from_config(BASE_CONFIG)
    router = Router(
        registry,
        ProviderHealthTracker(),
    )
    sync = ConfigSyncService(
        projects={},
        user_configs={},
        cost_tracker=CostTracker(pricing_config={}),
        persistence=persistence,
        model_registry=registry,
    )

    await persistence.save_model_registry(
        _candidate("replacement", "replacement-id"),
        expected_revision=0,
    )

    assert router.get_fallback_chain("default-model")[0].model_id == ("default-provider-id")
    assert await sync.refresh_if_stale() is True
    assert registry.revision == 1
    assert "default-model" not in registry.models
    assert router.get_fallback_chain("replacement")[0].model_id == ("replacement-id")


def test_stale_admin_writer_gets_conflict_and_adopts_winner() -> None:
    dynamo = _CasDynamoClient()
    first_registry = ModelRegistry.from_config(BASE_CONFIG)
    second_registry = ModelRegistry.from_config(BASE_CONFIG)
    first = _admin_client(_Persistence(dynamo), first_registry)
    second = _admin_client(_Persistence(dynamo), second_registry)
    body = {
        "description": "added",
        "providers": [{"provider": "openai", "model_id": "added-id"}],
    }

    winner = first.post(
        "/admin/models",
        json={"name": "winner", **body},
    )
    stale = second.post(
        "/admin/models",
        json={"name": "stale", **body},
    )

    assert winner.status_code == 201
    assert winner.headers["etag"] == '"1"'
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == ("model_registry_write_conflict")
    assert second_registry.revision == 1
    assert "winner" in second_registry.models
    assert "stale" not in second_registry.models


def test_snapshot_checksum_prevents_partial_or_corrupt_adoption() -> None:
    client = _CasDynamoClient()
    persistence = _Persistence(client)
    row = DynamoPersistence.serialize_model_registry(
        BASE_CONFIG,
        revision=1,
    )
    row["document"] = '{"models":[]}'
    client.rows[("MODEL_REGISTRY", "CONFIG")] = row

    try:
        asyncio.run(persistence.load_model_registry_snapshot())
    except RuntimeError as exc:
        assert "checksum" in str(exc)
    else:
        raise AssertionError("a corrupt registry document was accepted")


def test_file_defaults_are_fallback_until_a_durable_snapshot_exists(
    tmp_path,
) -> None:
    path = tmp_path / "models.yaml"
    defaults = ModelRegistry.from_config(BASE_CONFIG)
    path.write_text(defaults.pretty_print(), encoding="utf-8")

    class _Snapshots:
        enabled = True

        def __init__(self, snapshot):
            self.snapshot = snapshot

        async def load_model_registry_snapshot(self):
            return self.snapshot

    fallback = _load_runtime_model_registry(
        str(path),
        _Snapshots(None),
    )
    durable = _load_runtime_model_registry(
        str(path),
        _Snapshots((_candidate("durable", "durable-id"), 7)),
    )

    assert fallback.revision == 0
    assert set(fallback.models) == {"default-model"}
    assert durable.revision == 7
    assert set(durable.models) == {"durable"}


def test_failed_durable_write_does_not_change_live_routes() -> None:
    class _Unavailable:
        enabled = True

        async def save_model_registry(self, *_args, **_kwargs):
            raise RuntimeError("DynamoDB unavailable")

    registry = ModelRegistry.from_config(BASE_CONFIG)
    client = _admin_client(_Unavailable(), registry)

    response = client.post(
        "/admin/models",
        json={
            "name": "not-committed",
            "description": "not committed",
            "providers": [{"provider": "openai", "model_id": "not-committed"}],
        },
    )

    assert response.status_code == 503
    assert registry.revision == 0
    assert set(registry.models) == {"default-model"}


async def test_invalid_new_snapshot_is_never_partially_adopted() -> None:
    class _InvalidSnapshot:
        enabled = True

        async def load_model_registry_snapshot(self):
            return (
                {
                    "models": [
                        {
                            "name": "invalid",
                            "description": "missing providers",
                        }
                    ]
                },
                1,
            )

    registry = ModelRegistry.from_config(BASE_CONFIG)
    sync = ConfigSyncService(
        projects={},
        user_configs={},
        cost_tracker=CostTracker(pricing_config={}),
        persistence=_InvalidSnapshot(),
        model_registry=registry,
    )
    # Isolate the model poll; project/user convergence has its own focused
    # suite and this fake intentionally implements no scan API.
    sync._known_version = 0
    sync._last_version_check = time.monotonic()
    active = sync.active_routing_snapshot

    assert await sync.refresh_if_stale() is False
    assert registry.revision == 0
    assert set(registry.models) == {"default-model"}
    assert sync.active_routing_snapshot == active
    assert sync._last_model_check == float("-inf")
