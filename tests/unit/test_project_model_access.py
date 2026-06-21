"""Unit tests for project model access API endpoints.

Tests the project model access management surface using Starlette TestClient:
- POST /admin/projects/{id}/models (add model)
- DELETE /admin/projects/{id}/models/{model_name} (remove model)
- GET /admin/projects/{id}/models (list models)
- DynamoDB persistence failure handling

Requirements: 1, 2, 3, 4
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import Project


# ── Helpers ──────────────────────────────────────────────────────────


def _make_app(admin_api: AdminAPI) -> Starlette:
    routes = create_admin_routes(admin_api)
    return Starlette(routes=routes)


def _make_project(
    project_id: str = "proj-1",
    name: str = "Test Project",
    allowed_models: list[str] | None = None,
) -> Project:
    return Project(project_id=project_id, name=name, allowed_models=allowed_models)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def cost_tracker():
    return CostTracker(pricing_config={})


@pytest.fixture
def health_tracker():
    return ProviderHealthTracker()


@pytest.fixture
def model_registry():
    return ModelRegistry()


@pytest.fixture
def admin_api(cost_tracker, health_tracker, model_registry):
    project = _make_project(allowed_models=["gpt-4", "claude-3"])
    return AdminAPI(
        cost_tracker=cost_tracker,
        health_tracker=health_tracker,
        model_registry=model_registry,
        projects={"proj-1": project},
    )


@pytest.fixture
def client(admin_api):
    app = _make_app(admin_api)
    return TestClient(app, raise_server_exceptions=False)


# ── Add Project Model (POST /admin/projects/{id}/models) ────────────


class TestAddProjectModel:
    """POST /admin/projects/{id}/models — Requirement 1"""

    def test_add_model_success(self, client):
        resp = client.post("/admin/projects/proj-1/models", json={"model": "gemini-pro"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "added"
        assert data["model"] == "gemini-pro"
        assert "gemini-pro" in data["allowed_models"]
        assert data["project_id"] == "proj-1"

    def test_add_model_duplicate_no_duplicate_in_list(self, client):
        resp = client.post("/admin/projects/proj-1/models", json={"model": "gpt-4"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed_models"].count("gpt-4") == 1

    def test_add_model_none_initialization(self, admin_api, client):
        """When allowed_models is None, adding a model initializes the list."""
        admin_api.projects["proj-none"] = _make_project(
            project_id="proj-none", allowed_models=None,
        )
        resp = client.post("/admin/projects/proj-none/models", json={"model": "claude-opus"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed_models"] == ["claude-opus"]

    def test_add_model_missing_model_field_returns_400(self, client):
        resp = client.post("/admin/projects/proj-1/models", json={"wrong_field": "value"})
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request"
        assert "model" in resp.json()["error"]["message"].lower()

    def test_add_model_project_not_found_returns_404(self, client):
        resp = client.post("/admin/projects/nonexistent/models", json={"model": "gpt-4"})
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "not_found"
        assert "not found" in resp.json()["error"]["message"].lower()


# ── Remove Project Model (DELETE /admin/projects/{id}/models/{model_name}) ──


class TestRemoveProjectModel:
    """DELETE /admin/projects/{id}/models/{model_name} — Requirement 2"""

    def test_remove_model_success(self, client):
        resp = client.delete("/admin/projects/proj-1/models/gpt-4")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "removed"
        assert data["model"] == "gpt-4"
        assert "gpt-4" not in data["allowed_models"]
        assert data["project_id"] == "proj-1"

    def test_remove_model_not_in_list_returns_404(self, client):
        resp = client.delete("/admin/projects/proj-1/models/nonexistent-model")
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "not_found"

    def test_remove_model_project_not_found_returns_404(self, client):
        resp = client.delete("/admin/projects/nonexistent/models/gpt-4")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]["message"].lower()

    def test_remove_last_model_leaves_empty_list(self, admin_api, client):
        admin_api.projects["proj-single"] = _make_project(
            project_id="proj-single", allowed_models=["only-model"],
        )
        resp = client.delete("/admin/projects/proj-single/models/only-model")
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed_models"] == []


# ── List Project Models (GET /admin/projects/{id}/models) ────────────


class TestListProjectModels:
    """GET /admin/projects/{id}/models — Requirement 3"""

    def test_list_models_success(self, client):
        resp = client.get("/admin/projects/proj-1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "proj-1"
        assert set(data["allowed_models"]) == {"gpt-4", "claude-3"}

    def test_list_models_none_returns_empty_list(self, admin_api, client):
        admin_api.projects["proj-none"] = _make_project(
            project_id="proj-none", allowed_models=None,
        )
        resp = client.get("/admin/projects/proj-none/models")
        assert resp.status_code == 200
        assert resp.json()["allowed_models"] == []

    def test_list_models_project_not_found_returns_404(self, client):
        resp = client.get("/admin/projects/nonexistent/models")
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "not_found"


# ── DynamoDB Persistence Failure ─────────────────────────────────────


class TestDynamoPersistenceFailure:
    """DynamoDB persistence failure handling — Requirement 4"""

    def test_add_model_dynamo_failure_logs_warning_returns_success(
        self, cost_tracker, health_tracker, model_registry, caplog
    ):
        mock_persistence = MagicMock()
        mock_persistence.enabled = True
        mock_persistence.save_project = AsyncMock(side_effect=Exception("DynamoDB unavailable"))

        project = _make_project(allowed_models=["gpt-4"])
        api = AdminAPI(
            cost_tracker=cost_tracker,
            health_tracker=health_tracker,
            model_registry=model_registry,
            projects={"proj-1": project},
            persistence=mock_persistence,
        )
        app = _make_app(api)
        client = TestClient(app, raise_server_exceptions=False)

        with caplog.at_level(logging.WARNING, logger="src.gateway.admin.routes"):
            resp = client.post("/admin/projects/proj-1/models", json={"model": "new-model"})

        assert resp.status_code == 200
        assert "new-model" in resp.json()["allowed_models"]
        assert any("Failed to persist" in msg for msg in caplog.messages)
