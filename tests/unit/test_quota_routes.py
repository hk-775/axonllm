"""Tests for quota admin API routes."""

import asyncio
import dataclasses

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.quota_routes import QuotaAPI, create_quota_routes
from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.models import (
    PolicyNode,
    Project,
    RequestContext,
    ResolvedPolicy,
)
from src.gateway.quota_enforcer import QuotaEnforcer


class FakePersistence:
    def __init__(self):
        self._nodes = {}
        self._tenant_nodes = {}
        self._enabled = True

    @property
    def enabled(self):
        return self._enabled

    async def save_policy_node(self, node):
        self._nodes[node.node_id] = node

    async def get_policy_node(self, node_id):
        return self._nodes.get(node_id)

    async def load_all_policy_nodes(self):
        return list(self._nodes.values())

    async def save_tenant_policy_node(self, tenant_id, node):
        self._tenant_nodes.setdefault(tenant_id, {})[node.node_id] = node
        return True

    async def load_tenant_policy_nodes(self, tenant_id):
        return list(self._tenant_nodes.get(tenant_id, {}).values())


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def setup():
    persistence = FakePersistence()
    resolver = PolicyHierarchyResolver(persistence=persistence, cache_ttl_seconds=0)
    enforcer = QuotaEnforcer()

    # Create a policy hierarchy
    org = PolicyNode("org:acme", "org", None, "Acme",
                     limits={"rate_limit_rpm": 1000, "budget_limit": 50000.0,
                             "allowed_models": ["claude-opus", "claude-sonnet"]})
    proj = PolicyNode("proj:ml", "project", "org:acme", "ML",
                      limits={"rate_limit_rpm": 200, "budget_limit": 5000.0})
    _run(persistence.save_policy_node(org))
    _run(persistence.save_policy_node(proj))

    quota_api = QuotaAPI(quota_enforcer=enforcer, policy_resolver=resolver)
    app = Starlette(routes=create_quota_routes(quota_api))
    client = TestClient(app)
    return client, enforcer, resolver


class TestGetProjectQuota:
    def test_returns_resolved_limits(self, setup):
        client, enforcer, _ = setup
        resp = client.get("/admin/quotas/proj:ml")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "proj:ml"
        assert data["policy_limits"]["rate_limit_rpm"] == 200
        assert data["policy_limits"]["budget_limit"] == 5000.0
        assert set(data["policy_limits"]["allowed_models"]) == {"claude-opus", "claude-sonnet"}

    def test_shows_current_spend(self, setup):
        client, enforcer, _ = setup
        asyncio.run(enforcer.record_spend("proj:ml", 1234.56))
        resp = client.get("/admin/quotas/proj:ml")
        data = resp.json()
        assert data["usage"]["current_spend"] == 1234.56
        assert data["usage"]["budget_remaining"] == round(5000.0 - 1234.56, 4)

    def test_budget_utilization_pct(self, setup):
        client, enforcer, _ = setup
        asyncio.run(enforcer.record_spend("proj:ml", 2500.0))
        resp = client.get("/admin/quotas/proj:ml")
        assert resp.json()["usage"]["budget_utilization_pct"] == 50.0

    def test_every_resolved_policy_field_is_reported(self, setup):
        """policy_limits is a hand-written whitelist, so a field added to
        ResolvedPolicy is reported nowhere until someone remembers to add it
        here too. That has already happened twice: pii_reinject and the two
        pii_ner_* fields resolved correctly on the request path but were
        invisible in this response, so two projects with different policies
        looked identical. Comparing against the dataclass makes the omission a
        test failure instead of a silent reporting gap."""
        client, _, _ = setup
        reported = set(client.get("/admin/quotas/proj:ml").json()["policy_limits"])
        expected = {f.name for f in dataclasses.fields(ResolvedPolicy)}
        assert reported == expected, (
            f"not reported: {expected - reported}; unknown: {reported - expected}")

    def test_entity_detection_settings_are_visible(self, setup):
        """The one policy feature that costs money per request. If the admin API
        does not surface it, an operator cannot tell which projects are paying
        for Comprehend without reading the seed file."""
        client, _, resolver = setup
        node = PolicyNode("proj:ner", "project", "org:acme", "NER",
                          limits={"pii_redaction_enabled": True,
                                  "pii_ner_enabled": True,
                                  "pii_ner_types": ["name"]})
        _run(resolver._persistence.save_policy_node(node))
        _run(resolver.load_nodes())

        limits = client.get("/admin/quotas/proj:ner").json()["policy_limits"]
        assert limits["pii_ner_enabled"] is True
        assert limits["pii_ner_types"] == ["name"]
        # And a project without it reports off rather than absent.
        assert client.get("/admin/quotas/proj:ml").json()[
            "policy_limits"]["pii_ner_enabled"] is False


class TestResetSpend:
    def test_resets_spend(self, setup):
        client, enforcer, _ = setup
        asyncio.run(enforcer.record_spend("proj:ml", 1000.0))
        resp = client.post("/admin/quotas/proj:ml/reset")
        assert resp.status_code == 200
        assert resp.json()["previous_spend"] == 1000.0
        assert resp.json()["current_spend"] == 0.0
        assert enforcer.get_spend("proj:ml") == 0.0

    def test_a_failed_shared_reset_is_not_reported_as_done(self):
        """503, not 200, when the shared counter kept its old total.

        Spend is fleet-wide, so a reset that only cleared this process leaves
        every other instance still refusing the project. Answering "reset" would
        send an operator away believing they had unblocked it.
        """
        class _CounterDown:
            enabled = True

            async def add_spend(self, scope, ident, cost):
                return 1000.0

            async def get_spend(self, scope, ident):
                return 1000.0

            async def reset_spend(self, scope, ident):
                return False

        enforcer = QuotaEnforcer(persistence=_CounterDown())
        resolver = PolicyHierarchyResolver(persistence=FakePersistence(), cache_ttl_seconds=0)
        client = TestClient(Starlette(routes=create_quota_routes(
            QuotaAPI(quota_enforcer=enforcer, policy_resolver=resolver))))

        resp = client.post("/admin/quotas/proj:ml/reset")
        assert resp.status_code == 503, "claimed success while the fleet counter was unchanged"
        body = resp.json()
        assert body["status"] == "reset_failed"
        assert body["current_spend"] == 1000.0, "reported $0 for a counter still holding $1000"

    def test_the_reported_spend_comes_from_the_shared_counter(self):
        """`previous_spend` must be the fleet figure, not this process's share.

        An instance that never served the project holds $0 locally, so a reset
        would report "previous_spend: 0" for a project that had spent thousands.
        """
        class _Counter:
            enabled = True

            async def get_spend(self, scope, ident):
                return 250.0

            async def reset_spend(self, scope, ident):
                return True

        enforcer = QuotaEnforcer(persistence=_Counter())
        resolver = PolicyHierarchyResolver(persistence=FakePersistence(), cache_ttl_seconds=0)
        client = TestClient(Starlette(routes=create_quota_routes(
            QuotaAPI(quota_enforcer=enforcer, policy_resolver=resolver))))

        assert enforcer.get_spend("proj:ml") == 0.0  # this process served nothing
        resp = client.post("/admin/quotas/proj:ml/reset")
        assert resp.json()["previous_spend"] == 250.0

    def test_a_quota_read_reflects_another_instance(self):
        """`GET /admin/quotas/{id}` must not depend on which task answers."""
        class _Counter:
            enabled = True

            async def get_spend(self, scope, ident):
                return 4200.0

        enforcer = QuotaEnforcer(persistence=_Counter())
        persistence = FakePersistence()
        resolver = PolicyHierarchyResolver(persistence=persistence, cache_ttl_seconds=0)
        _run(persistence.save_policy_node(PolicyNode(
            "proj:ml", "project", None, "ML", limits={"budget_limit": 5000.0})))
        _run(resolver.load_nodes())
        client = TestClient(Starlette(routes=create_quota_routes(
            QuotaAPI(quota_enforcer=enforcer, policy_resolver=resolver))))

        usage = client.get("/admin/quotas/proj:ml").json()["usage"]
        assert usage["current_spend"] == 4200.0, (
            "reported this instance's $0 for a project the fleet has spent $4200 on"
        )
        assert usage["budget_remaining"] == 800.0


class TestSimulateRequest:
    def test_allowed_request(self, setup):
        client, _, _ = setup
        resp = client.post("/admin/quotas/simulate", json={
            "project_id": "proj:ml",
            "model": "claude-opus",
            "provider": "anthropic",
            "max_tokens": 4096,
            "estimated_cost": 0.05,
        })
        assert resp.status_code == 200
        assert resp.json()["allowed"] is True

    def test_blocked_model(self, setup):
        client, _, _ = setup
        resp = client.post("/admin/quotas/simulate", json={
            "project_id": "proj:ml",
            "model": "gpt-4o",
            "estimated_cost": 0.0,
        })
        data = resp.json()
        assert data["allowed"] is False
        assert data["limit_type"] == "allowed_models"

    def test_blocked_budget(self, setup):
        client, enforcer, _ = setup
        asyncio.run(enforcer.record_spend("proj:ml", 4999.0))
        resp = client.post("/admin/quotas/simulate", json={
            "project_id": "proj:ml",
            "model": "claude-opus",
            "estimated_cost": 5.0,
        })
        data = resp.json()
        assert data["allowed"] is False
        assert data["limit_type"] == "budget_limit"

    def test_response_includes_resolved_policy(self, setup):
        client, _, _ = setup
        resp = client.post("/admin/quotas/simulate", json={
            "project_id": "proj:ml",
            "model": "claude-sonnet",
            "estimated_cost": 0.01,
        })
        policy = resp.json()["resolved_policy"]
        assert policy["rate_limit_rpm"] == 200
        assert policy["budget_limit"] == 5000.0


class TestTenantQualifiedQuotaRoutes:
    @staticmethod
    def _setup_tenants():
        persistence = FakePersistence()
        resolver = PolicyHierarchyResolver(
            persistence=persistence,
            cache_ttl_seconds=0,
        )
        enforcer = QuotaEnforcer()
        _run(persistence.save_tenant_policy_node(
            "tenant-a",
            PolicyNode(
                "same-project",
                "project",
                None,
                "Tenant A",
                limits={
                    "budget_limit": 100.0,
                    "allowed_models": ["model-a"],
                },
            ),
        ))
        _run(persistence.save_tenant_policy_node(
            "tenant-b",
            PolicyNode(
                "same-project",
                "project",
                None,
                "Tenant B",
                limits={
                    "budget_limit": 200.0,
                    "allowed_models": ["model-b"],
                },
            ),
        ))
        _run(enforcer.record_spend(
            "same-project",
            25.0,
            tenant_id="tenant-a",
        ))
        _run(enforcer.record_spend(
            "same-project",
            75.0,
            tenant_id="tenant-b",
        ))
        app = Starlette(routes=create_quota_routes(QuotaAPI(
            quota_enforcer=enforcer,
            policy_resolver=resolver,
        )))
        return TestClient(app), enforcer

    def test_same_project_id_reads_policy_and_spend_for_requested_tenant(self):
        client, _ = self._setup_tenants()

        tenant_a = client.get(
            "/admin/quotas/same-project?tenant_id=tenant-a"
        ).json()
        tenant_b = client.get(
            "/admin/quotas/same-project?tenant_id=tenant-b"
        ).json()

        assert tenant_a["tenant_id"] == "tenant-a"
        assert tenant_a["policy_limits"]["budget_limit"] == 100.0
        assert tenant_a["usage"]["current_spend"] == 25.0
        assert tenant_b["tenant_id"] == "tenant-b"
        assert tenant_b["policy_limits"]["budget_limit"] == 200.0
        assert tenant_b["usage"]["current_spend"] == 75.0

    def test_simulation_uses_body_tenant_scope(self):
        client, _ = self._setup_tenants()

        tenant_a = client.post("/admin/quotas/simulate", json={
            "tenant_id": "tenant-a",
            "project_id": "same-project",
            "model": "model-a",
        }).json()
        tenant_b = client.post("/admin/quotas/simulate", json={
            "tenant_id": "tenant-b",
            "project_id": "same-project",
            "model": "model-a",
        }).json()

        assert tenant_a["allowed"] is True
        assert tenant_b["allowed"] is False
        assert tenant_b["limit_type"] == "allowed_models"

    def test_reset_does_not_clear_same_project_id_in_other_tenant(self):
        client, enforcer = self._setup_tenants()

        response = client.post(
            "/admin/quotas/same-project/reset?tenant_id=tenant-a"
        )

        assert response.status_code == 200
        assert enforcer.get_spend(
            "same-project",
            tenant_id="tenant-a",
        ) == 0.0
        assert enforcer.get_spend(
            "same-project",
            tenant_id="tenant-b",
        ) == 75.0

    def test_blank_tenant_does_not_fall_back_to_legacy(self):
        client, _ = self._setup_tenants()

        response = client.get("/admin/quotas/same-project?tenant_id=")

        assert response.status_code == 400

    def test_tenant_policy_store_outage_returns_503(self):
        class UnavailablePersistence(FakePersistence):
            async def load_tenant_policy_nodes(self, tenant_id):
                return None

        resolver = PolicyHierarchyResolver(UnavailablePersistence())
        client = TestClient(Starlette(routes=create_quota_routes(QuotaAPI(
            quota_enforcer=QuotaEnforcer(),
            policy_resolver=resolver,
        ))))

        response = client.get(
            "/admin/quotas/same-project?tenant_id=tenant-a"
        )

        assert response.status_code == 503
        assert response.json()["error"]["type"] == "policy_store_unavailable"

    def test_authenticated_tenant_rejects_body_override(self):
        persistence = FakePersistence()
        resolver = PolicyHierarchyResolver(persistence)
        app = Starlette(routes=create_quota_routes(QuotaAPI(
            quota_enforcer=QuotaEnforcer(),
            policy_resolver=resolver,
        )))

        async def add_context(request, call_next):
            request.state.context = RequestContext(
                user_id="admin",
                project_id="same-project",
                roles=["admin"],
                scopes=["admin:*"],
                tenant_id="tenant-a",
            )
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
        client = TestClient(app)
        response = client.post("/admin/quotas/simulate", json={
            "tenant_id": "tenant-b",
            "project_id": "same-project",
            "model": "model",
        })

        assert response.status_code == 403

    def test_authenticated_tenant_rejects_query_override(self):
        persistence = FakePersistence()
        resolver = PolicyHierarchyResolver(persistence)
        app = Starlette(routes=create_quota_routes(QuotaAPI(
            quota_enforcer=QuotaEnforcer(),
            policy_resolver=resolver,
        )))

        async def add_context(request, call_next):
            request.state.context = RequestContext(
                user_id="admin",
                project_id="same-project",
                roles=["tenant_admin"],
                scopes=[],
                tenant_id="tenant-a",
            )
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
        response = TestClient(app).get(
            "/admin/quotas/same-project?tenant_id=tenant-b"
        )

        assert response.status_code == 403

    def test_canonical_role_without_tenant_fails_closed(self):
        app = Starlette(routes=create_quota_routes(QuotaAPI(
            quota_enforcer=QuotaEnforcer(),
            policy_resolver=PolicyHierarchyResolver(FakePersistence()),
        )))

        async def add_context(request, call_next):
            request.state.context = RequestContext(
                user_id="admin",
                project_id="same-project",
                roles=["tenant_admin"],
                scopes=[],
            )
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
        response = TestClient(app).get(
            "/admin/quotas/same-project?tenant_id=tenant-a"
        )

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_tenant_scope"

    def test_direct_stub_without_state_keeps_legacy_scope(self):
        persistence = FakePersistence()
        resolver = PolicyHierarchyResolver(persistence)
        _run(persistence.save_policy_node(PolicyNode(
            "same-project",
            "project",
            None,
            "Legacy",
            limits={"budget_limit": 10.0},
        )))

        class RequestStub:
            path_params = {"project_id": "same-project"}
            query_params = {}

        response = _run(QuotaAPI(
            quota_enforcer=QuotaEnforcer(),
            policy_resolver=resolver,
        ).get_project_quota(RequestStub()))

        assert response.status_code == 200
        assert b'"tenant_id":null' in response.body

    def test_authorized_project_supplies_canonical_rpm(self):
        persistence = FakePersistence()
        resolver = PolicyHierarchyResolver(persistence)
        project = Project(
            project_id="same-project",
            name="Project",
            tenant_id="tenant-a",
            rate_limit_rpm=1,
        )
        app = Starlette(routes=create_quota_routes(QuotaAPI(
            quota_enforcer=QuotaEnforcer(),
            policy_resolver=resolver,
        )))

        async def add_context(request, call_next):
            request.state.context = RequestContext(
                user_id="admin",
                project_id="same-project",
                roles=["admin"],
                scopes=["admin:*"],
                tenant_id="tenant-a",
                authorized_project=project,
            )
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
        client = TestClient(app)
        body = {
            "project_id": "same-project",
            "model": "model",
        }

        assert client.post(
            "/admin/quotas/simulate",
            json=body,
        ).json()["allowed"] is True
        second = client.post("/admin/quotas/simulate", json=body).json()
        assert second["allowed"] is False
        assert second["limit_value"] == 1
