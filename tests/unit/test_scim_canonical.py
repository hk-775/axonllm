"""Tenant and transaction tests for canonical SCIM provisioning."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.auth.principal import CredentialIdentity
from src.gateway.auth.scim_routes import ScimAPI, create_scim_routes
from src.gateway.auth.scim_service import ScimConflictError, ScimStore
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    ScimGroup,
    ScimUser,
    TenantRole,
)
from src.gateway.persistence import DynamoPersistence


class _TransactionCanceled(RuntimeError):
    def __init__(self, item_count: int, failed_index: int) -> None:
        reasons = [{"Code": "None"} for _ in range(item_count)]
        reasons[failed_index] = {"Code": "ConditionalCheckFailed"}
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": reasons,
        }
        super().__init__("transaction condition failed")


class _TransactionalClient:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.transactions: list[dict] = []
        self.scim_version_reads: dict[str, int] = {}
        self.scim_snapshot_reads: dict[str, int] = {}
        self.fail_at: int | None = None
        self.conditional_fail_at: int | None = None
        self.fail_reads = False
        self._lock = threading.Lock()
        self._deserializer = TypeDeserializer()

    def _decode(self, values: dict) -> dict:
        return {name: self._deserializer.deserialize(value) for name, value in values.items()}

    @staticmethod
    def _key(item: dict) -> tuple[str, str]:
        return item["PK"], item["SK"]

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
                    if condition.startswith("attribute_not_exists"):
                        if current is not None:
                            raise _TransactionCanceled(len(items), index)
                    elif condition == "authorization_version = :expected":
                        values = self._decode(put["ExpressionAttributeValues"])
                        if current is None or current.get("authorization_version") != values[":expected"]:
                            raise _TransactionCanceled(len(items), index)
                    elif condition == (
                        "entity_type = :entity_type "
                        "AND tenant_id = :tenant_id "
                        "AND members = :members"
                    ):
                        values = self._decode(
                            put["ExpressionAttributeValues"]
                        )
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
                values = self._decode(delete["ExpressionAttributeValues"])
                current = staged.get(key)
                if current is None or current.get("user_id") != values[":user_id"]:
                    raise _TransactionCanceled(len(items), index)
                staged.pop(key)
            self.rows = staged


class _Table:
    def __init__(self, client: _TransactionalClient) -> None:
        self.meta = SimpleNamespace(client=client)
        self._client = client

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        if Key["SK"].startswith("SCIM#") or Key["SK"].startswith(
            "TENANT#"
        ):
            assert ConsistentRead is True
        if self._client.fail_reads:
            raise RuntimeError("injected read failure")
        if Key["SK"] == "SCIM#VERSION":
            tenant_id = Key["PK"].removeprefix("TENANT#")
            self._client.scim_version_reads[tenant_id] = (
                self._client.scim_version_reads.get(tenant_id, 0) + 1
            )
        item = self._client.rows.get((Key["PK"], Key["SK"]))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def query(
        self,
        *,
        KeyConditionExpression,  # noqa: N803
        ConsistentRead=False,  # noqa: N803
        **_kwargs,
    ):
        assert ConsistentRead is True
        if self._client.fail_reads:
            raise RuntimeError("injected read failure")
        equals, begins_with = KeyConditionExpression._values
        partition = equals._values[1]
        prefix = begins_with._values[1]
        tenant_id = partition.removeprefix("TENANT#")
        self._client.scim_snapshot_reads[tenant_id] = (
            self._client.scim_snapshot_reads.get(tenant_id, 0) + 1
        )
        items = [
            copy.deepcopy(row)
            for (pk, sk), row in sorted(self._client.rows.items())
            if pk == partition and sk.startswith(prefix)
        ]
        return {"Items": items}


class _Persistence(DynamoPersistence):
    def __init__(self, client: _TransactionalClient) -> None:
        super().__init__(table_name="scim-test")
        self._enabled = True
        self._table = _Table(client)
        self._client = client


def _strict_store(
    client: _TransactionalClient,
) -> tuple[ScimStore, _Persistence]:
    persistence = _Persistence(client)
    return (
        ScimStore(
            persistence,
            canonical_identity_required=True,
        ),
        persistence,
    )


def _user(
    *,
    tenant_id: str = "tenant-a",
    subject: str = "subject-a",
    user_name: str = "user@example.test",
) -> ScimUser:
    return ScimUser(
        id="",
        user_name=user_name,
        tenant_id=tenant_id,
        issuer="https://idp.example.test",
        subject=subject,
        external_id=subject,
        project_id="project-a",
    )


async def test_create_updates_canonical_principal_atomically() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)

    user = await store.create_user(_user())
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=user.issuer,
            subject=user.subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=user.tenant_id,
            project_hint=user.project_id,
        )
    )

    assert principal is not None
    assert principal.principal_id == f"scim:{user.id}"
    assert principal.roles == frozenset({TenantRole.TENANT_MEMBER})
    assert len(client.transactions[0]["TransactItems"]) == 4


async def test_deprovision_failure_leaves_memory_and_dynamo_unchanged() -> None:
    client = _TransactionalClient()
    store, _ = _strict_store(client)
    user = await store.create_user(_user())
    before = copy.deepcopy(client.rows)
    client.fail_at = 1

    with pytest.raises(RuntimeError, match="SCIM identity transaction failed"):
        await store.set_user_active(user.id, False, user.tenant_id)

    assert store.get_user(user.id, user.tenant_id).active is True
    assert client.rows == before


async def test_group_role_change_versions_affected_principal() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)
    user = await store.create_user(_user())

    group = await store.create_group(
        ScimGroup(
            id="",
            display_name="tenant administrators",
            tenant_id=user.tenant_id,
            members=[user.id],
            roles=[TenantRole.TENANT_ADMIN.value],
        )
    )
    updated = store.get_user(user.id, user.tenant_id)
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=user.issuer,
            subject=user.subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=user.tenant_id,
        )
    )

    assert updated.authorization_version == 2
    assert principal is not None
    assert principal.roles == frozenset({TenantRole.TENANT_ADMIN})
    assert principal.authorization_version == 2
    assert len(client.transactions[-1]["TransactItems"]) == 4

    await store.delete_group(group.id, user.tenant_id)
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=user.issuer,
            subject=user.subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=user.tenant_id,
        )
    )
    assert principal is not None
    assert principal.roles == frozenset({TenantRole.TENANT_MEMBER})
    assert principal.authorization_version == 3


async def test_canonical_user_groups_cannot_preserve_removed_group_roles() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)
    group = await store.create_group(
        ScimGroup(
            id="group-admins",
            display_name="tenant administrators",
            tenant_id="tenant-a",
            roles=[TenantRole.TENANT_ADMIN.value],
        )
    )
    candidate = _user()
    candidate.groups = [group.id]
    user = await store.create_user(candidate)

    assert user.groups == []
    assert store.groups_for_user(user) == []

    await store.replace_group(
        group.id,
        ScimGroup(
            id="",
            display_name=group.display_name,
            tenant_id=user.tenant_id,
            members=[user.id],
            roles=list(group.roles),
        ),
        user.tenant_id,
    )
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=user.issuer,
            subject=user.subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=user.tenant_id,
        )
    )
    assert principal is not None
    assert principal.roles == frozenset({TenantRole.TENANT_ADMIN})

    await store.replace_group(
        group.id,
        ScimGroup(
            id="",
            display_name=group.display_name,
            tenant_id=user.tenant_id,
            roles=list(group.roles),
        ),
        user.tenant_id,
    )
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=user.issuer,
            subject=user.subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=user.tenant_id,
        )
    )
    assert principal is not None
    assert principal.roles == frozenset({TenantRole.TENANT_MEMBER})


async def test_username_uniqueness_is_enforced_across_instances() -> None:
    client = _TransactionalClient()
    first, _ = _strict_store(client)
    second, _ = _strict_store(client)
    await first.create_user(_user(subject="subject-a"))
    before = copy.deepcopy(client.rows)

    with pytest.raises(ScimConflictError):
        await second.create_user(_user(subject="subject-b"))

    assert client.rows == before


async def test_real_tenant_snapshot_contract_distinguishes_empty_and_failed_reads() -> None:
    client = _TransactionalClient()
    persistence = _Persistence(client)

    assert await persistence.get_tenant_scim_version("tenant-a") == 0
    assert await persistence.load_tenant_scim_snapshot_or_none(
        "tenant-a"
    ) == ([], [])

    client.fail_reads = True
    assert await persistence.get_tenant_scim_version("tenant-a") is None
    assert (
        await persistence.load_tenant_scim_snapshot_or_none("tenant-a")
        is None
    )


async def test_scim_mutations_advance_the_real_version_in_the_transaction() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)

    user = await store.create_user(_user())
    assert await persistence.get_tenant_scim_version("tenant-a") == 1

    await store.set_user_active(user.id, False, "tenant-a")
    assert await persistence.get_tenant_scim_version("tenant-a") == 2

    await store.create_group(
        ScimGroup(
            id="group-a",
            display_name="Readers",
            tenant_id="tenant-a",
        )
    )
    assert await persistence.get_tenant_scim_version("tenant-a") == 3


async def test_failed_scim_transaction_does_not_advance_version() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)
    client.fail_at = 3

    with pytest.raises(RuntimeError, match="transaction failed"):
        await store.create_user(_user())

    assert client.rows == {}
    client.fail_at = None
    assert await persistence.get_tenant_scim_version("tenant-a") == 0


async def test_real_tenant_snapshot_never_returns_a_neighbor_directory() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)
    tenant_a = await store.create_user(_user(tenant_id="tenant-a"))
    tenant_b = await store.create_user(
        _user(
            tenant_id="tenant-b",
            subject="subject-b",
            user_name="user-b@example.test",
        )
    )

    users_a, groups_a = (
        await persistence.load_tenant_scim_snapshot_or_none("tenant-a")
    )
    users_b, groups_b = (
        await persistence.load_tenant_scim_snapshot_or_none("tenant-b")
    )

    assert [user.id for user in users_a] == [tenant_a.id]
    assert [user.id for user in users_b] == [tenant_b.id]
    assert groups_a == groups_b == []


async def test_group_transaction_retains_the_dynamodb_operation_limit() -> None:
    client = _TransactionalClient()
    persistence = _Persistence(client)
    group = ScimGroup(
        id="group-a",
        display_name="Too large",
        tenant_id="tenant-a",
    )

    with pytest.raises(ValueError, match="at most 49"):
        await persistence.save_scim_group_with_principals(
            group,
            expected_authorization_version=None,
            previous_group=None,
            user_updates=[None] * 50,
        )


@pytest.fixture
def strict_client(monkeypatch):
    client = _TransactionalClient()
    store, _ = _strict_store(client)
    credentials = {
        "tenant-a": {
            "issuer": "https://idp.example.test",
            "token": "tenant-a-secret",
        },
        "tenant-b": {
            "issuer": "https://idp.example.test",
            "token": "tenant-b-secret",
        },
    }
    monkeypatch.setenv("AXON_SCIM_TENANTS", json.dumps(credentials))
    monkeypatch.delenv("AXON_SCIM_TOKEN", raising=False)
    app = Starlette(routes=create_scim_routes(ScimAPI(store, canonical_identity_required=True)))
    return TestClient(app), store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_tenant_tokens_isolate_same_username(strict_client) -> None:
    client, store = strict_client
    payload = {
        "userName": "same@example.test",
        "externalId": "shared-subject",
        "projectId": "shared-project",
    }

    tenant_a = client.post(
        "/scim/v2/Users",
        headers=_auth("tenant-a-secret"),
        json=payload,
    )
    tenant_b = client.post(
        "/scim/v2/Users",
        headers=_auth("tenant-b-secret"),
        json=payload,
    )

    assert tenant_a.status_code == 201
    assert tenant_b.status_code == 201
    assert tenant_a.json()["id"] != tenant_b.json()["id"]
    assert (
        client.get(
            f"/scim/v2/Users/{tenant_a.json()['id']}",
            headers=_auth("tenant-b-secret"),
        ).status_code
        == 404
    )
    assert (
        store.get_user_by_username(
            "same@example.test",
            "tenant-a",
        )
        is not None
    )
    assert (
        store.get_user_by_username(
            "same@example.test",
            "tenant-b",
        )
        is not None
    )


def test_strict_scim_requires_subject_and_canonical_roles(strict_client) -> None:
    client, _ = strict_client

    missing_subject = client.post(
        "/scim/v2/Users",
        headers=_auth("tenant-a-secret"),
        json={"userName": "missing@example.test"},
    )
    invalid_role = client.post(
        "/scim/v2/Users",
        headers=_auth("tenant-a-secret"),
        json={
            "userName": "invalid@example.test",
            "externalId": "subject-invalid",
            "roles": [{"value": "platform_admin"}],
        },
    )

    assert missing_subject.status_code == 400
    assert invalid_role.status_code == 400


def test_global_token_is_rejected_in_strict_mode(
    monkeypatch,
    strict_client,
) -> None:
    client, _ = strict_client
    monkeypatch.delenv("AXON_SCIM_TENANTS")
    monkeypatch.setenv("AXON_SCIM_TOKEN", "legacy-secret")

    response = client.get(
        "/scim/v2/Users",
        headers=_auth("legacy-secret"),
    )

    assert response.status_code == 503
    assert "tenant-bound" in response.json()["detail"]


@pytest.mark.parametrize(
    "credentials",
    [
        json.dumps(
            {
                "tenant-a": {
                    "issuer": "https://idp.example.test",
                    "token": "first",
                },
                " tenant-a ": {
                    "issuer": "https://idp.example.test",
                    "token": "second",
                },
            }
        ),
        (
            '{"tenant-a":{"issuer":"https://idp.example.test","token":"first"},'
            '"tenant-a":{"issuer":"https://idp.example.test","token":"second"}}'
        ),
    ],
)
def test_scim_rejects_ambiguous_tenant_credential_keys(
    monkeypatch,
    strict_client,
    credentials,
) -> None:
    client, _ = strict_client
    monkeypatch.setenv("AXON_SCIM_TENANTS", credentials)

    response = client.get(
        "/scim/v2/Users",
        headers=_auth("first"),
    )

    assert response.status_code == 503


def test_deprovisioned_principal_is_not_resolved() -> None:
    client = _TransactionalClient()
    store, persistence = _strict_store(client)
    user = asyncio.run(store.create_user(_user()))

    asyncio.run(store.set_user_active(user.id, False, user.tenant_id))
    principal = asyncio.run(
        DynamoPrincipalRepository(persistence).resolve(
            CredentialIdentity(
                issuer=user.issuer,
                subject=user.subject,
                auth_method=AuthMethod.OIDC_JWT,
                tenant_hint=user.tenant_id,
            )
        )
    )

    assert principal is None
    stored = store.get_user(user.id, user.tenant_id)
    assert stored.active is False
    assert stored.authorization_version == 2
    principal_rows = [row for row in client.rows.values() if row.get("entity_type") == "tenant_principal"]
    assert principal_rows[0]["membership_status"] == (MembershipStatus.DEPROVISIONED.value)
