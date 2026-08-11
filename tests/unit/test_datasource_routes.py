"""Datasource administration and canonical RBAC integration tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from src.gateway.admin.datasource_routes import (
    DatasourceAPI,
    _json_object,
    create_datasource_routes,
)
from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    Project,
    RequestContext,
    TenantRole,
)
from src.gateway.query.models import (
    AthenaRoleBinding,
    AthenaRoleBindings,
    QueryConfigurationError,
)
from src.gateway.query.repository import InMemoryDatasourceRepository
from src.gateway.security.audit_trail import AuditEventType


ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"


class _ProjectResolver:
    async def resolve(
        self,
        tenant_id: str,
        project_id: str,
    ) -> Project | None:
        if tenant_id != "tenant-a" or project_id != "project-a":
            return None
        return Project(
            project_id=project_id,
            tenant_id=tenant_id,
            name="Project A",
        )


class _AuditTrail:
    durable_enabled = True

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> object:
        if self.error is not None:
            raise self.error
        self.records.append(kwargs)
        return object()


class _IdentityMiddleware:
    def __init__(self, app: Any, *, role: TenantRole) -> None:
        self.app = app
        self.role = role

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            principal = Principal(
                principal_id=f"principal:{self.role.value}",
                tenant_id="tenant-a",
                subject=f"subject:{self.role.value}",
                issuer="https://issuer.example",
                roles=frozenset({self.role}),
                auth_method=AuthMethod.OIDC_JWT,
                membership_status=MembershipStatus.ACTIVE,
                project_ids=frozenset({"project-a"}),
                scopes=(
                    frozenset({"query.select"})
                    if self.role is TenantRole.SERVICE
                    else frozenset()
                ),
            )
            scope.setdefault("state", {})
            scope["state"]["principal"] = principal
            scope["state"]["context"] = RequestContext(
                user_id=principal.principal_id,
                project_id="project-a",
                roles=[self.role.value],
                scopes=list(principal.scopes),
                auth_method=principal.auth_method,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                authorization_version=1,
            )
        await self.app(scope, receive, send)


def _body(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "datasource_id": "warehouse",
        "project_id": "project-a",
        "name": "Analytics warehouse",
        "role_arn": ROLE_ARN,
        "region": "us-east-1",
        "catalog": "AwsDataCatalog",
        "database": "analytics",
        "workgroup": "axon_read_only",
        "enabled": True,
    }
    value.update(changes)
    return value


def _client(
    role: TenantRole,
    repository: InMemoryDatasourceRepository | None = None,
    audit: _AuditTrail | None = None,
) -> tuple[TestClient, InMemoryDatasourceRepository]:
    resolved_repository = (
        repository or InMemoryDatasourceRepository()
    )
    bindings = AthenaRoleBindings(
        (
            AthenaRoleBinding(
                tenant_id="tenant-a",
                project_id="project-a",
                role_arn=ROLE_ARN,
            ),
        )
    )
    api = DatasourceAPI(
        repository=resolved_repository,
        bindings=bindings,
        project_resolver=_ProjectResolver(),
        audit_trail=audit or _AuditTrail(),
        require_durable_audit=True,
    )
    app = Starlette(routes=create_datasource_routes(api))
    app.add_middleware(AdminRBACMiddleware, mode="ENFORCE")
    app.add_middleware(_IdentityMiddleware, role=role)
    return TestClient(app), resolved_repository


def _streaming_request(
    chunks: list[bytes],
    *,
    stream_error: Exception | None = None,
) -> tuple[Request, list[int]]:
    events = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    receive_calls: list[int] = []

    async def receive() -> dict[str, object]:
        receive_calls.append(1)
        if stream_error is not None:
            raise stream_error
        return events.pop(0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/datasources",
        "raw_path": b"/admin/datasources",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("testserver", 80),
    }
    return Request(scope, receive), receive_calls


async def test_datasource_stream_reader_stops_at_64_kib() -> None:
    request, calls = _streaming_request(
        [
            b"x" * (32 * 1024),
            b"x" * (32 * 1024 + 1),
            b"unread",
        ]
    )

    with pytest.raises(
        QueryConfigurationError,
        match="exceeds 64 KiB",
    ):
        await _json_object(request)

    assert len(calls) == 2


async def test_datasource_stream_failures_are_sanitized() -> None:
    secret = "sensitive-asgi-stream-failure"
    request, _ = _streaming_request(
        [],
        stream_error=RuntimeError(secret),
    )

    with pytest.raises(
        QueryConfigurationError,
        match="datasource request body could not be read",
    ) as raised:
        await _json_object(request)

    assert secret not in str(raised.value)


def test_tenant_admin_can_manage_datasource_with_cas() -> None:
    client, _ = _client(TenantRole.TENANT_ADMIN)

    created = client.post("/admin/datasources", json=_body())
    assert created.status_code == 201
    assert created.json()["revision"] == 1

    updated = client.put(
        "/admin/datasources/warehouse",
        json={
            **_body(),
            "datasource_id": "warehouse",
            "expected_revision": 1,
            "name": "Updated warehouse",
        },
    )
    # The resource id is path-owned and must not be accepted in the body.
    assert updated.status_code == 400

    update_body = _body(
        expected_revision=1,
        name="Updated warehouse",
    )
    update_body.pop("datasource_id")
    updated = client.put(
        "/admin/datasources/warehouse",
        json=update_body,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated warehouse"
    assert updated.json()["revision"] == 2

    stale = client.put(
        "/admin/datasources/warehouse",
        json=update_body,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "datasource_conflict"

    deleted = client.delete(
        "/admin/datasources/warehouse"
        "?project_id=project-a&expected_revision=2"
    )
    assert deleted.status_code == 200


@pytest.mark.parametrize(
    "role",
    [TenantRole.TENANT_MEMBER, TenantRole.TENANT_AUDITOR],
)
def test_member_and_auditor_can_read_but_not_write(
    role: TenantRole,
) -> None:
    admin, repository = _client(TenantRole.TENANT_ADMIN)
    assert admin.post(
        "/admin/datasources",
        json=_body(),
    ).status_code == 201
    reader, _ = _client(role, repository)

    listed = reader.get(
        "/admin/datasources?project_id=project-a"
    )
    fetched = reader.get(
        "/admin/datasources/warehouse?project_id=project-a"
    )

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["datasources"][0]["role_configured"] is True
    assert "role_arn" not in listed.json()["datasources"][0]
    assert fetched.status_code == 200
    assert fetched.json()["role_configured"] is True
    assert reader.post(
        "/admin/datasources",
        json=_body(datasource_id="blocked"),
    ).status_code == 403
    assert reader.put(
        "/admin/datasources/warehouse",
        json={},
    ).status_code == 403
    assert reader.delete(
        "/admin/datasources/warehouse"
        "?project_id=project-a&expected_revision=1"
    ).status_code == 403


def test_service_principal_has_no_control_plane_access() -> None:
    client, _ = _client(TenantRole.SERVICE)

    assert client.get("/admin/datasources").status_code == 403
    assert client.post(
        "/admin/datasources",
        json=_body(),
    ).status_code == 403


@pytest.mark.parametrize(
    "system_field",
    ["revision", "created_at", "updated_at"],
)
def test_admin_create_rejects_client_owned_system_fields(
    system_field: str,
) -> None:
    client, _ = _client(TenantRole.TENANT_ADMIN)

    response = client.post(
        "/admin/datasources",
        json=_body(**{system_field: 99}),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == (
        "invalid_datasource_request"
    )


def test_admin_create_rejects_duplicate_json_fields() -> None:
    client, _ = _client(TenantRole.TENANT_ADMIN)
    body = json.dumps(_body())
    duplicate = body[:-1] + ',"name":"second name"}'

    response = client.post(
        "/admin/datasources",
        content=duplicate,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert "duplicate field" in response.json()["error"]["message"]


def test_admin_create_rejects_unapproved_role_binding() -> None:
    client, _ = _client(TenantRole.TENANT_ADMIN)

    response = client.post(
        "/admin/datasources",
        json=_body(
            role_arn=(
                "arn:aws:iam::123456789012:role/"
                "unapproved-athena-role"
            )
        ),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == (
        "role_binding_not_approved"
    )


def test_admin_datasource_cannot_cross_project_authority() -> None:
    client, _ = _client(TenantRole.TENANT_ADMIN)

    response = client.post(
        "/admin/datasources",
        json=_body(project_id="project-b"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_datasource_listing_is_cursor_bounded() -> None:
    client, _ = _client(TenantRole.TENANT_ADMIN)
    for datasource_id in ("alpha", "beta", "gamma"):
        assert client.post(
            "/admin/datasources",
            json=_body(datasource_id=datasource_id),
        ).status_code == 201

    first = client.get(
        "/admin/datasources?project_id=project-a&limit=2"
    )
    assert first.status_code == 200
    assert [item["datasource_id"] for item in first.json()["datasources"]] == [
        "alpha",
        "beta",
    ]
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str)
    second = client.get(
        "/admin/datasources",
        params={
            "project_id": "project-a",
            "limit": "2",
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert [item["datasource_id"] for item in second.json()["datasources"]] == [
        "gamma"
    ]
    assert second.json()["next_cursor"] is None
    invalid = client.get(
        "/admin/datasources?project_id=project-a&cursor=invalid"
    )
    assert invalid.status_code == 400


def test_datasource_mutations_are_durably_audited_without_role_arn() -> None:
    audit = _AuditTrail()
    client, _ = _client(TenantRole.TENANT_ADMIN, audit=audit)
    headers = {"x-request-id": "datasource-request-123"}

    created = client.post(
        "/admin/datasources",
        json=_body(),
        headers=headers,
    )
    update_body = _body(
        expected_revision=1,
        name="Updated warehouse",
    )
    update_body.pop("datasource_id")
    updated = client.put(
        "/admin/datasources/warehouse",
        json=update_body,
        headers=headers,
    )
    deleted = client.delete(
        "/admin/datasources/warehouse"
        "?project_id=project-a&expected_revision=2",
        headers=headers,
    )

    assert [created.status_code, updated.status_code, deleted.status_code] == [
        201,
        200,
        200,
    ]
    assert [record["event_type"] for record in audit.records] == [
        AuditEventType.DATASOURCE_MUTATION_REQUEST,
        AuditEventType.DATASOURCE_MUTATION_RESULT,
        AuditEventType.DATASOURCE_MUTATION_REQUEST,
        AuditEventType.DATASOURCE_MUTATION_RESULT,
        AuditEventType.DATASOURCE_MUTATION_REQUEST,
        AuditEventType.DATASOURCE_MUTATION_RESULT,
    ]
    assert [audit.records[index]["data"]["operation"] for index in (0, 2, 4)] == [
        "create",
        "update",
        "delete",
    ]
    assert all(
        record["request_id"] == "datasource-request-123"
        for record in audit.records
    )
    assert ROLE_ARN not in repr(audit.records)
    assert "role_binding" in audit.records[0]["data"]["changes"]


def test_datasource_mutation_fails_closed_when_audit_is_unavailable() -> None:
    audit = _AuditTrail(error=RuntimeError("audit unavailable"))
    client, repository = _client(
        TenantRole.TENANT_ADMIN,
        audit=audit,
    )

    response = client.post("/admin/datasources", json=_body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "datasource_audit_unavailable"
    )
    assert (
        repository._items == {}
    )


def test_datasource_create_enforces_tenant_quota() -> None:
    repository = InMemoryDatasourceRepository(
        max_datasources_per_tenant=1
    )
    client, _ = _client(TenantRole.TENANT_ADMIN, repository)

    assert client.post(
        "/admin/datasources",
        json=_body(datasource_id="first"),
    ).status_code == 201
    exceeded = client.post(
        "/admin/datasources",
        json=_body(datasource_id="second"),
    )

    assert exceeded.status_code == 409
    assert exceeded.json()["error"]["code"] == (
        "datasource_quota_exceeded"
    )
