"""Tests for claim-to-principal resolution at the authentication boundary."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.auth.principal import (
    CanonicalPrincipalResolver,
    InMemoryPrincipalRepository,
)
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    RequestContext,
    TenantRole,
)


ISSUER = "https://idp.example.test"


class _OIDCService:
    def __init__(self, context: RequestContext) -> None:
        self.context = context

    async def validate_alb_jwt(
        self,
        token: str,
        expected_subject: str,
    ) -> RequestContext | None:
        return None

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None:
        return self.context


class _UnavailableResolver:
    async def resolve(self, context: RequestContext) -> Principal | None:
        raise RuntimeError("identity store unavailable")


def _principal(
    *,
    role: TenantRole = TenantRole.TENANT_MEMBER,
    tenant_id: str = "tenant-a",
    status: MembershipStatus = MembershipStatus.ACTIVE,
    projects: frozenset[str] = frozenset({"project-a"}),
) -> Principal:
    return Principal(
        principal_id="principal-123",
        tenant_id=tenant_id,
        subject="external-subject",
        issuer=ISSUER,
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=status,
        project_ids=projects,
        scopes=frozenset({"inference.invoke"}),
        authorization_version=7,
        email="user@example.test",
    )


def _claim_context(
    *,
    roles: list[str] | None = None,
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
) -> RequestContext:
    return RequestContext(
        user_id="external-subject",
        project_id=project_id,
        roles=roles or [],
        scopes=["admin:*"],
        auth_method=AuthMethod.OIDC_JWT,
        tenant_id=tenant_id,
        issuer=ISSUER,
        subject="external-subject",
    )


def _client(
    claim_context: RequestContext,
    *,
    principals: list[Principal] | None = None,
    with_resolver: bool = True,
    require_canonical_principal: bool = False,
    resolver_override=None,
) -> TestClient:
    async def protected(request: Request) -> JSONResponse:
        context = request.state.context
        principal = request.state.principal
        return JSONResponse({
            "user_id": context.user_id,
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
            "roles": context.roles,
            "scopes": context.scopes,
            "authorization_version": context.authorization_version,
            "principal_id": principal.principal_id if principal else None,
        })

    app = Starlette(routes=[Route("/protected", protected)])
    resolver = resolver_override
    if resolver is None and with_resolver:
        repository = InMemoryPrincipalRepository(principals or [])
        resolver = CanonicalPrincipalResolver(repository)
    app.add_middleware(
        AuthMiddleware,
        oidc_service=_OIDCService(claim_context),
        mode="ENFORCE",
        principal_resolver=resolver,
        require_canonical_principal=require_canonical_principal,
    )
    return TestClient(app)


def test_forged_admin_claim_is_replaced_by_server_membership() -> None:
    response = _client(
        _claim_context(roles=["admin", "platform_admin"]),
        principals=[_principal()],
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "principal-123"
    assert body["roles"] == ["tenant_member"]
    assert body["scopes"] == ["inference.invoke"]
    assert body["authorization_version"] == 7


def test_claimed_tenant_cannot_select_a_different_membership() -> None:
    response = _client(
        _claim_context(tenant_id="tenant-b"),
        principals=[_principal(tenant_id="tenant-a")],
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_membership_required"


def test_project_hint_must_be_granted_to_non_admin() -> None:
    response = _client(
        _claim_context(project_id="project-b"),
        principals=[_principal(projects=frozenset({"project-a"}))],
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403


def test_tenant_admin_project_hint_requires_an_explicit_grant() -> None:
    response = _client(
        _claim_context(project_id="new-project"),
        principals=[
            _principal(
                role=TenantRole.TENANT_ADMIN,
                projects=frozenset(),
            )
        ],
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403


def test_deprovisioned_membership_is_denied() -> None:
    response = _client(
        _claim_context(),
        principals=[
            _principal(status=MembershipStatus.DEPROVISIONED)
        ],
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403


def test_missing_repository_membership_is_denied() -> None:
    response = _client(
        _claim_context(),
        principals=[],
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403


def test_required_resolver_configuration_fails_closed() -> None:
    response = _client(
        _claim_context(),
        with_resolver=False,
        require_canonical_principal=True,
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "principal_resolver_unavailable"


def test_authoritative_identity_store_outage_is_sanitized() -> None:
    response = _client(
        _claim_context(),
        resolver_override=_UnavailableResolver(),
    ).get("/protected", headers={"Authorization": "Bearer token"})

    assert response.status_code == 503
    assert response.json()["error"] == {
        "type": "authorization_error",
        "message": "Canonical principal resolution is temporarily unavailable.",
        "code": "principal_resolver_unavailable",
    }


async def test_identity_without_tenant_hint_is_ambiguous_across_tenants() -> None:
    repository = InMemoryPrincipalRepository([
        _principal(tenant_id="tenant-a"),
        _principal(tenant_id="tenant-b"),
    ])
    resolver = CanonicalPrincipalResolver(repository)
    context = _claim_context(tenant_id="")
    context.tenant_id = None

    assert await resolver.resolve(context) is None
