"""Data-plane integration tests for canonical tenant authorization."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.auth.project_repository import ProjectStoreUnavailable
from src.gateway.middleware.tenant_authorization import TenantAuthorizationMiddleware
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    Project,
    RequestContext,
    TenantRole,
)


class _ProjectResolver:
    def __init__(
        self,
        *,
        tenant_id: str = "tenant-a",
        missing: bool = False,
        unavailable: bool = False,
    ) -> None:
        self.tenant_id = tenant_id
        self.missing = missing
        self.unavailable = unavailable

    async def resolve(self, tenant_id: str, project_id: str) -> Project | None:
        if self.unavailable:
            raise ProjectStoreUnavailable("unavailable")
        if self.missing or tenant_id != self.tenant_id:
            return None
        return Project(
            project_id=project_id,
            tenant_id=self.tenant_id,
            name=project_id,
        )


class _PrincipalMiddleware:
    def __init__(
        self,
        app,
        principal: Principal | None,
        project_id: str,
    ):
        self.app = app
        self.principal = principal
        self.project_id = project_id

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})
            scope["state"]["principal"] = self.principal
            scope["state"]["context"] = RequestContext(
                user_id=self.principal.principal_id if self.principal else "legacy",
                project_id=self.project_id,
                roles=(
                    [role.value for role in self.principal.roles]
                    if self.principal
                    else []
                ),
                scopes=list(self.principal.scopes) if self.principal else [],
                auth_method=(
                    self.principal.auth_method
                    if self.principal
                    else AuthMethod.OIDC_JWT
                ),
                tenant_id=self.principal.tenant_id if self.principal else None,
            )
        await self.app(scope, receive, send)


async def _ok(request: Request) -> JSONResponse:
    project = getattr(request.state.context, "authorized_project", None)
    return JSONResponse({
        "ok": True,
        "project_id": project.project_id if project is not None else None,
        "tenant_id": project.tenant_id if project is not None else None,
    })


def _principal(
    role: TenantRole,
    *,
    scopes: frozenset[str] = frozenset(),
    projects: frozenset[str] = frozenset({"project-a"}),
) -> Principal:
    return Principal(
        principal_id=f"principal:{role.value}",
        tenant_id="tenant-a",
        subject=f"subject:{role.value}",
        issuer="https://idp.example.test",
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=projects,
        scopes=scopes,
    )


def _client(
    principal: Principal | None,
    *,
    project_id: str = "project-a",
    project_resolver: _ProjectResolver | None = None,
) -> TestClient:
    app = Starlette(routes=[
        Route("/api/models", _ok, methods=["GET"]),
        Route("/api/users", _ok, methods=["GET"]),
        Route("/api/chat", _ok, methods=["POST"]),
        Route("/v1/chat/completions", _ok, methods=["POST"]),
        Route("/v1/responses", _ok, methods=["POST"]),
        Route("/v1/embeddings", _ok, methods=["POST"]),
        Route("/unmapped", _ok, methods=["POST"]),
    ])
    app.add_middleware(
        TenantAuthorizationMiddleware,
        project_resolver=project_resolver or _ProjectResolver(),
        require_tenant_project=True,
    )
    app.add_middleware(
        _PrincipalMiddleware,
        principal=principal,
        project_id=project_id,
    )
    return TestClient(app)


def test_tenant_member_may_list_models_and_invoke() -> None:
    client = _client(_principal(TenantRole.TENANT_MEMBER))

    response = client.get("/api/models")
    assert response.status_code == 200
    assert response.json()["project_id"] == "project-a"
    assert response.json()["tenant_id"] == "tenant-a"
    assert client.post("/api/chat").status_code == 200
    assert client.post("/v1/chat/completions").status_code == 200
    assert client.post("/v1/responses").status_code == 200
    assert client.post("/v1/embeddings").status_code == 200


def test_service_requires_explicit_inference_scope() -> None:
    assert _client(
        _principal(TenantRole.SERVICE),
    ).post("/api/chat").status_code == 403
    assert _client(
        _principal(
            TenantRole.SERVICE,
            scopes=frozenset({"inference.invoke"}),
        ),
    ).post("/api/chat").status_code == 200


def test_service_inference_scope_does_not_grant_model_list() -> None:
    response = _client(
        _principal(
            TenantRole.SERVICE,
            scopes=frozenset({"inference.invoke"}),
        ),
    ).get("/api/models")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_not_allowed"


def test_missing_project_grant_is_concealed() -> None:
    response = _client(
        _principal(TenantRole.TENANT_MEMBER, projects=frozenset()),
    ).post("/api/chat")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_tenant_admin_cannot_assume_unverified_project_ownership() -> None:
    response = _client(
        _principal(TenantRole.TENANT_ADMIN, projects=frozenset()),
        project_id="tenant-b-project",
    ).post("/api/chat")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_missing_authoritative_project_is_concealed() -> None:
    response = _client(
        _principal(TenantRole.TENANT_MEMBER),
        project_resolver=_ProjectResolver(missing=True),
    ).post("/api/chat")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_project_store_outage_fails_closed() -> None:
    response = _client(
        _principal(TenantRole.TENANT_MEMBER),
        project_resolver=_ProjectResolver(unavailable=True),
    ).post("/api/chat")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "project_resolver_unavailable"


def test_unmapped_canonical_api_route_defaults_to_deny() -> None:
    response = _client(
        _principal(
            TenantRole.SERVICE,
            scopes=frozenset({"inference.invoke"}),
        )
    ).get("/api/users")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "canonical_action_required"


def test_head_uses_the_same_model_list_authorization_as_get() -> None:
    client = _client(
        _principal(
            TenantRole.SERVICE,
            scopes=frozenset({"inference.invoke"}),
        )
    )

    assert client.get("/api/models").status_code == 403
    assert client.head("/api/models").status_code == 403


def test_canonical_request_requires_explicit_project_context() -> None:
    response = _client(
        _principal(TenantRole.TENANT_ADMIN),
        project_id="",
    ).post("/api/chat")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "project_context_required"


def test_legacy_migration_mode_is_unchanged() -> None:
    assert _client(None).post("/api/chat").status_code == 200


def test_unmapped_paths_are_left_to_their_route_authorizer() -> None:
    assert _client(_principal(TenantRole.SERVICE)).post("/unmapped").status_code == 200
