"""End-to-end isolation checks for the shipped multi-tenant demo."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI
from src.gateway.auth.principal import (
    API_KEY_ISSUER,
    CanonicalPrincipalResolver,
    InMemoryPrincipalRepository,
)
from src.gateway.config_loader import load_demo_seed_config
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    APIKey,
    AuthMethod,
    MembershipStatus,
    Principal,
    Project,
    TenantRole,
)

SEED_PATH = "config/demo_seed_multitenant.yaml"


class _APIKeys:
    def __init__(self, keys: dict[str, APIKey]) -> None:
        self.keys = keys

    async def validate_key(self, raw_key: str) -> APIKey | None:
        return self.keys.get(raw_key)


class _Projects:
    enabled = True

    def __init__(self, projects: list[Project]) -> None:
        self.projects = {
            (project.tenant_id, project.project_id): project
            for project in projects
        }

    async def list_tenant_projects(
        self,
        tenant_id: str,
    ) -> list[Project]:
        return [
            project
            for (owner, _project_id), project in self.projects.items()
            if owner == tenant_id
        ]

    async def get_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> Project | None:
        return self.projects.get((tenant_id, project_id))


def _key(key_id: str, tenant_id: str) -> APIKey:
    return APIKey(
        key_id=key_id,
        key_hash="unused",
        project_id="proj-alpha",
        name=f"{tenant_id} demo admin",
        scopes=[],
        created_by="demo-bootstrap",
        tenant_id=tenant_id,
        principal_role=TenantRole.TENANT_ADMIN,
        created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )


def _principal(key: APIKey) -> Principal:
    return Principal(
        principal_id=f"apikey:{key.key_id}",
        tenant_id=key.tenant_id or "",
        subject=key.key_id,
        issuer=API_KEY_ISSUER,
        roles=frozenset({TenantRole.TENANT_ADMIN}),
        auth_method=AuthMethod.API_KEY,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=frozenset({key.project_id}),
        credential_id=key.key_id,
    )


def _client() -> tuple[TestClient, str, str]:
    acme_key = _key("axk_acme", "tenant-acme")
    globex_key = _key("axk_globex", "tenant-globex")
    raw_acme = "axon_" + "a" * 64
    raw_globex = "axon_" + "b" * 64
    projects = _Projects(
        [
            Project(
                project_id="proj-alpha",
                tenant_id="tenant-acme",
                name="Alpha Team",
                budget_limit=500.0,
            ),
            Project(
                project_id="proj-alpha",
                tenant_id="tenant-globex",
                name="Globex Growth AI",
                budget_limit=1250.0,
            ),
            Project(
                project_id="proj-orbit",
                tenant_id="tenant-globex",
                name="Orbit Support Copilot",
                budget_limit=350.0,
            ),
        ]
    )
    tracker = CostTracker({})
    for project in projects.projects.values():
        tracker.register_project(
            project.project_id,
            project.budget_limit,
            tenant_id=project.tenant_id,
        )
    api = AdminAPI(
        cost_tracker=tracker,
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        persistence=projects,
    )
    app = Starlette(
        routes=[
            Route("/admin/session", api.session_context),
            Route("/admin/projects", api.list_projects),
            Route("/admin/projects/{id}", api.get_project),
        ]
    )
    app.add_middleware(AdminRBACMiddleware, mode="ENFORCE")
    app.add_middleware(
        AuthMiddleware,
        api_key_service=_APIKeys(
            {raw_acme: acme_key, raw_globex: globex_key}
        ),
        principal_resolver=CanonicalPrincipalResolver(
            InMemoryPrincipalRepository(
                [_principal(acme_key), _principal(globex_key)]
            )
        ),
        require_canonical_principal=True,
        mode="ENFORCE",
    )
    return TestClient(app), raw_acme, raw_globex


def _authorization(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def test_seed_contains_two_namespaces_with_deliberate_id_collisions() -> None:
    seed = load_demo_seed_config(SEED_PATH)

    assert {tenant["tenant_id"] for tenant in seed.tenants} == {
        "tenant-acme",
        "tenant-globex",
    }
    shared_projects = {
        project["tenant_id"]
        for project in seed.projects
        if project["project_id"] == "proj-alpha"
    }
    shared_users = {
        budget["tenant_id"]
        for budget in seed.user_budgets
        if budget["user_id"] == "user-alice"
    }
    assert shared_projects == {"tenant-acme", "tenant-globex"}
    assert shared_users == {"tenant-acme", "tenant-globex"}


def test_dashboard_credentials_switch_the_complete_tenant_view() -> None:
    client, acme_key, globex_key = _client()

    acme_session = client.get(
        "/admin/session",
        headers=_authorization(acme_key),
    )
    globex_session = client.get(
        "/admin/session",
        headers=_authorization(globex_key),
    )
    assert acme_session.json()["tenant_id"] == "tenant-acme"
    assert globex_session.json()["tenant_id"] == "tenant-globex"
    assert acme_session.json()["roles"] == ["tenant_admin"]
    assert globex_session.json()["roles"] == ["tenant_admin"]

    acme_projects = client.get(
        "/admin/projects",
        headers=_authorization(acme_key),
    ).json()
    globex_projects = client.get(
        "/admin/projects",
        headers=_authorization(globex_key),
    ).json()
    assert [(p["project_id"], p["name"]) for p in acme_projects] == [
        ("proj-alpha", "Alpha Team")
    ]
    assert {
        (p["project_id"], p["name"]) for p in globex_projects
    } == {
        ("proj-alpha", "Globex Growth AI"),
        ("proj-orbit", "Orbit Support Copilot"),
    }

    assert client.get(
        "/admin/projects/proj-orbit",
        headers=_authorization(acme_key),
    ).status_code == 404
    assert client.get(
        "/admin/projects/proj-alpha",
        headers=_authorization(acme_key),
    ).json()["name"] == "Alpha Team"
    assert client.get(
        "/admin/projects/proj-alpha",
        headers=_authorization(globex_key),
    ).json()["name"] == "Globex Growth AI"


def test_dashboard_switcher_uses_credentials_not_tenant_parameters() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gateway"
        / "admin"
        / "dashboard.jsx"
    ).read_text(encoding="utf-8")

    assert "api.get('/admin/session')" in source
    assert "Tenant: {sessionContext.tenant_id}" in source
    assert "Switch demo tenant with another tenant-admin API key" in source
    assert "if (hasNewSessionKey(headers))" in source
    assert "tenant_id=" not in source
