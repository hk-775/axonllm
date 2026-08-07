"""Tenant-isolation tests for canonical DynamoDB principal storage."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
    PrincipalConflictError,
    PrincipalStoreUnavailable,
    identity_partition_key,
    membership_sort_key,
)
from src.gateway.auth.principal import CredentialIdentity
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)


ISSUER = "https://idp.example.test"
SUBJECT = "external-user"


class _ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _Table:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.get_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.fail_reads = False
        self.fail_writes = False

    def put_item(self, **kwargs):
        if self.fail_writes:
            raise RuntimeError("write unavailable")
        item = deepcopy(kwargs["Item"])
        key = (item["PK"], item["SK"])
        existing = self.items.get(key)
        condition = kwargs["ConditionExpression"]
        if condition.startswith("attribute_not_exists"):
            if existing is not None:
                raise _ConditionalFailure()
        elif condition == "authorization_version = :expected_version":
            expected = kwargs["ExpressionAttributeValues"][":expected_version"]
            if existing is None or existing["authorization_version"] != expected:
                raise _ConditionalFailure()
        self.items[key] = item
        return {}

    def get_item(self, **kwargs):
        if self.fail_reads:
            raise RuntimeError("read unavailable")
        self.get_calls.append(deepcopy(kwargs))
        key = kwargs["Key"]
        item = self.items.get((key["PK"], key["SK"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def query(self, **kwargs):
        if self.fail_reads:
            raise RuntimeError("query unavailable")
        self.query_calls.append(deepcopy(kwargs))
        partition = kwargs["ExpressionAttributeValues"][":identity_pk"]
        items = [
            deepcopy(item)
            for (pk, _), item in self.items.items()
            if pk == partition
        ]
        return {"Items": items}


class _Persistence:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.table = _Table()

    def _get_table(self) -> _Table:
        return self.table


def _principal(
    tenant_id: str,
    *,
    version: int = 1,
    status: MembershipStatus = MembershipStatus.ACTIVE,
    role: TenantRole = TenantRole.TENANT_MEMBER,
) -> Principal:
    return Principal(
        principal_id=f"principal:{tenant_id}",
        tenant_id=tenant_id,
        subject=SUBJECT,
        issuer=ISSUER,
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=status,
        project_ids=frozenset({"shared-project"}),
        authorization_version=version,
    )


def _identity(tenant_hint: str | None) -> CredentialIdentity:
    return CredentialIdentity(
        issuer=ISSUER,
        subject=SUBJECT,
        auth_method=AuthMethod.OIDC_JWT,
        tenant_hint=tenant_hint,
        project_hint="shared-project",
    )


async def test_same_identity_and_project_ids_remain_tenant_isolated() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    tenant_a = _principal("tenant-a")
    tenant_b = _principal("tenant-b")
    await repository.put(tenant_a)
    await repository.put(tenant_b)

    assert await repository.resolve(_identity("tenant-a")) == tenant_a
    assert await repository.resolve(_identity("tenant-b")) == tenant_b
    assert persistence.table.get_calls[-1]["ConsistentRead"] is True


async def test_no_tenant_hint_denies_an_ambiguous_identity() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    await repository.put(_principal("tenant-a"))
    await repository.put(_principal("tenant-b"))

    assert await repository.resolve(_identity(None)) is None
    assert persistence.table.query_calls[-1]["ConsistentRead"] is True


async def test_no_tenant_hint_accepts_exactly_one_active_membership() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    active = _principal("tenant-a")
    await repository.put(active)
    await repository.put(
        _principal("tenant-b", status=MembershipStatus.DEPROVISIONED)
    )

    assert await repository.resolve(_identity(None)) == active


async def test_missing_or_inactive_tenant_membership_denies() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    await repository.put(
        _principal("tenant-a", status=MembershipStatus.SUSPENDED)
    )

    assert await repository.resolve(_identity("tenant-a")) is None
    assert await repository.resolve(_identity("tenant-b")) is None


async def test_authorization_version_update_is_optimistic() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    await repository.put(_principal("tenant-a", version=1))
    updated = _principal(
        "tenant-a",
        version=2,
        role=TenantRole.TENANT_ADMIN,
    )

    await repository.put(updated, expected_authorization_version=1)
    assert await repository.resolve(_identity("tenant-a")) == updated

    with pytest.raises(PrincipalConflictError):
        await repository.put(
            _principal("tenant-a", version=2),
            expected_authorization_version=1,
        )


async def test_duplicate_create_is_rejected() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    await repository.put(_principal("tenant-a"))

    with pytest.raises(PrincipalConflictError):
        await repository.put(_principal("tenant-a"))


async def test_disabled_store_and_outages_fail_closed() -> None:
    disabled = DynamoPrincipalRepository(_Persistence(enabled=False))
    with pytest.raises(PrincipalStoreUnavailable):
        await disabled.resolve(_identity("tenant-a"))
    with pytest.raises(PrincipalStoreUnavailable):
        await disabled.put(_principal("tenant-a"))

    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    persistence.table.fail_reads = True
    with pytest.raises(PrincipalStoreUnavailable):
        await repository.resolve(_identity("tenant-a"))
    persistence.table.fail_reads = False
    persistence.table.fail_writes = True
    with pytest.raises(PrincipalStoreUnavailable):
        await repository.put(_principal("tenant-a"))


def test_keys_are_tenant_qualified_and_hide_raw_identity() -> None:
    partition = identity_partition_key(ISSUER, SUBJECT)

    assert partition.startswith("IDENTITY#")
    assert ISSUER not in partition
    assert SUBJECT not in partition
    assert membership_sort_key("tenant-a") == "TENANT#tenant-a"


def test_malformed_authority_row_fails_closed() -> None:
    principal = _principal("tenant-a")
    item = DynamoPrincipalRepository.serialize(principal)
    item["roles"] = [{"admin": False}]

    with pytest.raises(ValueError):
        DynamoPrincipalRepository.deserialize(item)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "1"),
        ("principal_id", 123),
        ("tenant_id", True),
        ("subject", None),
        ("issuer", 123),
        ("auth_method", False),
        ("membership_status", 1),
        ("authorization_version", "1"),
        ("credential_id", 123),
        ("email", False),
        ("project_ids", [""]),
        ("scopes", [1]),
    ],
)
def test_malformed_scalar_types_are_rejected(field: str, value) -> None:
    item = DynamoPrincipalRepository.serialize(_principal("tenant-a"))
    item[field] = value

    with pytest.raises(ValueError):
        DynamoPrincipalRepository.deserialize(item)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "IDENTITY#wrong"),
        ("SK", "TENANT#tenant-b"),
    ],
)
async def test_identity_key_mismatch_is_reported_as_unavailable(
    field: str,
    value: str,
) -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    item = repository.serialize(_principal("tenant-a"))
    lookup_key = (item["PK"], item["SK"])
    item[field] = value
    persistence.table.items[lookup_key] = item

    with pytest.raises(PrincipalStoreUnavailable):
        await repository.resolve(_identity("tenant-a"))


async def test_malformed_stored_authority_is_reported_as_unavailable() -> None:
    persistence = _Persistence()
    repository = DynamoPrincipalRepository(persistence)
    item = repository.serialize(_principal("tenant-a"))
    item["roles"] = [{"admin": False}]
    persistence.table.items[(item["PK"], item["SK"])] = item

    with pytest.raises(PrincipalStoreUnavailable):
        await repository.resolve(_identity("tenant-a"))
