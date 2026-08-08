"""Tenant-boundary tests for the platform-global region admin routes."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.admin.region_routes import RegionAPI, create_region_routes
from src.gateway.config_sync import (
    ConfigSyncService,
    RegionTopologyUnavailable,
)
from src.gateway.cost_tracker import CostTracker
from src.gateway.models import RequestContext
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeStatus,
    apply_persisted_topology,
)
from src.gateway.multi_region.region_router import RegionRouter


def _api(persistence=None) -> tuple[RegionAPI, HubConfig]:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[SpokeConfig(region="us-east-1")],
    )
    return (
        RegionAPI(
            router=RegionRouter(config),
            monitor=SpokeHealthMonitor(config),
            persistence=persistence,
        ),
        config,
    )


def _client(
    api: RegionAPI,
    *,
    role: str,
    tenant_id: object = "tenant-a",
) -> TestClient:
    async def add_context(request, call_next):
        request.state.context = RequestContext(
            user_id="operator",
            project_id="",
            roles=[role],
            scopes=[],
            tenant_id=tenant_id,
        )
        return await call_next(request)

    app = Starlette(routes=create_region_routes(api))
    app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
    return TestClient(app)


def test_tenant_reader_cannot_inspect_global_topology() -> None:
    api, _ = _api()

    response = _client(api, role="tenant_auditor").get("/admin/regions")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "platform_scope_required"


def test_tenant_admin_cannot_mutate_global_region_id() -> None:
    api, config = _api()

    response = _client(api, role="tenant_admin").delete(
        "/admin/regions/spokes/us-east-1"
    )

    assert response.status_code == 403
    assert config.get_spoke("us-east-1") is not None


def test_platform_admin_can_read_and_update_global_topology() -> None:
    api, config = _api()
    client = _client(api, role="platform_admin")

    assert client.get("/admin/regions").status_code == 200
    response = client.put(
        "/admin/regions/config",
        json={"data_residency_strict": True},
    )

    assert response.status_code == 200
    assert config.data_residency_strict is True


def test_canonical_platform_role_without_tenant_fails_closed() -> None:
    api, _ = _api()

    response = _client(
        api,
        role="platform_admin",
        tenant_id=None,
    ).get("/admin/regions")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_tenant_scope"


def test_malformed_tenant_id_fails_closed() -> None:
    api, _ = _api()

    response = _client(
        api,
        role="platform_admin",
        tenant_id=123,
    ).get("/admin/regions")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_tenant_scope"


def test_direct_stub_without_state_keeps_legacy_scope() -> None:
    api, _ = _api()

    class RequestStub:
        pass

    response = asyncio.run(api.get_topology(RequestStub()))

    assert response.status_code == 200


class _FailingPersistence:
    enabled = True

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.attempted_config = None

    async def save_region_topology(
        self,
        config,
        expected_revision,
    ) -> int | None:
        self.attempted_config = config
        if self.failure == "exception":
            raise RuntimeError("dynamo is unavailable")
        return None


@pytest.mark.parametrize("failure", ["dropped", "exception"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param(
            "post",
            "/admin/regions/spokes",
            {"region": "eu-west-1"},
            id="add",
        ),
        pytest.param(
            "put",
            "/admin/regions/spokes/us-east-1",
            {"weight": 25, "role": "active"},
            id="update",
        ),
        pytest.param(
            "delete",
            "/admin/regions/spokes/us-east-1",
            None,
            id="delete",
        ),
        pytest.param(
            "put",
            "/admin/regions/config",
            {
                "hub_region": "eu-west-1",
                "data_residency_strict": True,
            },
            id="strategy",
        ),
    ],
)
def test_topology_mutations_fail_closed_without_live_state_changes(
    failure: str,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    persistence = _FailingPersistence(failure)
    api, config = _api(persistence)
    before = deepcopy(config)
    client = _client(api, role="platform_admin")

    response = client.request(method, path, json=body)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
    assert api.router.config is config
    assert api.monitor.config is config
    assert config == before
    assert persistence.attempted_config is not config
    assert persistence.attempted_config != before


def test_legacy_topology_mutation_stays_in_memory_without_persistence() -> None:
    api, config = _api()
    client = TestClient(Starlette(routes=create_region_routes(api)))

    response = client.post(
        "/admin/regions/spokes",
        json={"region": "eu-west-1"},
    )

    assert response.status_code == 201
    assert [spoke.region for spoke in config.spokes] == [
        "us-east-1",
        "eu-west-1",
    ]


def test_fleet_refresh_adopts_topology_without_resetting_live_health() -> None:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[
            SpokeConfig(
                region="us-east-1",
                status=SpokeStatus.UNHEALTHY,
            )
        ],
    )

    class Persistence:
        enabled = True

        async def load_region_topology_snapshot(self):
            return {
                "revision": 4,
                "hub_region": "us-east-1",
                "health_check_interval_seconds": 20,
                "failover_threshold_consecutive": 2,
                "failover_cooldown_seconds": 45,
                "data_residency_strict": True,
                "spokes": [
                    {"region": "us-east-1", "role": "primary"},
                    {"region": "eu-west-1", "role": "active"},
                ],
            }

        async def get_config_version(self):
            return 0

        async def load_projects_or_none(self):
            return {}

        async def load_user_configs_or_none(self):
            return {}

    sync = ConfigSyncService(
        projects={},
        user_configs={},
        cost_tracker=CostTracker(pricing_config={}),
        persistence=Persistence(),
        region_config=config,
    )

    assert asyncio.run(sync.refresh_if_stale()) is True
    assert config.revision == 4
    assert config.data_residency_strict is True
    assert [spoke.region for spoke in config.spokes] == [
        "us-east-1",
        "eu-west-1",
    ]
    assert config.get_spoke("us-east-1").status is SpokeStatus.UNHEALTHY
    assert config.get_spoke("eu-west-1").status is SpokeStatus.HEALTHY


class _TopologyPersistence:
    enabled = True

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.fail_load = False

    async def load_region_topology_snapshot(self):
        if self.fail_load:
            raise RuntimeError("store unavailable")
        return self.snapshot

    async def get_config_version(self):
        return 0

    async def load_projects_or_none(self):
        return {}

    async def load_user_configs_or_none(self):
        return {}


def _snapshot(
    revision: int,
    regions: tuple[str, ...] = ("us-east-1",),
    *,
    strict: bool = False,
) -> dict:
    return {
        "revision": revision,
        "hub_region": "us-east-1",
        "health_check_interval_seconds": 20,
        "failover_threshold_consecutive": 2,
        "failover_cooldown_seconds": 45,
        "data_residency_strict": strict,
        "spokes": [
            {
                "region": region,
                "role": "primary" if index == 0 else "active",
            }
            for index, region in enumerate(regions)
        ],
    }


def test_delayed_refresh_cannot_roll_back_newer_live_revision() -> None:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[SpokeConfig(region="us-east-1")],
        data_residency_strict=True,
        revision=6,
    )
    persistence = _TopologyPersistence(_snapshot(5, strict=False))
    sync = ConfigSyncService(
        projects={},
        user_configs={},
        cost_tracker=CostTracker(pricing_config={}),
        persistence=persistence,
        region_config=config,
    )
    sync.note_local_version(0)

    assert asyncio.run(sync.refresh_if_stale()) is False
    assert config.revision == 6
    assert config.data_residency_strict is True


def test_region_store_failure_is_not_treated_as_stale_success() -> None:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[SpokeConfig(region="us-east-1")],
    )
    persistence = _TopologyPersistence(_snapshot(1))
    persistence.fail_load = True
    sync = ConfigSyncService(
        projects={},
        user_configs={},
        cost_tracker=CostTracker(pricing_config={}),
        persistence=persistence,
        region_config=config,
    )

    with pytest.raises(RegionTopologyUnavailable):
        asyncio.run(sync.refresh_if_stale())


def test_malformed_snapshot_does_not_partially_mutate_live_topology() -> None:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[SpokeConfig(region="us-east-1")],
        revision=3,
    )
    before = deepcopy(config)
    malformed = _snapshot(4, ("eu-west-1",), strict=True)
    malformed["spokes"] = [{}]

    with pytest.raises(KeyError):
        apply_persisted_topology(config, malformed)

    assert config == before


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "health_check_interval_seconds",
            0,
            "health_check_interval_seconds must be a positive integer",
        ),
        (
            "weight",
            -1,
            "weight must be a non-negative integer",
        ),
    ],
)
def test_invalid_snapshot_routing_inputs_are_rejected_atomically(
    field: str,
    value: int,
    message: str,
) -> None:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[SpokeConfig(region="us-east-1")],
        revision=3,
    )
    before = deepcopy(config)
    malformed = _snapshot(4, ("eu-west-1",), strict=True)
    if field == "weight":
        malformed["spokes"][0]["weight"] = value
    else:
        malformed[field] = value

    with pytest.raises(ValueError, match=message):
        apply_persisted_topology(config, malformed)

    assert config == before


@pytest.mark.parametrize(
    ("method", "path", "body", "message"),
    [
        (
            "post",
            "/admin/regions/spokes",
            {"region": "eu-west-1", "weight": -0.5},
            "weight must be a non-negative integer",
        ),
        (
            "put",
            "/admin/regions/spokes/us-east-1",
            {"weight": -1},
            "weight must be a non-negative integer",
        ),
        (
            "put",
            "/admin/regions/config",
            {"health_check_interval_seconds": 0},
            "health_check_interval_seconds must be a positive integer",
        ),
    ],
)
def test_admin_rejects_invalid_routing_inputs_without_mutation(
    method: str,
    path: str,
    body: dict,
    message: str,
) -> None:
    api, config = _api()
    before = deepcopy(config)

    response = _client(api, role="platform_admin").request(
        method,
        path,
        json=body,
    )

    assert response.status_code == 400
    assert response.json() == {"error": message}
    assert config == before


def test_health_change_during_durable_write_survives_publication() -> None:
    config = HubConfig(
        hub_region="us-east-1",
        spokes=[SpokeConfig(region="us-east-1")],
    )

    class Persistence:
        enabled = True

        async def save_region_topology(self, candidate, expected_revision):
            config.get_spoke("us-east-1").status = SpokeStatus.UNHEALTHY
            return expected_revision + 1

    api = RegionAPI(
        router=RegionRouter(config),
        monitor=SpokeHealthMonitor(config),
        persistence=Persistence(),
    )
    response = _client(api, role="platform_admin").put(
        "/admin/regions/config",
        json={"data_residency_strict": True},
    )

    assert response.status_code == 200
    assert config.get_spoke("us-east-1").status is SpokeStatus.UNHEALTHY


def test_monitor_follows_fleet_topology_transitions() -> None:
    async def scenario() -> None:
        config = HubConfig(
            hub_region="us-east-1",
            spokes=[SpokeConfig(region="us-east-1")],
        )
        monitor = SpokeHealthMonitor(config)
        persistence = _TopologyPersistence(
            _snapshot(1, ("us-east-1", "eu-west-1"))
        )
        sync = ConfigSyncService(
            projects={},
            user_configs={},
            cost_tracker=CostTracker(pricing_config={}),
            persistence=persistence,
            region_config=config,
            health_monitor=monitor,
        )
        sync.CONFIG_SYNC_TTL_SECONDS = 0
        sync.note_local_version(0)

        assert await sync.refresh_if_stale() is True
        assert monitor.is_running is True

        persistence.snapshot = _snapshot(2)
        assert await sync.refresh_if_stale() is True
        assert monitor.is_running is False

    asyncio.run(scenario())


def test_starlette_request_fails_closed_when_topology_cannot_refresh() -> None:
    class Sync:
        async def refresh_if_stale(self):
            raise RegionTopologyUnavailable("store unavailable")

    async def private_route(request):
        return JSONResponse({"status": "should not run"})

    app = Starlette(routes=[Route("/private", private_route)])
    app.add_middleware(
        AuthMiddleware,
        mode="LOG_ONLY",
        config_sync=Sync(),
    )

    response = TestClient(app).get("/private")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "region_topology_unavailable"
    )
