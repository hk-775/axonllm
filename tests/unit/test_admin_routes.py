"""Unit tests for admin API endpoints.

Tests the full admin API surface using Starlette TestClient:
- GET /admin/overview
- POST /admin/projects, GET /admin/projects, GET /admin/projects/{id}, PUT /admin/projects/{id}
- GET /admin/usage
- GET /admin/policies, POST /admin/policies
- GET /admin/health

Requirements: 13.1, 13.2, 13.3, 13.5
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    Project,
    ProviderModelMapping,
    TokenPricing,
    UsageRecord,
    ModelConfig,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_app(admin_api: AdminAPI) -> Starlette:
    routes = create_admin_routes(admin_api)
    return Starlette(routes=routes)


def _make_usage_record(
    *,
    project_id: str = "proj-1",
    user_id: str = "user-1",
    provider: str = "openai",
    model: str = "gpt-4",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cost: float = 0.009,
    timestamp: datetime | None = None,
) -> UsageRecord:
    return UsageRecord(
        request_id=f"req-{id(object())}",
        project_id=project_id,
        user_id=user_id,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
        timestamp=timestamp or datetime.utcnow(),
    )


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def cost_tracker():
    pricing = {
        "openai": {"gpt-4": TokenPricing(0.03, 0.06)},
        "anthropic": {"claude-3": TokenPricing(0.003, 0.015)},
    }
    return CostTracker(pricing_config=pricing)


@pytest.fixture
def health_tracker():
    return ProviderHealthTracker()


@pytest.fixture
def model_registry():
    registry = ModelRegistry()
    registry.models["gpt-4"] = ModelConfig(
        name="gpt-4",
        description="GPT-4 class model",
        providers=[
            ProviderModelMapping(provider="openai", model_id="gpt-4-turbo"),
        ],
    )
    return registry


@pytest.fixture
def admin_api(cost_tracker, health_tracker, model_registry):
    return AdminAPI(
        cost_tracker=cost_tracker,
        health_tracker=health_tracker,
        model_registry=model_registry,
    )


@pytest.fixture
def client(admin_api):
    app = _make_app(admin_api)
    return TestClient(app, raise_server_exceptions=False)


# ── Smoke / import tests ────────────────────────────────────────────

def test_admin_api_import():
    """Verify AdminAPI and create_admin_routes can be imported."""
    assert AdminAPI is not None
    assert create_admin_routes is not None


def test_admin_api_init(admin_api):
    """Verify AdminAPI initializes with correct defaults."""
    assert admin_api.projects == {}
    assert admin_api.policies == []
    assert admin_api.cost_tracker is not None
    assert admin_api.health_tracker is not None
    assert admin_api.model_registry is not None


def test_create_admin_routes_returns_routes(admin_api):
    """Verify create_admin_routes returns a list of Route objects."""
    routes = create_admin_routes(admin_api)
    assert isinstance(routes, list)

    paths = [r.path for r in routes]
    # No hardcoded count: the number only ever changed because someone added a
    # route, so asserting it fails every legitimate addition while catching
    # nothing this doesn't catch better. The real bug is a duplicate
    # (path, method) — Starlette matches the first and silently ignores the
    # rest, so a route can be registered and never serve. Keyed on the pair,
    # not the path: GET and POST on /admin/projects are two different
    # endpoints and share a path by design.
    keys = [(r.path, m) for r in routes for m in sorted(r.methods or ())]
    assert len(keys) == len(set(keys)), (
        f"duplicate (path, method): {sorted({k for k in keys if keys.count(k) > 1})}"
    )
    assert "/" in paths
    assert "/admin/dashboard" in paths
    assert "/admin/pricing-drift" in paths
    assert "/admin/production-checklist" in paths
    assert "/admin/overview" in paths
    assert "/admin/projects" in paths
    assert "/admin/projects/{id}" in paths
    assert "/admin/usage" in paths
    assert "/admin/users" in paths
    assert "/admin/users/{id:path}/allowed-models" in paths
    assert "/admin/users/{id:path}/budget" in paths
    assert "/admin/users/{id:path}" in paths
    assert "/admin/catalog" in paths
    assert "/admin/models" in paths
    assert "/admin/policies" in paths
    assert "/admin/health" in paths


def test_admin_api_with_initial_projects():
    """Verify AdminAPI accepts initial projects dict."""
    project = Project(project_id="p1", name="Test Project")
    api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        projects={"p1": project},
    )
    assert "p1" in api.projects
    assert api.projects["p1"].name == "Test Project"


def test_admin_api_with_initial_policies():
    """Verify AdminAPI accepts initial policies list."""
    policies = [{"name": "test-policy", "description": "A test", "policy_text": "permit;", "mode": "LOG_ONLY"}]
    api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        policies=policies,
    )
    assert len(api.policies) == 1
    assert api.policies[0]["name"] == "test-policy"


# ── Overview endpoint ────────────────────────────────────────────────

class TestOverview:
    """GET /admin/overview — Requirement 13.1"""

    def test_overview_returns_required_fields(self, client):
        resp = client.get("/admin/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_requests" in data
        assert "total_cost" in data
        assert "active_projects" in data
        assert "active_users" in data

    def test_overview_zeros_when_no_records(self, client):
        resp = client.get("/admin/overview")
        data = resp.json()
        assert data["total_requests"] == 0
        assert data["total_cost"] == 0
        assert data["active_projects"] == 0
        assert data["active_users"] == 0

    def test_overview_correct_counts_after_usage(self, admin_api, client):
        records = [
            _make_usage_record(project_id="p1", user_id="u1", cost=0.01),
            _make_usage_record(project_id="p1", user_id="u2", cost=0.02),
            _make_usage_record(project_id="p2", user_id="u1", cost=0.03),
        ]
        for r in records:
            asyncio.run(
                admin_api.cost_tracker.record_usage(r)
            )

        resp = client.get("/admin/overview")
        data = resp.json()
        assert data["total_requests"] == 3
        assert data["total_cost"] == pytest.approx(0.06)
        assert data["active_projects"] == 2  # p1, p2
        assert data["active_users"] == 2     # u1, u2


# ── Project CRUD ─────────────────────────────────────────────────────

class TestProjectCRUD:
    """POST/GET/PUT /admin/projects — Requirements 13.2, 13.5"""

    def test_create_project(self, client):
        resp = client.post("/admin/projects", json={"name": "My Project"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Project"
        assert data["status"] == "created"
        assert "project_id" in data

    def test_create_project_with_explicit_id(self, client):
        resp = client.post(
            "/admin/projects",
            json={"project_id": "custom-id", "name": "Custom"},
        )
        assert resp.status_code == 201
        assert resp.json()["project_id"] == "custom-id"

    def test_create_project_without_name_returns_400(self, client):
        resp = client.post("/admin/projects", json={"budget_limit": 100})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"]["message"].lower()

    def test_create_project_with_full_config(self, client):
        resp = client.post("/admin/projects", json={
            "name": "Full Config",
            "budget_limit": 500.0,
            "alert_threshold": 400.0,
            "allowed_models": ["gpt-4"],
            "cache_enabled": True,
            "cache_ttl_seconds": 600,
            "log_level": "DEBUG",
            "guardrail_rules": [
                {"name": "no-pii", "rule_type": "regex_match", "pattern": "\\d{3}-\\d{2}-\\d{4}", "action": "block"}
            ],
        })
        assert resp.status_code == 201

    def test_list_projects_empty(self, client):
        resp = client.get("/admin/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_projects_after_create(self, client):
        client.post("/admin/projects", json={"project_id": "p1", "name": "Alpha"})
        client.post("/admin/projects", json={"project_id": "p2", "name": "Beta"})

        resp = client.get("/admin/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        ids = {p["project_id"] for p in data}
        assert ids == {"p1", "p2"}

    def test_list_projects_includes_spend_and_budget(self, admin_api, client):
        client.post("/admin/projects", json={
            "project_id": "p1",
            "name": "Alpha",
            "budget_limit": 100.0,
        })
        # Record some usage for p1
        asyncio.run(
            admin_api.cost_tracker.record_usage(
                _make_usage_record(project_id="p1", cost=25.0)
            )
        )

        resp = client.get("/admin/projects")
        data = resp.json()
        assert len(data) == 1
        proj = data[0]
        assert proj["current_spend"] == pytest.approx(25.0)
        assert proj["budget_limit"] == 100.0
        assert proj["budget_utilization_pct"] == pytest.approx(25.0)
        assert proj["request_count"] == 1

    def test_get_project_detail(self, admin_api, client):
        client.post("/admin/projects", json={"project_id": "p1", "name": "Alpha"})
        asyncio.run(
            admin_api.cost_tracker.record_usage(
                _make_usage_record(project_id="p1", user_id="u1", provider="openai", model="gpt-4", cost=0.01)
            )
        )

        resp = client.get("/admin/projects/p1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "p1"
        assert data["name"] == "Alpha"
        assert "u1" in data["users"]
        assert "gpt-4" in data["usage_by_model"]
        assert "openai" in data["usage_by_provider"]

    def test_get_project_unknown_returns_404(self, client):
        resp = client.get("/admin/projects/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]["message"].lower()

    def test_update_project(self, client):
        client.post("/admin/projects", json={"project_id": "p1", "name": "Alpha"})

        resp = client.put("/admin/projects/p1", json={
            "name": "Alpha Updated",
            "budget_limit": 200.0,
            "cache_enabled": True,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify the update took effect
        detail = client.get("/admin/projects/p1").json()
        assert detail["name"] == "Alpha Updated"
        assert detail["budget_limit"] == 200.0
        assert detail["cache_enabled"] is True

    def test_update_project_unknown_returns_404(self, client):
        resp = client.put("/admin/projects/nonexistent", json={"name": "X"})
        assert resp.status_code == 404

    def test_update_project_guardrail_rules(self, client):
        client.post("/admin/projects", json={"project_id": "p1", "name": "Alpha"})

        resp = client.put("/admin/projects/p1", json={
            "guardrail_rules": [
                {"name": "block-bad", "rule_type": "keyword_block", "pattern": "bad", "action": "block"}
            ],
        })
        assert resp.status_code == 200

        detail = client.get("/admin/projects/p1").json()
        assert len(detail["guardrail_rules"]) == 1
        assert detail["guardrail_rules"][0]["name"] == "block-bad"


# ── Usage query ──────────────────────────────────────────────────────

class TestUsageQuery:
    """GET /admin/usage — Requirement 13.3"""

    def test_usage_no_records_returns_zeros(self, client):
        resp = client.get("/admin/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 0
        assert data["total_tokens"] == 0
        assert data["total_cost"] == 0
        assert data["breakdown"] == []

    def test_usage_returns_aggregated_data(self, admin_api, client):
        records = [
            _make_usage_record(provider="openai", model="gpt-4", cost=0.01),
            _make_usage_record(provider="anthropic", model="claude-3", cost=0.02),
        ]
        for r in records:
            asyncio.run(
                admin_api.cost_tracker.record_usage(r)
            )

        resp = client.get("/admin/usage")
        data = resp.json()
        assert data["total_requests"] == 2
        assert data["total_cost"] == pytest.approx(0.03)
        assert len(data["breakdown"]) > 0

    def test_usage_with_provider_filter(self, admin_api, client):
        records = [
            _make_usage_record(provider="openai", cost=0.01),
            _make_usage_record(provider="anthropic", cost=0.02),
            _make_usage_record(provider="openai", cost=0.03),
        ]
        for r in records:
            asyncio.run(
                admin_api.cost_tracker.record_usage(r)
            )

        resp = client.get("/admin/usage?provider=openai")
        data = resp.json()
        assert data["total_requests"] == 2
        assert data["total_cost"] == pytest.approx(0.04)

    def test_usage_with_model_filter(self, admin_api, client):
        records = [
            _make_usage_record(model="gpt-4", cost=0.01),
            _make_usage_record(model="claude-3", cost=0.02),
        ]
        for r in records:
            asyncio.run(
                admin_api.cost_tracker.record_usage(r)
            )

        resp = client.get("/admin/usage?model=gpt-4")
        data = resp.json()
        assert data["total_requests"] == 1
        assert data["total_cost"] == pytest.approx(0.01)

    def test_usage_with_project_filter(self, admin_api, client):
        records = [
            _make_usage_record(project_id="p1", cost=0.01),
            _make_usage_record(project_id="p2", cost=0.02),
        ]
        for r in records:
            asyncio.run(
                admin_api.cost_tracker.record_usage(r)
            )

        resp = client.get("/admin/usage?project_id=p1")
        data = resp.json()
        assert data["total_requests"] == 1
        assert data["total_cost"] == pytest.approx(0.01)

    def test_usage_with_combined_filters(self, admin_api, client):
        records = [
            _make_usage_record(project_id="p1", provider="openai", cost=0.01),
            _make_usage_record(project_id="p1", provider="anthropic", cost=0.02),
            _make_usage_record(project_id="p2", provider="openai", cost=0.03),
        ]
        for r in records:
            asyncio.run(
                admin_api.cost_tracker.record_usage(r)
            )

        resp = client.get("/admin/usage?project_id=p1&provider=openai")
        data = resp.json()
        assert data["total_requests"] == 1
        assert data["total_cost"] == pytest.approx(0.01)


# ── Policy management ────────────────────────────────────────────────

class TestPolicyManagement:
    """GET/POST /admin/policies — Requirement 13.5"""

    def test_list_policies_empty(self, client):
        resp = client.get("/admin/policies")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_policy(self, client):
        resp = client.post("/admin/policies", json={
            "name": "allow-gpt4",
            "description": "Allow GPT-4 access",
            "policy_text": 'permit(principal, action, resource) when { resource.model == "gpt-4" };',
            "mode": "ENFORCE",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "allow-gpt4"
        assert data["status"] == "created"

    def test_create_policy_without_name_returns_400(self, client):
        resp = client.post("/admin/policies", json={
            "description": "Missing name",
            "policy_text": "permit;",
        })
        assert resp.status_code == 400
        assert "name" in resp.json()["error"]["message"].lower()

    def test_create_policy_with_existing_name_updates(self, client):
        client.post("/admin/policies", json={
            "name": "my-policy",
            "policy_text": "permit;",
            "mode": "LOG_ONLY",
        })
        # Update with same name
        resp = client.post("/admin/policies", json={
            "name": "my-policy",
            "policy_text": "forbid;",
            "mode": "ENFORCE",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify only one policy exists with updated text
        policies = client.get("/admin/policies").json()
        assert len(policies) == 1
        assert policies[0]["policy_text"] == "forbid;"
        assert policies[0]["mode"] == "ENFORCE"

    def test_list_policies_returns_created_policies(self, client):
        client.post("/admin/policies", json={"name": "pol-1", "policy_text": "permit;"})
        client.post("/admin/policies", json={"name": "pol-2", "policy_text": "forbid;"})

        resp = client.get("/admin/policies")
        data = resp.json()
        assert len(data) == 2
        names = {p["name"] for p in data}
        assert names == {"pol-1", "pol-2"}

    def test_create_policy_defaults(self, client):
        """Verify default values for optional fields."""
        client.post("/admin/policies", json={"name": "minimal"})
        policies = client.get("/admin/policies").json()
        assert len(policies) == 1
        p = policies[0]
        assert p["description"] == ""
        assert p["policy_text"] == ""
        assert p["mode"] == "LOG_ONLY"


# ── Health endpoint ──────────────────────────────────────────────────

class TestHealth:
    """GET /admin/health — Requirement 13.7"""

    def test_health_returns_ok(self, client):
        resp = client.get("/admin/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["runtime"] == "running"

    def test_health_includes_provider_status(self, client):
        resp = client.get("/admin/health")
        data = resp.json()
        assert "providers" in data
        # The model_registry fixture has openai configured
        assert data["providers"]["openai"] == "healthy"

    def test_health_shows_unhealthy_provider(self, admin_api, client):
        admin_api.health_tracker.mark_unhealthy("openai", cooldown_seconds=300)

        resp = client.get("/admin/health")
        data = resp.json()
        assert data["providers"]["openai"] == "unhealthy"


# ── Dashboard endpoint ───────────────────────────────────────────────

class TestDashboard:
    """GET /admin/dashboard — Requirement 13.1"""

    def test_dashboard_returns_html(self, client):
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_dashboard_contains_react_app(self, client):
        resp = client.get("/admin/dashboard")
        body = resp.text
        assert "AxonLLM" in body
        assert "react" in body.lower()
        assert '<div id="root">' in body

    def test_the_ribbon_links_to_the_architecture_page(self, client):
        """The pill in the top ribbon, shown on Overview and every other view.

        The href must be absolute. The dashboard is served from
        /admin/dashboard, so a relative "architecture.html" resolves to
        /admin/architecture.html — which is not a route, and the pill would
        404 while looking perfectly fine in the markup.
        """
        body = client.get("/admin/dashboard").text
        assert 'className="topbar-pill"' in body
        assert 'href="/architecture.html"' in body
        assert 'href="architecture.html"' not in body

    def test_the_ribbon_pill_target_is_actually_served(self, site_client_for_dashboard):
        """The pair, not just the href — same reason as the landing page's."""
        assert site_client_for_dashboard.get("/architecture.html").status_code == 200

    @pytest.fixture
    def site_client_for_dashboard(self, admin_api):
        from src.gateway.admin.routes import create_site_routes

        app = Starlette(
            routes=create_admin_routes(admin_api) + create_site_routes(admin_api)
        )
        return TestClient(app, raise_server_exceptions=False)


# ── Virtual Model CRUD ───────────────────────────────────────────────

class TestVirtualModelCRUD:
    """POST/PUT/DELETE /admin/models — Requirements 6, 7, 8, 9"""

    def test_create_model(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.post("/admin/models", json={
            "name": "new-model",
            "description": "A new model",
            "routing_strategy": "round-robin",
            "providers": [
                {"provider": "openai", "model_id": "gpt-4-turbo", "weight": 1.0, "fallback_order": 0}
            ],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "new-model"
        assert data["status"] == "created"
        assert "new-model" in admin_api.model_registry.models

    def test_create_model_persists_to_yaml(self, client, admin_api, tmp_path):
        config_path = tmp_path / "models.yaml"
        admin_api._config_path = str(config_path)
        client.post("/admin/models", json={
            "name": "persisted-model",
            "description": "Persisted",
            "routing_strategy": "weighted",
            "providers": [
                {"provider": "anthropic", "model_id": "claude-3", "weight": 1.0, "fallback_order": 0}
            ],
        })
        assert config_path.exists()
        content = config_path.read_text()
        assert "persisted-model" in content

    def test_create_model_without_name_returns_400(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.post("/admin/models", json={
            "description": "No name",
            "providers": [{"provider": "openai", "model_id": "gpt-4"}],
        })
        assert resp.status_code == 400

    def test_create_model_invalid_provider_returns_400(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.post("/admin/models", json={
            "name": "bad-model",
            "description": "Bad provider",
            "providers": [{"provider": "invalid_provider", "model_id": "x"}],
        })
        assert resp.status_code == 400
        assert "errors" in resp.json()

    def test_create_model_duplicate_name_returns_400(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        # gpt-4 already exists in the fixture
        resp = client.post("/admin/models", json={
            "name": "gpt-4",
            "description": "Duplicate",
            "providers": [{"provider": "openai", "model_id": "gpt-4-turbo"}],
        })
        assert resp.status_code == 400

    def test_create_model_with_capabilities(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.post("/admin/models", json={
            "name": "capable-model",
            "description": "Has capabilities",
            "routing_strategy": "round-robin",
            "providers": [{"provider": "openai", "model_id": "gpt-4-turbo"}],
            "capabilities": ["chat", "vision"],
        })
        assert resp.status_code == 201
        model = admin_api.model_registry.models["capable-model"]
        assert model.capabilities == ["chat", "vision"]

    def test_update_model_full_edit(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.put("/admin/models/gpt-4", json={
            "description": "Updated GPT-4",
            "routing_strategy": "weighted",
            "providers": [
                {"provider": "openai", "model_id": "gpt-4-turbo", "weight": 0.5, "fallback_order": 0},
                {"provider": "anthropic", "model_id": "claude-3", "weight": 0.5, "fallback_order": 1},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "gpt-4"
        assert data["status"] == "updated"

        model = admin_api.model_registry.models["gpt-4"]
        assert model.description == "Updated GPT-4"
        assert model.routing_strategy.value == "weighted"
        assert len(model.providers) == 2

    def test_update_model_invalid_config_returns_400(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.put("/admin/models/gpt-4", json={
            "providers": [{"provider": "invalid_provider", "model_id": "x"}],
        })
        assert resp.status_code == 400
        assert "errors" in resp.json()

    def test_update_model_not_found_returns_404(self, client):
        resp = client.put("/admin/models/nonexistent", json={"description": "X"})
        assert resp.status_code == 404

    def test_delete_model(self, client, admin_api, tmp_path):
        admin_api._config_path = str(tmp_path / "models.yaml")
        resp = client.delete("/admin/models/gpt-4")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "gpt-4"
        assert data["status"] == "deleted"
        assert "gpt-4" not in admin_api.model_registry.models

    def test_delete_model_persists(self, client, admin_api, tmp_path):
        config_path = tmp_path / "models.yaml"
        admin_api._config_path = str(config_path)
        client.delete("/admin/models/gpt-4")
        assert config_path.exists()
        content = config_path.read_text()
        assert "gpt-4" not in content

    def test_delete_model_not_found_returns_404(self, client):
        resp = client.delete("/admin/models/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]["message"].lower()


# ── Usage export (task #9) ──────────────────────────────────────────

class TestUsageExport:
    def _seed(self, cost_tracker):
        cost_tracker._records = [
            _make_usage_record(project_id="proj-a", user_id="alice", provider="openai", model="gpt-4"),
            _make_usage_record(project_id="proj-a", user_id="bob", provider="openai", model="gpt-4"),
            _make_usage_record(project_id="proj-b", user_id="carol", provider="anthropic", model="claude-3"),
        ]

    def test_csv_records_is_attachment_with_header(self, client, cost_tracker):
        self._seed(cost_tracker)
        r = client.get("/admin/usage/export")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert 'attachment; filename="axonllm-usage-records.csv"' in r.headers["content-disposition"]
        lines = r.text.strip().splitlines()
        assert lines[0].startswith("request_id,timestamp,project_id,user_id,provider,model")
        assert len(lines) - 1 == 3  # header + 3 rows

    def test_csv_breakdown(self, client, cost_tracker):
        self._seed(cost_tracker)
        r = client.get("/admin/usage/export?level=breakdown")
        assert r.status_code == 200
        assert r.text.strip().splitlines()[0] == "group_by,group_key,requests,tokens,cost"

    def test_json_records(self, client, cost_tracker):
        self._seed(cost_tracker)
        r = client.get("/admin/usage/export?format=json&level=records")
        assert r.status_code == 200
        body = r.json()
        assert body["level"] == "records"
        assert len(body["rows"]) == 3
        assert {"request_id", "project_id", "user_id", "cost", "total_tokens"} <= set(body["rows"][0])

    def test_json_breakdown(self, client, cost_tracker):
        self._seed(cost_tracker)
        r = client.get("/admin/usage/export?format=json&level=breakdown")
        assert r.status_code == 200
        assert all({"group_by", "group_key", "requests", "cost"} <= set(row) for row in r.json()["rows"])

    def test_filter_by_project(self, client, cost_tracker):
        self._seed(cost_tracker)
        r = client.get("/admin/usage/export?format=json&project_id=proj-a")
        assert r.status_code == 200
        assert len(r.json()["rows"]) == 2  # only proj-a

    def test_invalid_format_400(self, client, cost_tracker):
        self._seed(cost_tracker)
        assert client.get("/admin/usage/export?format=xml").status_code == 400

    def test_invalid_level_400(self, client, cost_tracker):
        self._seed(cost_tracker)
        assert client.get("/admin/usage/export?level=bogus").status_code == 400

    def test_empty_export_is_header_only(self, client, cost_tracker):
        cost_tracker._records = []
        r = client.get("/admin/usage/export")
        assert r.status_code == 200
        assert len(r.text.strip().splitlines()) == 1  # header only


# ── Static asset serving (task #10: vendored JS for air-gap) ────────

class TestStaticAssets:
    def test_serves_vendored_js(self, client):
        r = client.get("/admin/static/vendor/react.production.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_missing_asset_404(self, client):
        assert client.get("/admin/static/vendor/nope.js").status_code == 404

    def test_traversal_is_blocked(self, client):
        # Escape attempt must not read files outside the static dir.
        assert client.get("/admin/static/..%2f..%2froutes.py").status_code == 404
        assert client.get("/admin/static/../routes.py").status_code == 404


# ── Landing page at the gateway root ────────────────────────────────

class TestLandingPage:
    """GET / — the marketing page, served from the same origin as the dashboard.

    Same-origin matters: the page's Dashboard buttons are relative links, so
    they resolve against whatever host and port the gateway runs on. Serving
    the page from a separate static host would require hardcoding a gateway
    URL into it, which is wrong for every self-hosted deployment.
    """

    def test_root_serves_the_landing_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "The Neural Control Plane" in r.text

    def test_the_dashboard_links_are_relative(self, client):
        """A hardcoded host here would break every deployment but localhost."""
        r = client.get("/")
        assert 'href="/admin/dashboard"' in r.text
        assert "localhost:8000/admin/dashboard" not in r.text

    def test_the_link_target_actually_resolves(self, client):
        """The pair of routes, not just the href.

        A relative link is only correct if the path it names is served by the
        same app — this fails if the landing page ships without the dashboard
        route, which is exactly the mistake the relative link invites.
        """
        assert client.get(client.get("/").url.path).status_code == 200
        assert client.get("/admin/dashboard").status_code == 200

    def test_a_missing_site_dir_404s_rather_than_raising(self, monkeypatch):
        """site/ is outside the package and not in package-data.

        A pip-installed gateway has no site/index.html. Reading it
        unconditionally would turn the root path into a 500 on a deployment
        that is otherwise completely healthy.
        """
        import pathlib

        from src.gateway.admin import routes as routes_mod

        monkeypatch.setattr(
            routes_mod, "_PROJECT_ROOT", pathlib.Path("/nonexistent-axon-root")
        )
        api = AdminAPI(
            cost_tracker=CostTracker(pricing_config={}),
            model_registry=ModelRegistry(),
            health_tracker=ProviderHealthTracker(),
        )
        with TestClient(_make_app(api)) as c:
            r = c.get("/")
            assert r.status_code == 404
            # Still points somewhere useful — the gateway is up, after all.
            assert "/admin/dashboard" in r.text

    def test_the_hero_leads_with_the_logo_lockup(self, client):
        """Mark, wordmark and tagline, above the headline.

        The mark used to appear only at 30px in the nav, so the page opened with
        text and nothing identified the product visually.
        """
        html = client.get("/").text
        assert 'class="hero-lockup"' in html
        assert 'class="hero-wordmark"' in html
        assert 'class="hero-tagline"' in html
        # Above the headline, or it is not a lockup, it is a footnote.
        assert html.index("hero-lockup") < html.index("<h1>")

    def test_the_lockup_is_a_column_so_the_badge_clears_it(self, client):
        """The lockup must be `flex`, not `inline-flex`.

        As inline-flex it shared a line with the `.badge` pill that follows it,
        so the badge rendered beside the tagline instead of below the lockup.
        """
        import re

        html = client.get("/").text
        rule = re.search(r"\.hero-lockup \{([^}]*)\}", html).group(1)
        assert re.search(r"display:\s*flex\b", rule)
        assert "inline-flex" not in rule
        assert re.search(r"flex-direction:\s*column", rule)

    def _hero_sizes(self, client) -> dict[str, float]:
        """The four hero type sizes, in rem.

        Parsed from the desktop rules — the first match for each — so the
        ordering tests below all measure the same breakpoint. The @media block
        restates three of these and would otherwise be picked up instead.
        """
        import re

        html = client.get("/").text

        def rem(pattern: str) -> float:
            m = re.search(pattern, html)
            assert m, f"no match for {pattern!r}"
            return float(m.group(1))

        return {
            "wordmark": rem(r"\.hero-wordmark \{[^}]*font-size:\s*([\d.]+)rem"),
            "tagline": rem(r"\.hero-tagline \{[^}]*font-size:\s*([\d.]+)rem"),
            "h1": rem(r"\n        h1 \{[^}]*font-size:\s*([\d.]+)rem"),
            "sub": rem(r"\.hero-sub \{[^}]*font-size:\s*([\d.]+)rem"),
        }

    def test_the_wordmark_is_the_largest_thing_in_the_hero(self, client):
        """The lockup is the hero, and the headline is its subhead.

        This was wrong twice in the same way and the earlier version of this
        test enforced the wrong thing: it asserted `wordmark < h1`, on the
        reasoning that the headline is the page's primary line. But the lockup
        also came first in the document, so the brand led by position while the
        headline led by size and the page had two competing heroes -- which is
        what actually read as unprofessional.

        One of them has to win outright. On a product landing page it is the
        brand, so the ordering is asserted rather than left to taste.
        """
        s = self._hero_sizes(client)
        assert s["wordmark"] > s["h1"], (
            f"wordmark {s['wordmark']}rem does not lead the h1 at {s['h1']}rem"
        )
        # A clear step, not a hair's difference — 4.5 vs 2.6 read as two heroes
        # and so would 2.7 vs 2.6.
        assert s["h1"] / s["wordmark"] <= 0.8, (
            f"h1 is {s['h1'] / s['wordmark']:.0%} of the wordmark; too close to read as a subhead"
        )

    def test_the_hero_type_scale_descends(self, client):
        """wordmark > h1 > sub > tagline, strictly.

        Each pair was inverted at some point: the h1 outgrew the wordmark, and
        the tagline reached the body copy when the lockup was scaled up (0.28x
        of a 4.4rem wordmark is 1.24rem, indistinguishable from the 1.25rem
        sub). Asserted as one chain so raising any one size cannot quietly
        overtake its neighbour.
        """
        s = self._hero_sizes(client)
        order = ["wordmark", "h1", "sub", "tagline"]
        scale = " > ".join(f"{k} {s[k]}rem" for k in order)

        # Each step at least 15% smaller than the one above. Strict `>` is not
        # enough: a 1.24rem tagline under a 1.25rem sub is descending by the
        # letter and identical to the eye, which is a flat scale, not a
        # hierarchy.
        for bigger, smaller in zip(order, order[1:]):
            assert s[smaller] <= 0.85 * s[bigger], (
                f"{smaller} ({s[smaller]}rem) is not a clear step below "
                f"{bigger} ({s[bigger]}rem) — {scale}"
            )

    def test_the_tagline_captions_the_wordmark(self, client):
        """The ratio is Ostiari's own logo.svg: a 7.5-unit tagline under a
        26-unit wordmark, i.e. ~0.29x.

        Too small and the tracked-out tagline is far narrower than the row above
        it; too large and it stops reading as a caption.
        """
        s = self._hero_sizes(client)
        ratio = s["tagline"] / s["wordmark"]
        assert 0.25 <= ratio <= 0.33, f"tagline/wordmark is {ratio:.2f}, want ~0.29"

    def test_the_hero_sizes_are_stepped_not_fluid(self, client):
        """A clamped headline against a fixed wordmark is what let the ratio
        invert.

        Two type scales on different mechanisms drift apart between
        breakpoints, so `wordmark > h1` can hold at 1440px and cross over at
        800px where nobody is looking. Both are stepped at the same breakpoint
        instead, which is also what makes the ordering tests above meaningful at
        more than one width.
        """
        import re

        html = client.get("/").text
        for sel in (r"\n        h1", r"\.hero-wordmark", r"\.hero-tagline"):
            rule = re.search(rf"{sel} \{{([^}}]*)\}}", html).group(1)
            assert "clamp(" not in rule and "vw" not in rule, (
                f"{sel} is fluid; it must step with the lockup, not against it"
            )

    def test_the_narrow_viewport_keeps_the_same_ordering(self, client):
        """The @media block restates the whole set, so it can invert on its own.

        Scaling the lockup down without scaling the headline is the same bug at
        a different width — and it is easy to miss, because the desktop tests
        above all pass while a phone shows a headline larger than the brand.
        """
        import re

        html = client.get("/").text
        media = re.search(
            r"@media \(max-width: 768px\) \{(.*?)\n        \}", html, re.S
        ).group(1)

        def rem(sel: str) -> float:
            m = re.search(rf"{sel} \{{[^}}]*font-size:\s*([\d.]+)rem", media)
            assert m, f"the mobile block does not restate {sel}"
            return float(m.group(1))

        word, h1, sub, tag = (
            rem(r"\.hero-wordmark"), rem(r"h1"),
            rem(r"\.hero-sub"), rem(r"\.hero-tagline"),
        )
        assert word > h1 > sub > tag, (
            f"mobile scale is not descending: wordmark {word} > h1 {h1} "
            f"> sub {sub} > tagline {tag}"
        )
        assert 0.25 <= tag / word <= 0.33, f"mobile tagline/wordmark is {tag / word:.2f}"

    def test_the_tagline_fits_within_the_lockup_row(self, client):
        """The failure both earlier attempts had, in opposite directions.

        The tagline is 24 characters of tracked-out uppercase under a
        7-character wordmark, so its rendered width is set by the tracking, not
        by the font size — and it is easy to land far wider or far narrower than
        the mark-plus-wordmark row above it. Either way the lockup stops reading
        as one block.

        The advance ratios below are calibrated against a headless-Chrome
        measurement of this page with Inter actually loaded — the earlier
        estimated figures (3.67x / 15.63x) were off by ~20%, enough that this
        test read 94% while the browser laid the line out at 100% of the row.
        Still an estimate, so the bound stays loose: it catches "obviously
        wrong", which is what shipped twice, not sub-pixel drift.
        """
        import re

        html = client.get("/").text

        def num(pattern: str) -> float:
            return float(re.search(pattern, html).group(1))

        mark = num(r"\.hero-lockup svg \{[^}]*width:\s*(\d+)px")
        gap = num(r"\.hero-lockup-row \{[^}]*gap:\s*(\d+)px")
        word_rem = num(r"\.hero-wordmark \{[^}]*font-size:\s*([\d.]+)rem")
        tag_rem = num(r"\.hero-tagline \{[^}]*font-size:\s*([\d.]+)rem")
        track = num(r"\.hero-tagline \{[^}]*letter-spacing:\s*([\d.]+)em")

        # "AxonLLM" in Inter 800 advances ~4.46x its font size; the tagline
        # ~19.59x at 0.18em tracking, moving by one em per character beyond that.
        # Both from Chrome, at 1440px with the webfont loaded.
        row = mark + gap + word_rem * 16 * 4.46
        tag_px = tag_rem * 16 * (19.59 - len("The Neural Control Plane") * (0.18 - track))

        ratio = tag_px / row
        assert 0.80 <= ratio <= 1.02, (
            f"tagline is {ratio:.0%} of the lockup row "
            f"({tag_px:.0f}px vs {row:.0f}px) — Ostiari's logo.svg sits at 94%"
        )

    def test_the_tagline_does_not_outweigh_the_body_copy(self, client):
        """A caption heavier than the sentence it captions inverts the hierarchy.

        The tagline is the smallest text in the hero, so it must not also be the
        boldest thing under the wordmark.
        """
        import re

        html = client.get("/").text
        tag = re.search(r"\.hero-tagline \{([^}]*)\}", html).group(1)
        weight = int(re.search(r"font-weight:\s*(\d+)", tag).group(1))
        assert weight <= 600, f"tagline at {weight} competes with the body copy"
        # stone-500, whose 4.59 on stone-50 still clears the 4.5 AA bar.
        assert "var(--text-dim)" in tag

    def test_the_two_marks_do_not_share_a_gradient_id(self, client):
        """Both the nav and the hero inline the mark, each with its own <defs>.

        Two <linearGradient> elements sharing an id in one document is a
        collision the browser resolves silently: both fills take whichever came
        first, so one mark renders in the wrong gradient and nothing errors.
        """
        import re

        html = client.get("/").text
        ids = re.findall(r'<linearGradient id="([^"]+)"', html)
        assert len(ids) == len(set(ids)), f"duplicate gradient ids: {ids}"
        # Every url(#...) reference resolves to a gradient that exists.
        assert set(re.findall(r"url\(#([^)]+)\)", html)) <= set(ids)


class TestGuidedTour:
    """The dashboard's narrated walkthrough: static/tour/ plus its player.

    The narration is data, not code — a JSON script and one Polly MP3 per
    scene, served out of static/ and fetched by the SPA at runtime. That split
    is what these cover: the assets have to be reachable and correctly typed,
    and the script has to keep agreeing with the shell it drives.
    """

    @pytest.fixture
    def tour(self):
        """The narration script, read from the file the dashboard fetches."""
        import json
        import pathlib

        from src.gateway.admin import routes as routes_mod

        path = routes_mod._STATIC_DIR / "tour" / "tour-narration.json"
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

    def test_the_narration_script_is_served_as_json(self, client):
        r = client.get("/admin/static/tour/tour-narration.json")
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]
        assert len(r.json()["tracks"]) > 0

    def test_every_scene_has_its_audio(self, client, tour):
        """The player builds the URL from the scene id, so a rename that misses
        the MP3 leaves a scene that advances in silence."""
        for track in tour["tracks"]:
            r = client.get(f"/admin/static/tour/{track['id']}.mp3")
            assert r.status_code == 200, f"{track['id']}.mp3 missing"
            assert r.headers["content-type"] == "audio/mpeg"
            assert len(r.content) > 1000, f"{track['id']}.mp3 is suspiciously small"

    def test_audio_is_not_served_as_octet_stream(self, client, tour):
        """The regression this guards: before .mp3 was in the media-type map the
        default was application/octet-stream, which a browser declines to play.
        Every asset still resolved with a 200, so nothing else would catch it."""
        r = client.get(f"/admin/static/tour/{tour['tracks'][0]['id']}.mp3")
        assert "octet-stream" not in r.headers["content-type"]

    def test_audio_is_range_requestable(self, client, tour):
        """Without ranges a browser reports the audio as unseekable, so the
        tour's scene dots would move the progress bar and not the sound."""
        name = f"/admin/static/tour/{tour['tracks'][0]['id']}.mp3"
        full = client.get(name)
        assert full.headers["accept-ranges"] == "bytes"

        part = client.get(name, headers={"Range": "bytes=10-109"})
        assert part.status_code == 206
        assert part.headers["content-range"] == f"bytes 10-109/{len(full.content)}"
        assert part.content == full.content[10:110]

    def test_every_scene_names_a_view_the_shell_can_render(self, client, tour):
        """The coupling that makes this tour maintainable, asserted.

        Each scene's ``view`` is passed to the shell's navigate(), which is a
        switch over view keys. A typo or a renamed page would send the tour to
        the default view and narrate the wrong page — silently, since the
        default case renders Overview rather than failing.
        """
        import re

        html = client.get("/admin/dashboard").text
        views = set(re.findall(r"case '([a-z-]+)': content =", html))
        assert views, "could not find the view switch — this test needs updating"
        for track in tour["tracks"]:
            assert track["view"] in views, (
                f"scene {track['id']} narrates view {track['view']!r}, "
                f"which the shell does not render"
            )

    def test_scene_durations_are_measured(self, tour):
        """build_narration_audio.sh writes ffprobe's reading back into the JSON.
        A scene with no duration means the audio was never synthesized for it, and
        the player's progress bar would have no scale until metadata loaded."""
        for track in tour["tracks"]:
            assert track.get("duration", 0) > 0, f"{track['id']} has no measured duration"

    def test_scenes_carry_display_text_not_only_ssml(self, tour):
        """The card shows ``text``; Polly speaks ``ssml``. A scene with only the
        latter would render its <break> tags into the caption."""
        for track in tour["tracks"]:
            assert track["text"].strip()
            assert "<" not in track["text"], f"{track['id']} caption contains markup"
            assert track["ssml"].startswith("<speak>")

    def test_the_dashboard_offers_the_tour(self, client):
        r = client.get("/admin/dashboard")
        assert "Guided Demo" in r.text
        assert "function GuidedTour" in r.text

    def test_the_player_fetches_the_script_at_the_served_path(self, client):
        """The fetch URL is a string in the SPA and the route is in Python; only
        an assertion over both keeps them together."""
        html = client.get("/admin/dashboard").text
        assert "/admin/static/tour/tour-narration.json" in html
        assert client.get("/admin/static/tour/tour-narration.json").status_code == 200

    def test_tour_assets_are_still_confined_to_the_static_dir(self, client):
        """static/tour/ added a second level under static/, so the traversal
        check is worth re-asserting from inside it."""
        assert client.get("/admin/static/tour/../../../config/providers.yaml").status_code == 404
        assert client.get("/admin/static/tour/..%2f..%2f..%2fconfig%2fproviders.yaml").status_code == 404


class TestArchitecturePage:
    """GET /architecture.html — the interactive architecture page.

    The page lives in site/ next to index.html and is served two ways: from the
    S3 bucket on the deployed site, and from this gateway at the same relative
    path so a self-hosted deployment gets the same nav. Both need the assets it
    fetches to be reachable, which is what most of these cover.
    """

    @pytest.fixture
    def site_client(self, admin_api):
        """A client with the site catch-all mounted, as build_app assembles it."""
        from src.gateway.admin.routes import create_site_routes

        app = Starlette(routes=create_admin_routes(admin_api) + create_site_routes(admin_api))
        return TestClient(app, raise_server_exceptions=False)

    def test_the_page_is_served(self, site_client):
        r = site_client.get("/architecture.html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Architecture" in r.text

    def test_the_landing_page_links_to_it(self, client):
        """An unlinked page is one nobody finds.

        The link is relative for the same reason the dashboard links are: it has
        to resolve against the bucket on S3 and against this gateway when
        self-hosted, with no host hardcoded either way.
        """
        html = client.get("/").text
        assert 'href="architecture.html"' in html
        assert "axonllm.com/architecture" not in html

    def test_every_svg_the_page_fetches_is_served(self, site_client):
        """The panels lazy-fetch by data-src, so a rename fails at runtime only.

        Nothing builds site/ — CDK uploads it verbatim — so a diagram exported
        under a new name and not committed would leave a blank tab with a
        console 404 and no other symptom.
        """
        import re

        html = site_client.get("/architecture.html").text
        srcs = re.findall(r'data-src="([^"]+)"', html)
        assert len(srcs) == 3, f"expected three diagram panels, found {srcs}"
        for src in srcs:
            r = site_client.get("/" + src)
            assert r.status_code == 200, f"{src} is referenced but not served"
            assert r.headers["content-type"] == "image/svg+xml"
            assert "<svg" in r.text

    def test_the_download_link_resolves(self, site_client):
        """The editable original, offered next to the diagram."""
        html = site_client.get("/architecture.html").text
        assert 'href="architecture.drawio"' in html
        r = site_client.get("/architecture.drawio")
        assert r.status_code == 200
        assert "<mxfile" in r.text

    def test_the_three_svgs_are_distinct_pages(self, site_client):
        """A -p off-by-one exports the same page three times.

        drawio's -p is 1-based and -p 0 silently means page 1, so the obvious
        0,1,2 loop yields two identical files. Identical bytes here means the
        export was wrong, and every tab would show the same diagram.
        """
        import re

        html = site_client.get("/architecture.html").text
        bodies = [site_client.get("/" + s).text for s in re.findall(r'data-src="([^"]+)"', html)]
        assert len({hash(b) for b in bodies}) == 3, "two panels serve the same SVG"

    def test_the_step_numbers_match_the_source(self, site_client):
        """The prose claims these are the source's own numbers.

        agent.py numbers its pipeline with fractional inserts (2.5, 9.5, 11.6)
        added after the original sixteen. The page states it keeps that
        numbering, so a step renumbered or dropped here makes the page lie about
        the code — which is the one thing an architecture page cannot do.
        """
        import pathlib
        import re

        html = site_client.get("/architecture.html").text
        steps = re.findall(r'<li data-step="([^"]+)"', html)
        assert steps, "the flow list carries no step numbers"

        agent_src = (
            pathlib.Path(__file__).resolve().parents[2] / "src" / "gateway" / "agent.py"
        ).read_text(encoding="utf-8")
        source_steps = set(re.findall(r"^\s+# (\d+(?:\.\d+)?)\. ", agent_src, re.M))

        # Ranges on the page ("4–5") collapse two adjacent source steps into one
        # row, so split them back out before comparing.
        page_steps = {n for s in steps for n in s.split("–")}
        assert page_steps <= source_steps, (
            f"page lists steps absent from agent.py: {sorted(page_steps - source_steps)}"
        )
        assert source_steps <= page_steps, (
            f"agent.py has steps the page omits: {sorted(source_steps - page_steps)}"
        )

    def test_the_step_badges_are_not_a_css_counter(self, site_client):
        """A counter would renumber 1..18 and contradict the prose above it."""
        html = site_client.get("/architecture.html").text
        assert "content: attr(data-step)" in html
        assert "counter(step)" not in html

    # ── Narration ────────────────────────────────────────────────────

    def test_the_narration_manifest_is_served(self, site_client):
        """The page fetches it to decide whether to show a player at all.

        A 404 here is a silent feature removal: the player stays hidden, the
        diagrams still work, and nobody notices the walkthrough is gone.
        """
        r = site_client.get("/narration/architecture-narration.json")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"
        assert r.json()["voice"] == "Matthew"

    def test_every_narration_track_has_its_audio(self, site_client):
        """Track ids name the MP3s by convention, so a rename fails at runtime.

        The player builds "narration/<id>.mp3" from the manifest rather than
        reading a path out of it. That keeps the JSON small and makes the
        convention worth asserting: an id edited without renaming the file
        leaves a play button that errors on click.
        """
        manifest = site_client.get("/narration/architecture-narration.json").json()
        tracks = manifest["tracks"]
        assert len(tracks) == 3, f"expected one track per diagram, got {len(tracks)}"
        for track in tracks:
            r = site_client.get(f"/narration/{track['id']}.mp3")
            assert r.status_code == 200, f"{track['id']}.mp3 missing"
            assert r.headers["content-type"] == "audio/mpeg"
            # ID3/MPEG frame sync. Catches a truncated or HTML-error-page file
            # committed under an .mp3 name, which would 200 and never play.
            assert r.content[:3] == b"ID3" or r.content[:2] == b"\xff\xfb", (
                f"{track['id']}.mp3 is not an MP3"
            )

    def test_every_narration_track_names_a_panel_on_the_page(self, site_client):
        """The manifest binds tracks to panels by id, in one direction only.

        The page looks tracks up by panel id, so a typo in the manifest is a
        panel with no narration and no error. Asserted both ways: every track
        must find its panel, and every panel must have a track — otherwise
        switching tabs hides the player mid-demo.
        """
        import re

        html = site_client.get("/architecture.html").text
        panel_ids = set(re.findall(r'role="tabpanel" id="([^"]+)"', html))
        assert len(panel_ids) == 3, f"expected three panels, found {panel_ids}"

        manifest = site_client.get("/narration/architecture-narration.json").json()
        track_panels = {t["panel"] for t in manifest["tracks"]}
        assert track_panels == panel_ids, (
            f"narration and panels disagree: {track_panels ^ panel_ids}"
        )

    def test_the_narration_durations_are_real(self, site_client):
        """The progress bar reads duration from here before the file loads.

        build_narration_audio.sh writes the ffprobe measurement back into the
        JSON. A zero or missing value gives a bar that cannot move and a
        "0:00 / 0:00" readout on a track that plays for a minute and a half.
        """
        manifest = site_client.get("/narration/architecture-narration.json").json()
        for track in manifest["tracks"]:
            assert track.get("duration", 0) > 10, (
                f"{track['id']} has no plausible duration — re-run the build script"
            )

    def test_the_narration_text_matches_the_ssml(self, site_client):
        """Two fields, one script: the transcript must be what Polly read.

        The SSML is what got synthesized; the plain text is what the transcript
        panel shows. They are maintained by hand as a pair, so a claim corrected
        in one and not the other puts a caption on screen that disagrees with
        the audio playing under it.
        """
        import re

        manifest = site_client.get("/narration/architecture-narration.json").json()
        for track in manifest["tracks"]:
            spoken = re.sub(r"<[^>]+>", "", track["ssml"])
            spoken = re.sub(r"\s+", " ", spoken).strip()
            shown = re.sub(r"\s+", " ", track["text"]).strip()
            assert spoken == shown, (
                f"{track['id']}: transcript differs from the synthesized SSML"
            )

    def test_the_audio_is_seekable(self, site_client):
        """Range support is what makes the scrub bar work.

        Without Accept-Ranges a browser reports audio.seekable as empty and
        refuses to seek at all — the bar moves and the audio doesn't. Measured
        in Chromium, not inferred. S3 serves ranges, so a gateway that doesn't
        would demo worse than the deployed site.
        """
        head = site_client.get("/narration/infrastructure.mp3")
        assert head.headers.get("accept-ranges") == "bytes"

        r = site_client.get(
            "/narration/infrastructure.mp3", headers={"Range": "bytes=100-199"}
        )
        assert r.status_code == 206
        assert r.headers["content-range"].startswith("bytes 100-199/")
        assert len(r.content) == 100

    def test_an_unusable_range_serves_the_whole_file(self, site_client):
        """A media element must never be handed an error for a bad range.

        The spec allows ignoring a Range it doesn't understand, and the whole
        file is already in memory, so 200 is both cheaper and safer than the
        416 a stricter reading would send.
        """
        full = len(site_client.get("/narration/infrastructure.mp3").content)
        for bad in ("bananas", "bytes=99999999-", "bytes=500-100", "bytes=0-1,5-6"):
            r = site_client.get(
                "/narration/infrastructure.mp3", headers={"Range": bad}
            )
            assert r.status_code == 200, f"{bad!r} produced {r.status_code}"
            assert len(r.content) == full, f"{bad!r} truncated the response"

    def test_the_site_route_does_not_expose_the_cdk_app(self, site_client):
        """site/infra/ holds the deploy config, not public assets.

        The narration lives one level down in site/narration/, so the depth
        check alone no longer keeps this out — SITE_ASSET_DIRS names the one
        subdirectory that is public, and site/infra/ is not in it.
        """
        for path in ("/infra", "/infra/stack.py", "/infra/app.py"):
            assert site_client.get(path).status_code == 404, f"{path} is readable"

    def test_a_served_suffix_inside_the_cdk_app_is_still_refused(self, site_client):
        """The dangerous case, made real rather than asserted against nothing.

        Everything currently in site/infra/ is .py, which 404s on the suffix
        check alone — so a depth rule that had replaced the directory allow-list
        would pass the test above while handing out site/infra/cdk.json. This
        writes one to prove the refusal comes from the allow-list.
        """
        import pathlib

        infra = pathlib.Path(__file__).resolve().parents[2] / "site" / "infra"
        planted = infra / "cdk.json"
        assert not planted.exists(), "would clobber a real file — rename the probe"
        planted.write_text('{"app": "python3 app.py"}', encoding="utf-8")
        try:
            assert site_client.get("/infra/cdk.json").status_code == 404
        finally:
            planted.unlink()

    def test_only_the_named_subdirectory_is_public(self, site_client):
        """Depth is not the rule; the allow-list is.

        Asserted against the constants rather than a fixed path so adding a
        directory to SITE_ASSET_DIRS is a deliberate act with a test that
        already describes what it means.
        """
        import pathlib

        from src.gateway.admin.routes import SITE_ASSET_DIRS, _is_servable_site_path

        assert "narration" in SITE_ASSET_DIRS
        assert _is_servable_site_path(pathlib.PurePosixPath("narration/x.mp3"))
        assert not _is_servable_site_path(pathlib.PurePosixPath("infra/cdk.json"))
        # Two levels below site/ is out regardless of the directory.
        assert not _is_servable_site_path(
            pathlib.PurePosixPath("narration/deep/x.mp3")
        )
        # A served suffix in an unlisted directory is still out.
        assert not _is_servable_site_path(pathlib.PurePosixPath("secrets/x.json"))

    def test_the_site_route_rejects_traversal(self, site_client):
        """The path param is attacker-controlled; confine it to site/."""
        for path in ("/..%2fconfig%2fmodels.yaml", "/....//pyproject.toml"):
            r = site_client.get(path)
            assert r.status_code == 404, f"{path} escaped site/ ({r.status_code})"

    def test_the_site_route_does_not_shadow_the_app_pages(self, admin_api):
        """These are bare segments — order decides whether /chat survives.

        build_app appends the site routes last for exactly this reason. If they
        were ever merged into create_admin_routes, /chat, /playground and
        /routing would resolve here and 404 instead of rendering.

        Both patterns are also asserted to be segment-counted rather than
        ``:path`` convertors. A ``/{path:path}`` would swallow /admin/projects
        and every other multi-segment route, turning "must be last" from a
        three-page concern into an application-wide one.
        """
        from src.gateway.admin.routes import create_site_routes

        site_paths = {r.path for r in create_site_routes(admin_api)}
        assert site_paths == {"/{path}", "/{directory}/{path}"}, site_paths
        for path in site_paths:
            assert ":path" not in path, f"{path} matches unbounded depth"
        admin_paths = {r.path for r in create_admin_routes(admin_api)}
        assert not site_paths & admin_paths, site_paths & admin_paths
