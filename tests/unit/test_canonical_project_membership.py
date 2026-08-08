"""Atomic tenant project membership and principal-grant tests."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.auth.principal import CredentialIdentity
from src.gateway.config import AppConfig
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import AuthMethod, Project
from src.gateway.persistence import (
    CanonicalMembershipConflictError,
    CanonicalMembershipNotFoundError,
)
from tests.unit.test_scim_canonical import (
    _Persistence,
    _TransactionCanceled,
    _TransactionalClient,
    _strict_store,
    _user,
)


class _ProjectRevisionTransactionalClient(_TransactionalClient):
    """SCIM transaction fake with the project revision condition enabled."""

    def transact_write_items(self, **request) -> None:
        with self._lock:
            self.transactions.append(copy.deepcopy(request))
            items = request["TransactItems"]
            staged = copy.deepcopy(self.rows)
            for index, operation in enumerate(items):
                if self.fail_at == index:
                    raise RuntimeError(f"injected item {index} failure")
                if self.conditional_fail_at == index:
                    raise _TransactionCanceled(len(items), index)
                if "Put" in operation:
                    put = operation["Put"]
                    item = self._decode(put["Item"])
                    key = self._key(item)
                    current = staged.get(key)
                    condition = put["ConditionExpression"]
                    values = self._decode(
                        put.get("ExpressionAttributeValues", {})
                    )
                    if condition.startswith("attribute_not_exists"):
                        if current is not None:
                            raise _TransactionCanceled(len(items), index)
                    elif condition == "authorization_version = :expected":
                        if (
                            current is None
                            or current.get("authorization_version")
                            != values[":expected"]
                        ):
                            raise _TransactionCanceled(len(items), index)
                    elif "#revision = :expected_revision" in condition:
                        if current is None or any(
                            current.get(field) != values[token]
                            for field, token in (
                                ("entity_type", ":entity_type"),
                                ("tenant_id", ":tenant_id"),
                                ("project_id", ":project_id"),
                            )
                        ):
                            raise _TransactionCanceled(len(items), index)
                        revision = current.get("revision", 0)
                        if revision != values[":expected_revision"]:
                            raise _TransactionCanceled(len(items), index)
                    elif condition == (
                        "entity_type = :entity_type "
                        "AND tenant_id = :tenant_id "
                        "AND members = :members"
                    ):
                        if current is None or any(
                            current.get(field) != values[token]
                            for field, token in (
                                ("entity_type", ":entity_type"),
                                ("tenant_id", ":tenant_id"),
                                ("members", ":members"),
                            )
                        ):
                            raise _TransactionCanceled(len(items), index)
                    else:
                        raise AssertionError(condition)
                    staged[key] = item
                    continue
                if "Update" in operation:
                    update = operation["Update"]
                    key_values = self._decode(update["Key"])
                    key = self._key(key_values)
                    values = self._decode(
                        update["ExpressionAttributeValues"]
                    )
                    assert update["UpdateExpression"] == (
                        "SET entity_type = :entity_type, "
                        "tenant_id = :tenant_id ADD #version :one"
                    )
                    current = staged.get(key, dict(key_values))
                    current["entity_type"] = values[":entity_type"]
                    current["tenant_id"] = values[":tenant_id"]
                    current["version"] = (
                        current.get("version", 0) + values[":one"]
                    )
                    staged[key] = current
                    continue
                delete = operation["Delete"]
                key = self._key(self._decode(delete["Key"]))
                values = self._decode(
                    delete["ExpressionAttributeValues"]
                )
                current = staged.get(key)
                if (
                    current is None
                    or current.get("user_id") != values[":user_id"]
                ):
                    raise _TransactionCanceled(len(items), index)
                staged.pop(key)
            self.rows = staged


def _seed_project(
    persistence: _Persistence,
    client: _TransactionalClient,
    *,
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
) -> Project:
    project = Project(
        project_id=project_id,
        name=f"{tenant_id} project",
        tenant_id=tenant_id,
    )
    row = persistence.serialize_project(project)
    client.rows[(row["PK"], row["SK"])] = row
    return project


async def _principal(
    persistence: _Persistence,
    *,
    tenant_id: str,
    subject: str,
):
    return await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer="https://idp.example.test",
            subject=subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=tenant_id,
        )
    )


async def test_grant_and_revoke_update_every_authoritative_row() -> None:
    client = _ProjectRevisionTransactionalClient()
    store, persistence = _strict_store(client)
    _seed_project(persistence, client)
    user_input = _user()
    user_input.id = "user-a"
    user_input.project_id = ""
    user = await store.create_user(user_input)

    granted_project, changed = (
        await persistence.set_tenant_project_membership(
            "tenant-a",
            "project-a",
            user.id,
            granted=True,
        )
    )
    granted_principal = await _principal(
        persistence,
        tenant_id="tenant-a",
        subject=user.subject,
    )
    users, _groups = (
        await persistence.load_tenant_scim_snapshot_or_none("tenant-a")
    )

    assert changed is True
    assert granted_project.members == ["scim:user-a"]
    assert users[0].project_ids == ["project-a"]
    assert users[0].authorization_version == 2
    assert granted_principal.project_ids == frozenset({"project-a"})
    assert granted_principal.authorization_version == 2
    assert await persistence.get_tenant_scim_version("tenant-a") == 2

    revoked_project, changed = (
        await persistence.set_tenant_project_membership(
            "tenant-a",
            "project-a",
            user.id,
            granted=False,
        )
    )
    revoked_principal = await _principal(
        persistence,
        tenant_id="tenant-a",
        subject=user.subject,
    )

    assert changed is True
    assert revoked_project.members == []
    assert revoked_principal.project_ids == frozenset()
    assert revoked_principal.authorization_version == 3
    assert await persistence.get_tenant_scim_version("tenant-a") == 3


async def test_membership_cas_failure_rolls_back_all_rows_and_version() -> None:
    client = _ProjectRevisionTransactionalClient()
    store, persistence = _strict_store(client)
    _seed_project(persistence, client)
    user_input = _user()
    user_input.id = "user-a"
    user_input.project_id = ""
    await store.create_user(user_input)
    before = copy.deepcopy(client.rows)
    client.conditional_fail_at = 2

    with pytest.raises(CanonicalMembershipConflictError):
        await persistence.set_tenant_project_membership(
            "tenant-a",
            "project-a",
            "user-a",
            granted=True,
        )

    assert client.rows == before
    assert await persistence.get_tenant_scim_version("tenant-a") == 1


async def test_project_membership_lookup_cannot_cross_tenants() -> None:
    client = _ProjectRevisionTransactionalClient()
    store, persistence = _strict_store(client)
    _seed_project(persistence, client, tenant_id="tenant-a")
    user_input = _user(
        tenant_id="tenant-b",
        subject="subject-b",
        user_name="user-b@example.test",
    )
    user_input.id = "user-b"
    user_input.project_id = ""
    await store.create_user(user_input)
    before = copy.deepcopy(client.rows)

    with pytest.raises(CanonicalMembershipNotFoundError):
        await persistence.set_tenant_project_membership(
            "tenant-a",
            "project-a",
            "user-b",
            granted=True,
        )

    assert client.rows == before


class _TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.context = SimpleNamespace(tenant_id="tenant-a")
        return await call_next(request)


def _admin_client(persistence: _Persistence) -> TestClient:
    api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        persistence=persistence,
        app_config=AppConfig(canonical_identity_required=True),
    )
    app = Starlette(routes=create_admin_routes(api))
    app.add_middleware(_TenantContextMiddleware)
    return TestClient(app)


def test_admin_member_routes_use_the_canonical_transaction() -> None:
    client_backend = _ProjectRevisionTransactionalClient()
    store, persistence = _strict_store(client_backend)
    _seed_project(persistence, client_backend)
    user_input = _user()
    user_input.id = "user-a"
    user_input.project_id = ""
    asyncio.run(store.create_user(user_input))
    client = _admin_client(persistence)

    granted = client.post(
        "/admin/projects/project-a/members",
        json={"user_id": "user-a"},
    )
    bulk_bypass = client.put(
        "/admin/projects/project-a",
        json={"members": []},
    )
    revoked = client.delete(
        "/admin/projects/project-a/members/scim:user-a"
    )

    assert granted.status_code == 200
    assert granted.json()["user_id"] == "scim:user-a"
    assert bulk_bypass.status_code == 400
    assert revoked.status_code == 200


def test_admin_member_route_publishes_returned_revision_without_mutating_stale_read() -> None:
    client_backend = _ProjectRevisionTransactionalClient()
    store, persistence = _strict_store(client_backend)
    _seed_project(persistence, client_backend)
    user_input = _user()
    user_input.id = "user-a"
    user_input.project_id = ""
    asyncio.run(store.create_user(user_input))

    loaded: list[Project] = []
    original_get = persistence.get_project

    async def capture_get(project_id, tenant_id=None):
        project = await original_get(project_id, tenant_id)
        if project is not None:
            loaded.append(project)
        return project

    persistence.get_project = capture_get
    response = _admin_client(persistence).post(
        "/admin/projects/project-a/members",
        json={"user_id": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert response.headers["etag"] == '"1"'
    assert loaded[0].members == []
    assert loaded[0].revision == 0
    stored = asyncio.run(original_get("project-a", "tenant-a"))
    assert stored is not None
    assert stored.members == ["scim:user-a"]
    assert stored.revision == 1
