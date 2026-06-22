"""Tests for quota admin API routes."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.quota_routes import QuotaAPI, create_quota_routes
from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
from src.gateway.models import PolicyNode
from src.gateway.quota_enforcer import QuotaEnforcer


class FakePersistence:
    def __init__(self):
        self._nodes = {}
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


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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
        enforcer.record_spend("proj:ml", 1234.56)
        resp = client.get("/admin/quotas/proj:ml")
        data = resp.json()
        assert data["usage"]["current_spend"] == 1234.56
        assert data["usage"]["budget_remaining"] == round(5000.0 - 1234.56, 4)

    def test_budget_utilization_pct(self, setup):
        client, enforcer, _ = setup
        enforcer.record_spend("proj:ml", 2500.0)
        resp = client.get("/admin/quotas/proj:ml")
        assert resp.json()["usage"]["budget_utilization_pct"] == 50.0


class TestResetSpend:
    def test_resets_spend(self, setup):
        client, enforcer, _ = setup
        enforcer.record_spend("proj:ml", 1000.0)
        resp = client.post("/admin/quotas/proj:ml/reset")
        assert resp.status_code == 200
        assert resp.json()["previous_spend"] == 1000.0
        assert resp.json()["current_spend"] == 0.0
        assert enforcer.get_spend("proj:ml") == 0.0


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
        enforcer.record_spend("proj:ml", 4999.0)
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
