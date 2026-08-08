"""Durable audit requirements for platform-admin tenant elevation."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.models import (
    AuthMethod,
    Principal,
    RequestContext,
    TenantRole,
)


class _CanonicalPlatformAdmin:
    def __init__(self, app, *, attach_principal: bool = True):
        self.app = app
        self.attach_principal = attach_principal

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            principal = Principal(
                principal_id="principal-platform-1",
                tenant_id="platform-home",
                subject="platform-operator",
                issuer="https://idp.example.test",
                roles=frozenset({TenantRole.PLATFORM_ADMIN}),
                auth_method=AuthMethod.OIDC_JWT,
            )
            scope["state"] = {
                "context": RequestContext(
                    user_id=principal.principal_id,
                    project_id="",
                    roles=["platform_admin"],
                    scopes=[],
                    auth_method=AuthMethod.OIDC_JWT,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    authorization_version=principal.authorization_version,
                )
            }
            if self.attach_principal:
                scope["state"]["principal"] = principal
        await self.app(scope, receive, send)


class _Audit:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict] = []

    async def record_break_glass_access(self, **record):
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.records.append(record)


def _client(audit, *, attach_principal: bool = True) -> TestClient:
    async def _ok(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "handler_tenant_id": request.state.context.tenant_id,
            "principal_tenant_id": request.state.principal.tenant_id,
        })

    app = Starlette(
        routes=[
            Route("/admin/projects", _ok, methods=["GET", "POST"]),
            Route("/admin/models", _ok, methods=["GET"]),
        ]
    )
    app.add_middleware(
        AdminRBACMiddleware,
        mode="ENFORCE",
        audit_trail=audit,
    )
    app.add_middleware(
        _CanonicalPlatformAdmin,
        attach_principal=attach_principal,
    )
    return TestClient(app)


def test_allowed_elevation_is_audited_before_access() -> None:
    audit = _Audit()
    response = _client(audit).post(
        "/admin/projects",
        headers={
            "X-Axon-Break-Glass-Reason": "incident INC-1234",
            "X-Axon-Target-Tenant": "tenant-a",
        },
    )

    assert response.status_code == 200
    assert response.json()["handler_tenant_id"] == "tenant-a"
    assert response.json()["principal_tenant_id"] == "platform-home"
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record["principal_id"] == "principal-platform-1"
    assert record["tenant_id"] == "tenant-a"
    assert record["route"] == "/admin/projects"
    assert record["method"] == "POST"
    assert record["reason"] == "incident INC-1234"
    assert record["result"] == "allowed"
    assert record["access"] == "write"


def test_denied_elevation_is_also_audited() -> None:
    audit = _Audit()
    response = _client(audit).get(
        "/admin/projects",
        headers={"X-Axon-Target-Tenant": "tenant-a"},
    )

    assert response.status_code == 403
    assert len(audit.records) == 1
    assert audit.records[0]["reason"] == ""
    assert audit.records[0]["result"] == "denied"
    assert audit.records[0]["access"] == "read"


def test_audit_failure_blocks_elevation() -> None:
    response = _client(_Audit(fail=True)).post(
        "/admin/projects",
        headers={
            "X-Axon-Break-Glass-Reason": "incident INC-1234",
            "X-Axon-Target-Tenant": "tenant-a",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "break_glass_audit_unavailable"


def test_missing_audit_wiring_blocks_elevation() -> None:
    response = _client(None).post(
        "/admin/projects",
        headers={
            "X-Axon-Break-Glass-Reason": "incident INC-1234",
            "X-Axon-Target-Tenant": "tenant-a",
        },
    )

    assert response.status_code == 503


def test_platform_resource_access_is_not_break_glass() -> None:
    response = _client(None).get("/admin/models")

    assert response.status_code == 200


def test_target_tenant_is_required_and_not_inferred_from_home_tenant() -> None:
    audit = _Audit()
    response = _client(audit).get(
        "/admin/projects",
        headers={"X-Axon-Break-Glass-Reason": "incident INC-1234"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_break_glass_target"
    assert audit.records == []


def test_conflicting_query_tenant_is_rejected_before_audit() -> None:
    audit = _Audit()
    response = _client(audit).get(
        "/admin/projects?tenant_id=tenant-b",
        headers={
            "X-Axon-Break-Glass-Reason": "incident INC-1234",
            "X-Axon-Target-Tenant": "tenant-a",
        },
    )

    assert response.status_code == 400
    assert audit.records == []


def test_duplicate_target_headers_are_rejected() -> None:
    response = _client(_Audit()).get(
        "/admin/projects",
        headers=[
            ("X-Axon-Break-Glass-Reason", "incident INC-1234"),
            ("X-Axon-Target-Tenant", "tenant-a"),
            ("X-Axon-Target-Tenant", "tenant-b"),
        ],
    )

    assert response.status_code == 400


def test_claim_supplied_platform_role_cannot_break_glass() -> None:
    response = _client(
        _Audit(),
        attach_principal=False,
    ).get(
        "/admin/projects",
        headers={
            "X-Axon-Break-Glass-Reason": "incident INC-1234",
            "X-Axon-Target-Tenant": "tenant-a",
        },
    )

    assert response.status_code == 403


def test_home_tenant_is_not_a_break_glass_target() -> None:
    audit = _Audit()
    response = _client(audit).get(
        "/admin/projects",
        headers={
            "X-Axon-Break-Glass-Reason": "incident INC-1234",
            "X-Axon-Target-Tenant": "platform-home",
        },
    )

    assert response.status_code == 403
    assert audit.records[0]["tenant_id"] == "platform-home"
    assert audit.records[0]["result"] == "denied"
