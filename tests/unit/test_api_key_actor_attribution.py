"""Canonical API-key actor attribution cannot be supplied by the caller."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.key_routes import KeyManagementAPI, create_key_routes
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.models import RequestContext
from src.gateway.persistence import DynamoPersistence


@pytest.fixture
def service(monkeypatch) -> APIKeyService:
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    persistence = DynamoPersistence()
    assert not persistence.enabled
    return APIKeyService(persistence)


def _client(
    service: APIKeyService,
    *,
    canonical: bool,
) -> TestClient:
    async def add_context(request, call_next):
        request.state.context = RequestContext(
            user_id=(
                "principal:tenant-admin"
                if canonical
                else "legacy-admin"
            ),
            project_id="project-a",
            roles=["tenant_admin"] if canonical else ["admin"],
            scopes=[] if canonical else ["admin:*"],
            tenant_id="tenant-a" if canonical else None,
            principal_id="principal:tenant-admin" if canonical else None,
        )
        return await call_next(request)

    app = Starlette(
        routes=create_key_routes(
            KeyManagementAPI(service, mode="ENFORCE")
        )
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
    return TestClient(app)


async def test_canonical_issue_ignores_spoofed_created_by(service) -> None:
    client = _client(service, canonical=True)

    response = client.post(
        "/admin/projects/project-a/keys",
        json={
            "name": "application",
            "scopes": ["inference.invoke"],
            "created_by": "spoofed-operator",
        },
    )

    assert response.status_code == 201
    keys = await service.list_keys("project-a", "tenant-a")
    assert len(keys) == 1
    assert keys[0].created_by == "principal:tenant-admin"


async def test_canonical_rotation_ignores_spoofed_rotated_by(service) -> None:
    old_key, _ = await service.issue_key(
        "project-a",
        "application",
        ["inference.invoke"],
        "original-operator",
        tenant_id="tenant-a",
    )
    client = _client(service, canonical=True)

    response = client.post(
        f"/admin/keys/{old_key.key_id}/rotate",
        json={"rotated_by": "spoofed-operator"},
    )

    assert response.status_code == 201
    replacement_id = response.json()["new_key_id"]
    keys = await service.list_keys("project-a", "tenant-a")
    replacement = next(key for key in keys if key.key_id == replacement_id)
    assert replacement.created_by == "principal:tenant-admin"
    source = next(key for key in keys if key.key_id == old_key.key_id)
    assert source.revoked_by == "principal:tenant-admin"


async def test_canonical_revoke_persists_principal_actor(service) -> None:
    key, _ = await service.issue_key(
        "project-a",
        "application",
        ["inference.invoke"],
        "original-operator",
        tenant_id="tenant-a",
    )
    client = _client(service, canonical=True)

    response = client.delete(f"/admin/keys/{key.key_id}")

    assert response.status_code == 200
    keys = await service.list_keys("project-a", "tenant-a")
    revoked = next(item for item in keys if item.key_id == key.key_id)
    assert revoked.revoked is True
    assert revoked.revoked_at is not None
    assert revoked.revoked_by == "principal:tenant-admin"


async def test_legacy_issue_retains_body_actor_compatibility(service) -> None:
    client = _client(service, canonical=False)

    response = client.post(
        "/admin/projects/project-a/keys",
        json={
            "name": "legacy",
            "scopes": ["chat"],
            "created_by": "legacy-operator",
        },
    )

    assert response.status_code == 201
    keys = await service.list_keys("project-a")
    assert len(keys) == 1
    assert keys[0].created_by == "legacy-operator"
