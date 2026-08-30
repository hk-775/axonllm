"""Unit tests for src.gateway.bootstrap."""

from __future__ import annotations

import os

import pytest

from src.gateway.bootstrap import (
    ControlAPIComponents,
    GatewayComponents,
    _merge_policies,
    _persistence_readiness,
    build_control_api,
    build_gateway_components,
    build_starlette_app,
    build_gateway_agent,
)
from src.gateway.agent import GatewayAgent
from src.gateway.config import AppConfig


@pytest.fixture
def demo_app_config() -> AppConfig:
    """AppConfig pointing at real config files with demo data enabled."""
    return AppConfig(
        models_config_path="config/models.yaml",
        providers_config_path="config/providers.yaml",
        pricing_config_path="config/pricing.yaml",
        demo_seed_config_path="config/demo_seed.yaml",
        catalog_config_path="config/catalog.yaml",
        load_demo_data=True,
    )


@pytest.fixture
def minimal_app_config() -> AppConfig:
    """AppConfig with demo data disabled."""
    return AppConfig(
        models_config_path="config/models.yaml",
        providers_config_path="config/providers.yaml",
        pricing_config_path="config/pricing.yaml",
        demo_seed_config_path="config/demo_seed.yaml",
        catalog_config_path="config/catalog.yaml",
        load_demo_data=False,
    )


class TestBuildGatewayComponents:
    def test_policy_merge_keeps_same_name_in_distinct_tenants(self):
        merged = _merge_policies(
            [
                {
                    "tenant_id": "tenant-a",
                    "name": "guard",
                    "mode": "ENFORCE",
                },
                {
                    "tenant_id": "tenant-b",
                    "name": "guard",
                    "mode": "LOG_ONLY",
                },
            ],
            [
                {
                    "tenant_id": "tenant-a",
                    "name": "guard",
                    "mode": "LOG_ONLY",
                }
            ],
        )

        assert {
            (policy["tenant_id"], policy["mode"])
            for policy in merged
        } == {
            ("tenant-a", "LOG_ONLY"),
            ("tenant-b", "LOG_ONLY"),
        }

    def test_returns_gateway_components(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        assert isinstance(comp, GatewayComponents)
        assert comp.cost_tracker is not None
        assert comp.health_tracker is not None
        assert comp.registry is not None
        assert comp.router is not None
        assert comp.gateway_agent is not None

    def test_demo_data_loaded(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        assert "proj-alpha" in comp.projects
        assert "proj-beta" in comp.projects
        assert len(comp.policies) > 0
        # Usage seeds should have been recorded
        assert len(comp.cost_tracker._records) > 0

    def test_no_demo_data_when_disabled(self, minimal_app_config: AppConfig):
        comp = build_gateway_components(minimal_app_config)
        assert len(comp.projects) == 0
        assert len(comp.policies) == 0
        assert len(comp.cost_tracker._records) == 0

    def test_custom_oidc_resource_claims_reach_the_verifier(
        self,
        minimal_app_config: AppConfig,
    ):
        minimal_app_config.oidc_tenant_claim = (
            "https://axonllm.example/tenant"
        )
        minimal_app_config.oidc_project_claim = (
            "https://axonllm.example/project"
        )

        comp = build_gateway_components(minimal_app_config)

        assert comp.oidc_service._config.claim_mappings["tenant_id"] == (
            "https://axonllm.example/tenant"
        )
        assert comp.oidc_service._config.claim_mappings["project_id"] == (
            "https://axonllm.example/project"
        )

    def test_pricing_loaded(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        # Pricing should have been loaded from config/pricing.yaml
        assert len(comp.cost_tracker.pricing_config) > 0
        assert "openai" in comp.cost_tracker.pricing_config

    def test_production_routes_only_priced_mappings(
        self,
        minimal_app_config: AppConfig,
    ):
        minimal_app_config.deployment_profile = "production"

        comp = build_gateway_components(minimal_app_config)

        assert comp.router.require_priced_mappings is True
        assert comp.router.is_model_available("gpt-5.5") is False
        assert comp.router.is_model_available("gpt-oss-120b-mantle") is True
        assert "gemini-2.5-pro" in comp.registry.models
        assert (
            "gemini-2.5-pro"
            in comp.cost_tracker.pricing_config["google_ai"]
        )

    def test_catalog_loaded(self, demo_app_config: AppConfig):
        comp = build_gateway_components(demo_app_config)
        assert isinstance(comp.catalog, dict)
        assert len(comp.catalog) > 0


class TestBuildStaletteApp:
    def test_returns_starlette_app(self, demo_app_config: AppConfig):
        app = build_starlette_app(demo_app_config)
        assert app is not None
        assert hasattr(app, "routes")

    def test_disabled_browser_auth_has_an_explicit_config(
        self,
        demo_app_config: AppConfig,
    ):
        from starlette.testclient import TestClient

        response = TestClient(build_starlette_app(demo_app_config)).get(
            "/auth/config"
        )

        assert response.status_code == 200
        assert response.json() == {"browser_auth": {"enabled": False}}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"

    def test_lifespan_closes_provider_connections(
        self,
        demo_app_config: AppConfig,
        monkeypatch,
    ):
        from starlette.testclient import TestClient

        closed = []

        async def close(factory):
            closed.append(factory)

        monkeypatch.setattr(
            "src.gateway.multi_provider_factory.MultiProviderFactory.close",
            close,
        )
        with TestClient(build_starlette_app(demo_app_config)) as client:
            assert client.get("/health").status_code == 200

        assert len(closed) == 1

    def test_control_api_uses_injected_services_without_data_plane_or_workers(
        self,
        minimal_app_config: AppConfig,
        monkeypatch,
    ):
        from starlette.testclient import TestClient

        components = build_gateway_components(minimal_app_config)

        def fail_client_agent(*args, **kwargs):
            raise AssertionError("control API constructed an inference client")

        def fail_worker(*args, **kwargs):
            raise AssertionError("control API constructed a worker")

        monkeypatch.setattr(
            "src.gateway.bootstrap.ClientAgent",
            fail_client_agent,
        )
        monkeypatch.setattr(
            "src.gateway.bootstrap.build_worker",
            fail_worker,
        )
        app = build_control_api(
            minimal_app_config,
            ControlAPIComponents.from_gateway(components),
        )
        route_paths = {
            route.path
            for route in app.routes
            if hasattr(route, "path")
        }

        assert "/admin/dashboard" in route_paths
        assert "/v1/chat/completions" not in route_paths
        assert "/api/chat" not in route_paths
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    def test_readiness_route_is_separate_from_liveness(
        self,
        minimal_app_config: AppConfig,
    ):
        from starlette.testclient import TestClient

        response = TestClient(build_starlette_app(minimal_app_config)).get(
            "/ready"
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "ready": True,
            "dependencies": {
                "persistence": "disabled",
                "routing_configuration": "ready",
                "security_event_outbox": "disabled",
            },
        }

    def test_control_plane_sync_holds_the_live_model_registry(
        self,
        minimal_app_config: AppConfig,
    ):
        from src.gateway.admin.routes import AdminAPI
        from src.gateway.middleware.auth import AuthMiddleware

        app = build_starlette_app(minimal_app_config)
        admin_api = next(
            route.endpoint.__self__
            for route in app.routes
            if getattr(route, "path", None) == "/admin/models"
            and isinstance(
                getattr(route.endpoint, "__self__", None),
                AdminAPI,
            )
        )
        middleware_kwargs = next(
            middleware.kwargs
            for middleware in app.user_middleware
            if middleware.cls is AuthMiddleware
        )

        assert (
            middleware_kwargs["config_sync"]._model_registry
            is admin_api.model_registry
        )

    def test_readiness_reports_degraded_routing_without_stopping_lkg(
        self,
        minimal_app_config: AppConfig,
    ):
        from starlette.testclient import TestClient

        app = build_starlette_app(minimal_app_config)
        admin_api = next(
            route.endpoint.__self__
            for route in app.routes
            if getattr(route, "path", None) == "/admin/models"
        )
        admin_api._config_sync._routing_sync_error = (
            "signature_verification_failed"
        )

        response = TestClient(app).get("/ready")

        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["ready"] is True
        assert response.json()["dependencies"][
            "routing_configuration"
        ] == "degraded"


class TestPersistenceReadiness:
    @pytest.mark.asyncio
    async def test_unreachable_enabled_store_is_not_ready(self):
        class _Persistence:
            enabled = True

            async def health_status(self):
                return {"enabled": True, "reachable": False}

        ready, dependencies = await _persistence_readiness(_Persistence())

        assert ready is False
        assert dependencies == {"persistence": "unavailable"}

    @pytest.mark.asyncio
    async def test_store_timeout_is_not_ready(self):
        import asyncio

        class _Persistence:
            enabled = True

            async def health_status(self):
                await asyncio.sleep(0.05)
                return {"enabled": True, "reachable": True}

        ready, dependencies = await _persistence_readiness(
            _Persistence(),
            timeout_seconds=0.001,
        )

        assert ready is False
        assert dependencies == {"persistence": "timeout"}


class TestCedarWiring:
    """The admin API and the auth middleware must share one evaluator instance.

    `POST /admin/policies` recompiles the evaluator it holds. If bootstrap built a
    second one for `AuthMiddleware`, the recompile would land on an object no
    request consults and the policy would silently do nothing — the same
    end-state as the pre-fix "needs a restart" bug, but harder to spot because the
    route reports success and `GET /admin/policies` shows the policy.
    """

    def _services(self, app):
        """The AdminAPI's evaluator and the AuthMiddleware's, from a built app."""
        from src.gateway.admin.routes import AdminAPI
        from src.gateway.middleware.auth import AuthMiddleware

        admin_api = next(
            route.endpoint.__self__
            for route in app.routes
            if getattr(route, "path", None) == "/admin/policies"
            and isinstance(getattr(route.endpoint, "__self__", None), AdminAPI)
        )
        middleware_kwargs = next(
            mw.kwargs for mw in app.user_middleware if mw.cls is AuthMiddleware
        )
        return admin_api._policy_service, middleware_kwargs["policy_service"]

    def test_the_admin_api_and_auth_middleware_share_one_evaluator(
        self, demo_app_config: AppConfig
    ):
        admin_service, middleware_service = self._services(
            build_starlette_app(demo_app_config)
        )
        assert admin_service is not None
        assert admin_service is middleware_service

    def test_an_evaluator_exists_even_with_no_policies(
        self, minimal_app_config: AppConfig
    ):
        """A clean install boots with zero policies. It still needs an evaluator,
        or the first policy an operator adds has nothing to recompile.

        Safe because an empty policy set governs no action, so wiring it changes
        no decision — asserted below rather than argued.
        """
        app = build_starlette_app(minimal_app_config)
        admin_service, middleware_service = self._services(app)
        assert admin_service is not None
        assert admin_service is middleware_service

    def test_that_empty_evaluator_denies_nothing(self, minimal_app_config: AppConfig):
        import asyncio

        from src.gateway.models import AuthMethod, RequestContext

        service, _ = self._services(build_starlette_app(minimal_app_config))
        ctx = RequestContext(
            user_id="u1",
            project_id="p1",
            roles=[],
            scopes=[],
            auth_method=AuthMethod.API_KEY,
        )
        for method in ("get", "post", "put", "patch", "delete"):
            assert asyncio.run(service.evaluate(ctx, method, "/api/chat")) == "ALLOW", method


class TestPersistedPoliciesAreLoadedAtStartup:
    """The restart half of "a policy written over HTTP survives a restart".

    `POST /admin/policies` writing to DynamoDB is worth nothing if boot does not
    read it back — and the failure is silent, because a gateway with no policies
    starts fine and just enforces nothing on the Cedar layer.
    """

    def _persistence(self, stored: list[dict]):
        """Real DynamoPersistence with only the table boundary replaced, so
        `deserialize_cedar_policy` is exercised on the way in."""
        from boto3.dynamodb.conditions import Attr
        from src.gateway.persistence import DynamoPersistence

        cedar_filter = Attr("entity_type").eq("cedar_policy").get_expression()

        class _Loading(DynamoPersistence):
            def __init__(self) -> None:
                super().__init__()
                self._enabled = True

            async def create_table_if_not_exists(self) -> None:
                return None

            def _get_table(self):
                class _Table:
                    def scan(self, **kwargs):
                        # Only the Cedar-policy scan yields anything; every other
                        # entity_type sees an empty table. Compared on the parsed
                        # expression because boto3's condition objects have no
                        # useful repr and no equality.
                        expr = kwargs.get("FilterExpression")
                        match = expr is not None and expr.get_expression() == cedar_filter
                        return {"Items": stored if match else []}

                    def get_item(self, **kwargs):
                        return {}

                    def put_item(self, **kwargs):
                        # Boot writes audit records; accepted and dropped so the
                        # load path is what this test isolates.
                        return {}

                return _Table()

        return _Loading()

    def test_a_stored_policy_reaches_the_evaluator(self, minimal_app_config, monkeypatch):
        import asyncio

        from src.gateway import bootstrap as bootstrap_mod
        from src.gateway.models import AuthMethod, RequestContext

        stored = [{
            "name": "no-writes",
            "policy_text": 'forbid(principal, action == Action::"write", resource);',
            "mode": "ENFORCE",
            "entity_type": "cedar_policy",
        }]
        persistence = self._persistence(stored)
        monkeypatch.setattr(bootstrap_mod, "DynamoPersistence", lambda *a, **k: persistence)

        comp = build_gateway_components(minimal_app_config)
        assert [p["name"] for p in comp.policies] == ["no-writes"]

        from src.gateway.auth.cedar_policy import CedarPolicyService

        ctx = RequestContext(
            user_id="u1", project_id="p1", roles=[], scopes=[],
            auth_method=AuthMethod.API_KEY,
        )
        service = CedarPolicyService(comp.policies)
        assert asyncio.run(service.evaluate(ctx, "post", "/api/chat")) == "DENY"
        assert asyncio.run(service.evaluate(ctx, "get", "/admin/overview")) == "ALLOW"

    def test_a_persisted_policy_replaces_the_seeded_one_of_the_same_name(
        self, demo_app_config, monkeypatch
    ):
        """Merged by name, not concatenated.

        The demo seed ships `allow-all-write` as a permit. If an operator edits it
        to a forbid, appending both would evaluate the stale permit alongside the
        new forbid — and since forbid wins, the result would happen to look right
        while the reverse edit (forbid to permit) would silently keep denying.
        """
        from src.gateway import bootstrap as bootstrap_mod

        stored = [{
            "name": "allow-all-write",
            "policy_text": 'permit(principal, action == Action::"write", resource);',
            "mode": "LOG_ONLY",
            "entity_type": "cedar_policy",
        }]
        persistence = self._persistence(stored)
        monkeypatch.setattr(bootstrap_mod, "DynamoPersistence", lambda *a, **k: persistence)
        async def completed_seed(*args, **kwargs):
            return None
        monkeypatch.setattr(
            bootstrap_mod,
            "_coordinate_demo_seed",
            completed_seed,
        )

        comp = build_gateway_components(demo_app_config)
        matching = [p for p in comp.policies if p["name"] == "allow-all-write"]
        assert len(matching) == 1, "seeded and persisted copies were both kept"
        assert matching[0]["mode"] == "LOG_ONLY", "the persisted edit did not win"


class TestSpendCountersAtStartup:
    """Budget enforcement is fleet-wide, which makes boot order load-bearing.

    Spend lives in a shared DynamoDB counter, so a starting instance has to adopt
    the fleet total — and must not add its own view of history on top of a counter
    that already contains it, or write fabricated demo spend into it.
    """

    def _persistence(self, spend: dict[str, float], writes: list):
        """Real DynamoPersistence with only the table boundary replaced."""
        from src.gateway.persistence import DynamoPersistence

        class _Counters(DynamoPersistence):
            def __init__(self) -> None:
                super().__init__()
                self._enabled = True

            async def create_table_if_not_exists(self) -> None:
                return None

            def _get_table(self):
                class _Table:
                    def scan(self, **kwargs):
                        return {"Items": []}

                    def get_item(self, **kwargs):
                        pk = kwargs["Key"]["PK"]
                        if pk.startswith("SPEND#") and pk in spend:
                            from decimal import Decimal
                            return {"Item": {"spend": Decimal(str(spend[pk]))}}
                        return {}

                    def put_item(self, **kwargs):
                        return {}

                    def update_item(self, **kwargs):
                        writes.append(kwargs["Key"]["PK"])
                        return {"Attributes": {"spend": 0}}

                return _Table()

        return _Counters()

    def test_the_enforcer_adopts_the_fleet_total(self, minimal_app_config, monkeypatch):
        """A booting task must not believe every project has spent nothing.

        Without this, the first request to each new task after a deploy is
        admitted against a budget the fleet had already exhausted — and
        `GET /admin/quotas` reports $0 for a project that is over its limit.
        """
        from src.gateway import bootstrap as bootstrap_mod

        persistence = self._persistence({"SPEND#quota#proj-alpha": 4200.0}, [])
        monkeypatch.setattr(bootstrap_mod, "DynamoPersistence", lambda *a, **k: persistence)
        monkeypatch.setattr(
            bootstrap_mod, "_load_persisted_state",
            _loads(({"proj-alpha": _project("proj-alpha")}, {}, [], [], [], [], None)),
        )

        comp = build_gateway_components(minimal_app_config)
        assert comp.quota_enforcer.get_spend("proj-alpha") == 4200.0, (
            "started up believing a project with $4200 of fleet spend had spent nothing"
        )

    def test_persisted_history_is_not_added_to_the_counter(self, minimal_app_config, monkeypatch):
        """`load_records` sums records the shared counter already includes.

        Adding rather than replacing would inflate a project's spend by its whole
        history on every single restart.
        """
        from datetime import datetime, timezone

        from src.gateway import bootstrap as bootstrap_mod
        from src.gateway.models import UsageRecord

        record = UsageRecord(
            request_id="r1", project_id="proj-alpha", user_id="u1",
            provider="anthropic", model="claude-sonnet-4",
            prompt_tokens=1, completion_tokens=1, total_tokens=2, cost=30.0,
            timestamp=datetime.now(timezone.utc),
        )
        persistence = self._persistence({"SPEND#project#proj-alpha": 30.0}, [])
        monkeypatch.setattr(bootstrap_mod, "DynamoPersistence", lambda *a, **k: persistence)
        monkeypatch.setattr(
            bootstrap_mod, "_load_persisted_state", _loads(({}, {}, [record], [], [], [], None)),
        )

        comp = build_gateway_components(minimal_app_config)
        assert comp.cost_tracker._project_spend["proj-alpha"] == 30.0, (
            "added persisted history to a shared counter that already contained it"
        )

    def test_demo_spend_is_never_written_to_the_shared_counter(
        self, demo_app_config, monkeypatch
    ):
        """Every instance fabricates the same seed, and ADD is not idempotent.

        Sharing it would multiply demo spend by the instance count and again on
        every restart, until a demo gateway refused its own seeded projects.
        """
        from src.gateway import bootstrap as bootstrap_mod

        writes: list = []
        persistence = self._persistence({}, writes)
        monkeypatch.setattr(bootstrap_mod, "DynamoPersistence", lambda *a, **k: persistence)
        async def completed_seed(*args, **kwargs):
            return None
        monkeypatch.setattr(
            bootstrap_mod,
            "_coordinate_demo_seed",
            completed_seed,
        )

        comp = build_gateway_components(demo_app_config)
        assert comp.cost_tracker._records, "expected the seed to have been applied"
        assert [w for w in writes if w.startswith("SPEND#")] == [], (
            "wrote fabricated demo spend into the shared budget counter"
        )


def _loads(result):
    """Stand in for the async `_load_persisted_state`, which boot awaits."""

    async def _load(_persistence):
        return result

    return _load


def _project(project_id: str):
    from src.gateway.models import Project

    return Project(project_id=project_id, name=project_id)


class TestBuildGatewayAgent:
    def test_returns_gateway_agent(self, minimal_app_config: AppConfig):
        agent = build_gateway_agent(minimal_app_config)
        assert isinstance(agent, GatewayAgent)
