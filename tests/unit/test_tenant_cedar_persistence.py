"""Real tenant Cedar persistence and admin-route isolation tests."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.routes import AdminAPI, create_admin_routes
from src.gateway.auth.cedar_policy import CedarPolicyService
from src.gateway.cost_tracker import CostTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.persistence import DynamoPersistence

PERMIT_READ = 'permit(principal, action == Action::"read", resource);'
FORBID_WRITE = 'forbid(principal, action == Action::"write", resource);'


class _CedarClient:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.transactions: list[dict] = []
        self.fail_transaction = False
        self.fail_reads = False
        self._deserializer = TypeDeserializer()

    def _decode(self, values: dict) -> dict:
        return {
            name: self._deserializer.deserialize(value)
            for name, value in values.items()
        }

    def transact_write_items(self, **request) -> None:
        self.transactions.append(copy.deepcopy(request))
        if self.fail_transaction:
            raise RuntimeError("injected transaction failure")
        staged = copy.deepcopy(self.rows)
        for operation in request["TransactItems"]:
            if "Put" in operation:
                item = self._decode(operation["Put"]["Item"])
                staged[(item["PK"], item["SK"])] = item
                continue
            update = operation["Update"]
            key = self._decode(update["Key"])
            values = self._decode(update["ExpressionAttributeValues"])
            row_key = (key["PK"], key["SK"])
            row = staged.get(row_key, dict(key))
            row["entity_type"] = values[":entity_type"]
            row["tenant_id"] = values[":tenant_id"]
            row["version"] = (
                row.get("version", 0) + values[":one"]
            )
            staged[row_key] = row
        self.rows = staged


class _CedarTable:
    def __init__(self, client: _CedarClient) -> None:
        self.meta = SimpleNamespace(client=client)
        self._client = client

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead is True
        if self._client.fail_reads:
            raise RuntimeError("injected read failure")
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
        return {
            "Items": [
                copy.deepcopy(row)
                for (pk, sk), row in sorted(self._client.rows.items())
                if pk == partition and sk.startswith(prefix)
            ]
        }


class _CedarPersistence(DynamoPersistence):
    def __init__(self, client: _CedarClient) -> None:
        super().__init__(table_name="cedar-test")
        self._enabled = True
        self._table = _CedarTable(client)


async def test_tenant_cedar_empty_and_failed_reads_are_distinct() -> None:
    client = _CedarClient()
    persistence = _CedarPersistence(client)

    assert (
        await persistence.get_tenant_cedar_policy_version("tenant-a")
        == 0
    )
    assert (
        await persistence.load_tenant_cedar_policies_or_none("tenant-a")
        == []
    )

    client.fail_reads = True
    assert (
        await persistence.get_tenant_cedar_policy_version("tenant-a")
        is None
    )
    assert (
        await persistence.load_tenant_cedar_policies_or_none("tenant-a")
        is None
    )


async def test_policy_and_version_are_atomic_and_tenant_qualified() -> None:
    client = _CedarClient()
    persistence = _CedarPersistence(client)

    await persistence.save_tenant_cedar_policy(
        "tenant-a",
        {
            "name": "guard",
            "policy_text": FORBID_WRITE,
            "mode": "ENFORCE",
        },
    )
    await persistence.save_tenant_cedar_policy(
        "tenant-b",
        {
            "name": "guard",
            "policy_text": PERMIT_READ,
            "mode": "ENFORCE",
        },
    )

    policies_a = await persistence.load_tenant_cedar_policies_or_none(
        "tenant-a"
    )
    policies_b = await persistence.load_tenant_cedar_policies_or_none(
        "tenant-b"
    )
    assert [policy["policy_text"] for policy in policies_a] == [
        FORBID_WRITE
    ]
    assert [policy["policy_text"] for policy in policies_b] == [
        PERMIT_READ
    ]
    assert policies_a[0]["tenant_id"] == "tenant-a"
    assert policies_b[0]["tenant_id"] == "tenant-b"
    assert (
        await persistence.get_tenant_cedar_policy_version("tenant-a")
        == 1
    )
    assert (
        await persistence.get_tenant_cedar_policy_version("tenant-b")
        == 1
    )
    assert all(
        len(transaction["TransactItems"]) == 2
        for transaction in client.transactions
    )


async def test_failed_policy_transaction_does_not_publish_a_version() -> None:
    client = _CedarClient()
    persistence = _CedarPersistence(client)
    client.fail_transaction = True

    try:
        await persistence.save_tenant_cedar_policy(
            "tenant-a",
            {
                "name": "guard",
                "policy_text": FORBID_WRITE,
                "mode": "ENFORCE",
            },
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed tenant Cedar transaction returned success")

    assert client.rows == {}
    client.fail_transaction = False
    assert (
        await persistence.get_tenant_cedar_policy_version("tenant-a")
        == 0
    )


class _TenantContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, tenant_id: str) -> None:
        super().__init__(app)
        self._tenant_id = tenant_id

    async def dispatch(self, request, call_next):
        request.state.context = SimpleNamespace(
            tenant_id=self._tenant_id
        )
        return await call_next(request)


def _admin_client(
    persistence: _CedarPersistence,
    tenant_id: str,
) -> TestClient:
    service = CedarPolicyService([], persistence=persistence)
    api = AdminAPI(
        cost_tracker=CostTracker(pricing_config={}),
        health_tracker=ProviderHealthTracker(),
        model_registry=ModelRegistry(),
        persistence=persistence,
        policy_service=service,
    )
    app = Starlette(routes=create_admin_routes(api))
    app.add_middleware(
        _TenantContextMiddleware,
        tenant_id=tenant_id,
    )
    return TestClient(app)


def test_policy_admin_routes_do_not_cross_tenant_boundaries() -> None:
    backend = _CedarClient()
    persistence = _CedarPersistence(backend)
    tenant_a = _admin_client(persistence, "tenant-a")
    tenant_b = _admin_client(persistence, "tenant-b")

    created_a = tenant_a.post(
        "/admin/policies",
        json={
            "name": "guard",
            "policy_text": FORBID_WRITE,
            "mode": "ENFORCE",
        },
    )
    before_b = tenant_b.get("/admin/policies")
    created_b = tenant_b.post(
        "/admin/policies",
        json={
            "name": "guard",
            "policy_text": PERMIT_READ,
            "mode": "ENFORCE",
        },
    )
    listed_a = tenant_a.get("/admin/policies")
    listed_b = tenant_b.get("/admin/policies")

    assert created_a.status_code == 201
    assert before_b.json() == []
    assert created_b.status_code == 201
    assert listed_a.json()[0]["policy_text"] == FORBID_WRITE
    assert listed_b.json()[0]["policy_text"] == PERMIT_READ
    assert listed_a.json()[0]["tenant_id"] == "tenant-a"
    assert listed_b.json()[0]["tenant_id"] == "tenant-b"


class _PolicyRequest:
    def __init__(self, tenant_id: str, body: dict) -> None:
        self.state = SimpleNamespace(
            context=SimpleNamespace(tenant_id=tenant_id)
        )
        self._body = body

    async def json(self) -> dict:
        return self._body


async def test_concurrent_policy_writers_reload_the_authoritative_snapshot() -> None:
    """Neither writer may associate its pre-write list with the latest version."""

    client = _CedarClient()

    class _BarrierPersistence(_CedarPersistence):
        def __init__(self, backend: _CedarClient) -> None:
            super().__init__(backend)
            self.completed = 0
            self.all_completed = asyncio.Event()
            self.write_lock = asyncio.Lock()

        async def save_tenant_cedar_policy(
            self,
            tenant_id: str,
            policy: dict,
        ) -> int | None:
            async with self.write_lock:
                version = await super().save_tenant_cedar_policy(
                    tenant_id,
                    policy,
                )
            self.completed += 1
            if self.completed == 2:
                self.all_completed.set()
            await self.all_completed.wait()
            return version

    persistence = _BarrierPersistence(client)

    def _api() -> tuple[AdminAPI, CedarPolicyService]:
        service = CedarPolicyService([], persistence=persistence)
        return (
            AdminAPI(
                cost_tracker=CostTracker(pricing_config={}),
                health_tracker=ProviderHealthTracker(),
                model_registry=ModelRegistry(),
                persistence=persistence,
                policy_service=service,
            ),
            service,
        )

    api_a, service_a = _api()
    api_b, service_b = _api()
    response_a, response_b = await asyncio.gather(
        api_a.create_policy(
            _PolicyRequest(
                "tenant-a",
                {
                    "name": "deny-writes",
                    "policy_text": FORBID_WRITE,
                    "mode": "ENFORCE",
                },
            )
        ),
        api_b.create_policy(
            _PolicyRequest(
                "tenant-a",
                {
                    "name": "permit-reads",
                    "policy_text": PERMIT_READ,
                    "mode": "ENFORCE",
                },
            )
        ),
    )

    assert response_a.status_code == 201
    assert response_b.status_code == 201
    assert {
        policy["name"]
        for policy in service_a.policies_for_scope("tenant-a")
    } == {"deny-writes", "permit-reads"}
    assert {
        policy["name"]
        for policy in service_b.policies_for_scope("tenant-a")
    } == {"deny-writes", "permit-reads"}
    assert service_a._tenant_known_versions["tenant-a"] == 2
    assert service_b._tenant_known_versions["tenant-a"] == 2
