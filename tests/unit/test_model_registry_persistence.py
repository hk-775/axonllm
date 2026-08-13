"""Durability, CAS, and fleet convergence for model administration."""

from __future__ import annotations

import asyncio
import copy
import time

import pytest
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
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_config_signing import (
    RoutingConfigRollbackError,
    RoutingConfigSignatureError,
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
KEY_ARN = (
    "arn:aws:kms:us-west-2:123456789012:"
    "key/11111111-2222-3333-4444-555555555555"
)


class _Authenticator:
    def __init__(self, key_arn: str = KEY_ARN) -> None:
        self.key_arn = key_arn
        self.sign_count = 0
        self.verify_count = 0
        self.available = True

    @staticmethod
    def _signature(snapshot: RoutingConfigSnapshot) -> bytes:
        return b"test:" + snapshot.signing_digest

    async def sign(
        self,
        snapshot: RoutingConfigSnapshot,
    ) -> RoutingConfigSnapshot:
        self.sign_count += 1
        if not self.available:
            raise RoutingConfigSignatureError(
                "routing configuration signing is unavailable"
            )
        return snapshot.with_signature(
            signing_key_arn=self.key_arn,
            signature=self._signature(snapshot),
        )

    async def verify(self, snapshot: RoutingConfigSnapshot) -> None:
        self.verify_count += 1
        if not self.available:
            raise RoutingConfigSignatureError(
                "routing configuration verification is unavailable"
            )
        if (
            snapshot.signing_key_arn != self.key_arn
            or snapshot.signature != self._signature(snapshot)
        ):
            raise RoutingConfigSignatureError(
                "routing configuration signature is invalid"
            )


class _Persistence(DynamoPersistence):
    def __init__(
        self,
        client: _CasDynamoClient,
        *,
        mode: str = "disabled",
        authenticator: _Authenticator | None = None,
    ) -> None:
        super().__init__(
            table_name="model-registry-test",
            region="us-west-2",
            routing_config_signing_mode=mode,
            routing_config_signing_key_arn=(
                KEY_ARN if mode != "disabled" else ""
            ),
            routing_config_authenticator=authenticator,
        )
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
    first_snapshot = await first.save_model_registry(
        BASE_CONFIG,
        expected_revision=0,
    )
    assert first_snapshot.revision == 1
    loaded = await first.load_model_registry_snapshot()
    assert loaded is not None
    assert loaded.config == BASE_CONFIG
    assert loaded.revision == 1

    winner = await first.save_model_registry(
        _candidate("winner", "winner-id"),
        expected_revision=1,
    )
    assert winner.revision == 2
    try:
        await second.save_model_registry(
            _candidate("stale", "stale-id"),
            expected_revision=1,
        )
    except PersistenceConflictError:
        pass
    else:
        raise AssertionError("a stale registry write was accepted")

    loaded = await first.load_model_registry_snapshot()
    assert loaded is not None
    assert loaded.config == _candidate("winner", "winner-id")
    assert loaded.revision == 2


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

        async def load_model_registry_snapshot(self, **_kwargs):
            return self.snapshot

    fallback = _load_runtime_model_registry(
        str(path),
        _Snapshots(None),
    )
    durable = _load_runtime_model_registry(
        str(path),
        _Snapshots(
            RoutingConfigSnapshot.from_config(
                _candidate("durable", "durable-id"),
                revision=7,
            )
        ),
    )

    assert fallback.revision == 0
    assert set(fallback.models) == {"default-model"}
    assert durable.revision == 7
    assert set(durable.models) == {"durable"}


def test_signing_control_plane_initializes_the_first_durable_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "models.yaml"
    defaults = ModelRegistry.from_config(BASE_CONFIG)
    path.write_text(defaults.pretty_print(), encoding="utf-8")
    persistence = _Persistence(
        _CasDynamoClient(),
        mode="sign-verify",
        authenticator=_Authenticator(),
    )

    registry = _load_runtime_model_registry(
        str(path),
        persistence,
    )

    assert registry.revision == 1
    assert persistence.authenticated_routing_snapshot is not None
    assert persistence.authenticated_routing_snapshot.is_signed is True


def test_verification_only_runtime_requires_an_initialized_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "models.yaml"
    defaults = ModelRegistry.from_config(BASE_CONFIG)
    path.write_text(defaults.pretty_print(), encoding="utf-8")
    persistence = _Persistence(
        _CasDynamoClient(),
        mode="verify",
        authenticator=_Authenticator(),
    )

    with pytest.raises(RuntimeError, match="not initialized"):
        _load_runtime_model_registry(str(path), persistence)


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

        async def load_model_registry_snapshot(self, **_kwargs):
            raise RuntimeError("invalid routing snapshot")

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
    assert sync.routing_config_status["status"] == "degraded"


async def test_signed_snapshot_is_verified_once_and_cached() -> None:
    client = _CasDynamoClient()
    signer = _Authenticator()
    writer = _Persistence(
        client,
        mode="sign-verify",
        authenticator=signer,
    )
    signed = await writer.save_model_registry(
        BASE_CONFIG,
        expected_revision=0,
    )
    assert signed.is_signed is True
    assert client.rows[("MODEL_REGISTRY", "CONFIG")][
        "schema_version"
    ] == 2

    verifier = _Authenticator()
    reader = _Persistence(
        client,
        mode="verify",
        authenticator=verifier,
    )
    first = await reader.load_model_registry_snapshot()
    second = await reader.load_model_registry_snapshot(
        after_revision=1
    )

    assert first == signed
    assert second == signed
    assert verifier.verify_count == 1


async def test_unsigned_writer_cannot_downgrade_a_signed_row() -> None:
    client = _CasDynamoClient()
    writer = _Persistence(
        client,
        mode="sign-verify",
        authenticator=_Authenticator(),
    )
    await writer.save_model_registry(
        BASE_CONFIG,
        expected_revision=0,
    )

    unsigned = _Persistence(client)
    with pytest.raises(PersistenceConflictError):
        await unsigned.save_model_registry(
            _candidate("downgrade", "downgrade-id"),
            expected_revision=1,
        )


async def test_tampered_or_wrong_key_snapshot_is_rejected() -> None:
    client = _CasDynamoClient()
    writer = _Persistence(
        client,
        mode="sign-verify",
        authenticator=_Authenticator(),
    )
    await writer.save_model_registry(
        BASE_CONFIG,
        expected_revision=0,
    )
    row = client.rows[("MODEL_REGISTRY", "CONFIG")]
    tampered = RoutingConfigSnapshot.from_config(
        _candidate("tampered", "tampered-id"),
        revision=1,
    )
    row["document"] = tampered.document
    row["document_sha256"] = tampered.sha256

    reader = _Persistence(
        client,
        mode="verify",
        authenticator=_Authenticator(),
    )
    with pytest.raises(RoutingConfigSignatureError, match="invalid"):
        await reader.load_model_registry_snapshot()

    row["signing_key_arn"] = (
        "arn:aws:kms:us-west-2:123456789012:"
        "key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(RoutingConfigSignatureError, match="invalid"):
        await reader.load_model_registry_snapshot()


async def test_legacy_snapshot_is_migrated_only_by_a_signing_writer() -> None:
    client = _CasDynamoClient()
    client.rows[("MODEL_REGISTRY", "CONFIG")] = (
        DynamoPersistence.serialize_model_registry(
            BASE_CONFIG,
            revision=4,
        )
    )
    verifier = _Persistence(
        client,
        mode="verify",
        authenticator=_Authenticator(),
    )
    with pytest.raises(RoutingConfigSignatureError, match="unsigned"):
        await verifier.load_model_registry_snapshot()

    signer = _Persistence(
        client,
        mode="sign-verify",
        authenticator=_Authenticator(),
    )
    migrated = await signer.load_model_registry_snapshot()

    assert migrated is not None
    assert migrated.revision == 4
    assert migrated.is_signed is True
    assert client.rows[("MODEL_REGISTRY", "CONFIG")][
        "schema_version"
    ] == 2


async def test_authenticated_router_rejects_revision_rollback() -> None:
    client = _CasDynamoClient()
    writer = _Persistence(
        client,
        mode="sign-verify",
        authenticator=_Authenticator(),
    )
    await writer.save_model_registry(
        BASE_CONFIG,
        expected_revision=0,
    )
    revision_one = copy.deepcopy(
        client.rows[("MODEL_REGISTRY", "CONFIG")]
    )
    await writer.save_model_registry(
        _candidate("newer", "newer-id"),
        expected_revision=1,
    )
    reader = _Persistence(
        client,
        mode="verify",
        authenticator=_Authenticator(),
    )
    current = await reader.load_model_registry_snapshot()
    assert current is not None
    assert current.revision == 2

    alternate = await _Authenticator().sign(
        RoutingConfigSnapshot.from_config(
            _candidate("rewritten", "rewritten-id"),
            revision=2,
        )
    )
    client.rows[("MODEL_REGISTRY", "CONFIG")] = (
        DynamoPersistence.serialize_signed_model_registry(alternate)
    )
    with pytest.raises(
        RoutingConfigRollbackError,
        match="rewritten",
    ):
        await reader.load_model_registry_snapshot(
            after_revision=2
        )

    client.rows[("MODEL_REGISTRY", "CONFIG")] = revision_one
    with pytest.raises(RoutingConfigRollbackError):
        await reader.load_model_registry_snapshot(
            after_revision=2
        )


async def test_signature_outage_retains_lkg_and_reports_degraded() -> None:
    class _Snapshots:
        enabled = True

        def __init__(self) -> None:
            self.fail = True

        async def load_model_registry_snapshot(self, **_kwargs):
            if self.fail:
                raise RoutingConfigSignatureError(
                    "verification unavailable"
                )
            return RoutingConfigSnapshot.from_config(
                _candidate("recovered", "recovered-id"),
                revision=1,
            )

    persistence = _Snapshots()
    registry = ModelRegistry.from_config(BASE_CONFIG)
    sync = ConfigSyncService(
        projects={},
        user_configs={},
        cost_tracker=CostTracker(pricing_config={}),
        persistence=persistence,
        model_registry=registry,
    )
    sync._known_version = 0
    sync._last_version_check = time.monotonic()
    active = sync.active_routing_snapshot

    assert await sync.refresh_if_stale() is False
    assert sync.active_routing_snapshot == active
    assert sync.routing_config_status["error"] == (
        "signature_verification_failed"
    )

    persistence.fail = False
    assert await sync.refresh_if_stale() is True
    assert registry.revision == 1
    assert sync.routing_config_status["status"] == "synchronized"
