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
    assert len(routes) == 30

    paths = [r.path for r in routes]
    assert "/admin/dashboard" in paths
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
