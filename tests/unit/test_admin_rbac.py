"""Tests for admin RBAC middleware."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import AuthMethod, RequestContext


async def mock_admin_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def mock_public_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "public"})


class FakeAuthMiddleware:
    """Injects a RequestContext based on X-Test-Role header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.requests import Request
            request = Request(scope, receive)
            role = request.headers.get("x-test-role", "")
            scope_val = request.headers.get("x-test-scope", "")
            scope["state"] = {
                "context": RequestContext(
                    user_id="test-user",
                    project_id="proj:test",
                    roles=[role] if role else [],
                    scopes=[scope_val] if scope_val else [],
                    auth_method=AuthMethod.API_KEY,
                )
            }
        await self.app(scope, receive, send)


def _make_app(mode="ENFORCE"):
    app = Starlette(routes=[
        Route("/admin/quotas/proj:test", mock_admin_endpoint, methods=["GET"]),
        Route("/admin/dashboard", mock_public_endpoint, methods=["GET"]),
        Route("/api/chat", mock_public_endpoint, methods=["POST"]),
    ])
    app.add_middleware(AdminRBACMiddleware, mode=mode)
    app.add_middleware(FakeAuthMiddleware)
    return TestClient(app)


class TestAdminRBACEnforce:
    def test_blocks_without_admin_role(self):
        client = _make_app("ENFORCE")
        resp = client.get("/admin/quotas/proj:test", headers={"x-test-role": "user"})
        assert resp.status_code == 403
        assert "admin_access_denied" in resp.json()["error"]["code"]

    def test_allows_admin_role(self):
        client = _make_app("ENFORCE")
        resp = client.get("/admin/quotas/proj:test", headers={"x-test-role": "admin"})
        assert resp.status_code == 200

    def test_allows_admin_wildcard_scope(self):
        client = _make_app("ENFORCE")
        resp = client.get("/admin/quotas/proj:test", headers={"x-test-scope": "admin:*"})
        assert resp.status_code == 200

    def test_allows_specific_admin_scope(self):
        client = _make_app("ENFORCE")
        resp = client.get("/admin/quotas/proj:test", headers={"x-test-scope": "admin:quotas"})
        assert resp.status_code == 200

    def test_denies_mismatched_admin_scope(self):
        client = _make_app("ENFORCE")
        resp = client.get("/admin/quotas/proj:test", headers={"x-test-scope": "admin:keys"})
        assert resp.status_code == 403

    def test_dashboard_is_public(self):
        client = _make_app("ENFORCE")
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200

    def test_non_admin_paths_unaffected(self):
        client = _make_app("ENFORCE")
        resp = client.post("/api/chat", json={})
        assert resp.status_code == 200


class TestAdminRBACLogOnly:
    def test_allows_in_log_only_mode(self):
        client = _make_app("LOG_ONLY")
        resp = client.get("/admin/quotas/proj:test", headers={"x-test-role": "user"})
        assert resp.status_code == 200
