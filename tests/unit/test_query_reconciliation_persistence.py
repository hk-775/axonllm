"""DynamoDB query reconciliation concurrency and retry contracts."""

from __future__ import annotations

import asyncio
import copy
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer

from src.gateway.persistence import DynamoPersistence
from src.gateway.query.reconciliation import (
    QueryLifecycleClaim,
    QueryTerminalAudit,
)


class _ConditionalFailure(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "ConditionalCheckFailedException"}
        }
        super().__init__("conditional check failed")


_DESERIALIZER = TypeDeserializer()


def _decode(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: _DESERIALIZER.deserialize(value)
        for name, value in values.items()
    }


class _StatefulDynamo:
    """Small, locked Dynamo model for the reconciliation expressions."""

    def __init__(self) -> None:
        self.meta = SimpleNamespace(client=self)
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.scan_barrier: threading.Barrier | None = None
        self.fail_after_transaction = False
        self.transaction_count = 0
        self.scan_limits: list[int] = []

    @staticmethod
    def _key(value: dict[str, str]) -> tuple[str, str]:
        return value["PK"], value["SK"]

    def get_item(
        self,
        *,
        Key: dict[str, str],  # noqa: N803
        ConsistentRead: bool,
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        with self.lock:
            item = self.rows.get(self._key(Key))
            return {"Item": copy.deepcopy(item)} if item is not None else {}

    @staticmethod
    def _eligible(item: dict[str, Any], now: int) -> bool:
        claim_available = (
            "reconciliation_expires_at" not in item
            or item["reconciliation_expires_at"] <= now
        )
        active = (
            item.get("status") in {"accepted", "running"}
            and item.get("lease_expires_at", now + 1) <= now
        )
        terminal = (
            item.get("status") in {"succeeded", "failed", "cancelled"}
            and item.get("audit_pending") is True
        )
        return (
            item.get("entity_type") == "query_lifecycle"
            and claim_available
            and (active or terminal)
        )

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        now = kwargs["ExpressionAttributeValues"][":now"]
        limit = kwargs["Limit"]
        self.scan_limits.append(limit)
        start = kwargs.get("ExclusiveStartKey")
        with self.lock:
            keys = sorted(self.rows)
            start_index = 0
            if start is not None:
                start_index = keys.index(self._key(start)) + 1
            evaluated = keys[start_index : start_index + limit]
            items = [
                copy.deepcopy(self.rows[key])
                for key in evaluated
                if self._eligible(self.rows[key], now)
            ]
            response: dict[str, Any] = {"Items": items}
            if start_index + len(evaluated) < len(keys) and evaluated:
                pk, sk = evaluated[-1]
                response["LastEvaluatedKey"] = {"PK": pk, "SK": sk}
        barrier = self.scan_barrier
        if barrier is not None:
            barrier.wait(timeout=1)
        return response

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        key = self._key(kwargs["Key"])
        update = kwargs["UpdateExpression"]
        with self.lock:
            item = self.rows.get(key)
            if item is None:
                raise _ConditionalFailure()

            if kwargs.get("ReturnValues") == "ALL_NEW":
                now = values[":now"]
                if (
                    item.get("entity_type") != values[":entity_type"]
                    or item.get("lease_token") != values[":lease_token"]
                    or item.get("status") != values[":status"]
                    or not self._eligible(item, now)
                ):
                    raise _ConditionalFailure()
                item.update(
                    {
                        "reconciliation_token": values[":claim_token"],
                        "reconciliation_owner": values[":owner_token"],
                        "reconciliation_expires_at": values[
                            ":claim_expires_at"
                        ],
                        "reconciliation_claimed_at": values[":claimed_at"],
                        "updated_at": values[":updated_at"],
                    }
                )
                return {"Attributes": copy.deepcopy(item)}

            if "reconciliation_deferred_token" in update:
                if (
                    item.get("lease_token") != values[":lease_token"]
                    or item.get("reconciliation_token")
                    != values[":claim_token"]
                    or item.get("status") != values[":claim_status"]
                ):
                    raise _ConditionalFailure()
                item.update(
                    {
                        "reconciliation_deferred_token": values[
                            ":claim_token"
                        ],
                        "reconciliation_deferred_at": values[
                            ":deferred_at"
                        ],
                        "updated_at": values[":updated_at"],
                    }
                )
            elif "audit_acknowledged_at" in update:
                if (
                    item.get("lease_token") != values[":lease_token"]
                    or item.get("reconciliation_token")
                    != values[":claim_token"]
                    or item.get("status") != values[":terminal_status"]
                    or item.get("audit_pending") is not True
                    or item.get("terminal_audit")
                    != values[":terminal_audit"]
                ):
                    raise _ConditionalFailure()
                item.update(
                    {
                        "audit_acknowledged_at": values[
                            ":acknowledged_at"
                        ],
                        "audit_acknowledged_claim_token": values[
                            ":claim_token"
                        ],
                        "updated_at": values[":updated_at"],
                    }
                )
                item.pop("audit_pending", None)
            else:
                raise AssertionError(f"unexpected update: {update}")

            for name in (
                "reconciliation_token",
                "reconciliation_owner",
                "reconciliation_expires_at",
                "reconciliation_claimed_at",
            ):
                item.pop(name, None)
            return {}

    def transact_write_items(self, **kwargs: Any) -> None:
        operations = kwargs["TransactItems"]
        with self.lock:
            self.transaction_count += 1
            staged = copy.deepcopy(self.rows)
            for operation in operations:
                if "Update" in operation:
                    update = operation["Update"]
                    key = self._key(_decode(update["Key"]))
                    values = _decode(update["ExpressionAttributeValues"])
                    item = staged.get(key)
                    if item is None:
                        raise _ConditionalFailure()
                    if ":claim_token" in values:
                        if (
                            item.get("lease_token")
                            != values[":lease_token"]
                            or item.get("reconciliation_token")
                            != values[":claim_token"]
                            or item.get("status")
                            != values[":claim_status"]
                        ):
                            raise _ConditionalFailure()
                        item.update(
                            {
                                "status": values[":terminal_status"],
                                "actual_scan_bytes": values[
                                    ":actual_scan_bytes"
                                ],
                                "terminal_at": values[":terminal_at"],
                                "updated_at": values[":updated_at"],
                                "audit_pending": True,
                                "terminal_audit": values[
                                    ":terminal_audit"
                                ],
                            }
                        )
                        if ":execution_id" in values:
                            item["execution_id"] = values[":execution_id"]
                        else:
                            item.pop("execution_id", None)
                        if ":failure_code" in values:
                            item["failure_code"] = values[":failure_code"]
                        else:
                            item.pop("failure_code", None)
                    else:
                        refund = -values[":refund"]
                        if item.get("reserved_scan_bytes", -1) < refund:
                            raise _ConditionalFailure()
                        item["reserved_scan_bytes"] -= refund
                else:
                    delete = operation["Delete"]
                    key = self._key(_decode(delete["Key"]))
                    values = _decode(delete["ExpressionAttributeValues"])
                    item = staged.get(key)
                    if (
                        item is None
                        or item.get("lease_token")
                        != values[":lease_token"]
                        or item.get("request_id") != values[":request_id"]
                    ):
                        raise _ConditionalFailure()
                    del staged[key]
            self.rows = staged
            fail_after = self.fail_after_transaction
            self.fail_after_transaction = False
        if fail_after:
            raise RuntimeError("response lost after commit")


def _store() -> tuple[DynamoPersistence, _StatefulDynamo]:
    store = DynamoPersistence(table_name="state")
    store._enabled = True
    backend = _StatefulDynamo()
    store._table = backend
    return store, backend


def _seed_lifecycle(
    store: DynamoPersistence,
    backend: _StatefulDynamo,
    *,
    request_id: str,
    lease_expires_at: int,
) -> tuple[str, str]:
    key = store._query_lifecycle_key(
        "tenant-a",
        "project-a",
        request_id,
    )
    backend.rows[(key["PK"], key["SK"])] = {
        **key,
        "entity_type": "query_lifecycle",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "principal_id": "principal:analyst",
        "request_id": request_id,
        "datasource_id": "warehouse",
        "query_sha256": "a" * 64,
        "status": "running",
        "execution_id": f"execution-{request_id}",
        "lease_token": f"lease-{request_id}",
        "lease_expires_at": lease_expires_at,
        "reserved_scan_bytes": 1000,
        "window_start": 1_700_000_000,
        "project_slot": 1,
        "principal_slot": 0,
    }
    return key["PK"], key["SK"]


def _seed_capacity(
    store: DynamoPersistence,
    backend: _StatefulDynamo,
    *,
    request_id: str,
) -> None:
    for scope, ident in (
        ("project", "project-a"),
        ("principal", "principal:analyst"),
    ):
        counter = store._query_scan_counter_key(
            "tenant-a",
            scope,
            ident,
            1_700_000_000,
        )
        backend.rows[(counter["PK"], counter["SK"])] = {
            **counter,
            "entity_type": "query_scan_counter",
            "tenant_id": "tenant-a",
            "scope": scope,
            "scope_id": ident,
            "reserved_scan_bytes": 1000,
        }
    for scope, ident, slot in (
        ("project", "project-a", 1),
        ("principal", "principal:analyst", 0),
    ):
        slot_key = store._query_slot_key(
            "tenant-a",
            scope,
            ident,
            slot,
        )
        backend.rows[(slot_key["PK"], slot_key["SK"])] = {
            **slot_key,
            "entity_type": "query_admission_slot",
            "lease_token": f"lease-{request_id}",
            "request_id": request_id,
        }


async def _claim(
    store: DynamoPersistence,
    now: datetime,
    *,
    cursor: str | None = None,
    owner: str = "worker-a",
    limit: int = 10,
):
    return await store.claim_query_reconciliation_page(
        owner_token=owner,
        now=now,
        claim_seconds=15,
        limit=limit,
        cursor=cursor,
    )


async def test_claim_is_fenced_across_workers_and_expired_claim_reclaims() -> None:
    store, backend = _store()
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    _seed_lifecycle(
        store,
        backend,
        request_id="race",
        lease_expires_at=int(now.timestamp()) - 1,
    )
    backend.scan_barrier = threading.Barrier(2)

    pages = await asyncio.gather(
        _claim(store, now, owner="worker-a"),
        _claim(store, now, owner="worker-b"),
    )

    claims = [claim for page in pages for claim in page.claims]
    assert len(claims) == 1
    first_token = claims[0].claim_token

    backend.scan_barrier = None
    reclaimed = await _claim(
        store,
        now + timedelta(seconds=16),
        owner="worker-c",
    )

    assert len(reclaimed.claims) == 1
    assert reclaimed.claims[0].claim_token != first_token
    stale_terminal = QueryTerminalAudit(
        status="failed",
        failure_code="athena_query_failed",
        execution_id="execution-race",
        athena_state="FAILED",
        observed_scan_bytes=100,
        accounted_scan_bytes=100,
        engine_execution_ms=5,
        cancellation_requested=False,
        scan_accounting="actual",
    )
    assert not await store.finalize_query_reconciliation(
        claim=claims[0],
        terminal_audit=stale_terminal,
        now=now + timedelta(seconds=16),
    )


async def test_claim_filters_unexpired_rows_and_validates_cursor() -> None:
    store, backend = _store()
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    for index in range(10):
        backend.rows[(f"META#{index:02d}", "VALUE")] = {
            "PK": f"META#{index:02d}",
            "SK": "VALUE",
            "entity_type": "unrelated",
        }
    _seed_lifecycle(
        store,
        backend,
        request_id="expired-a",
        lease_expires_at=int(now.timestamp()) - 1,
    )
    _seed_lifecycle(
        store,
        backend,
        request_id="expired-b",
        lease_expires_at=int(now.timestamp()) - 1,
    )
    _seed_lifecycle(
        store,
        backend,
        request_id="active",
        lease_expires_at=int(now.timestamp()) + 60,
    )

    cursor = None
    request_ids: list[str] = []
    for _ in range(3):
        page = await _claim(store, now, cursor=cursor, limit=1)
        request_ids.extend(
            claim.lease.request_id for claim in page.claims
        )
        cursor = page.next_cursor
        if cursor is None:
            break

    assert sorted(request_ids) == ["expired-a", "expired-b"]
    assert backend.scan_limits[0] == 20
    with pytest.raises(ValueError, match="cursor"):
        await _claim(store, now, cursor="not a cursor")


async def test_claim_rejects_cross_tenant_lifecycle_keys() -> None:
    store, backend = _store()
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    original_key = _seed_lifecycle(
        store,
        backend,
        request_id="mismatched",
        lease_expires_at=int(now.timestamp()) - 1,
    )
    item = backend.rows.pop(original_key)
    item["PK"] = "TENANT#tenant-b"
    backend.rows[(item["PK"], item["SK"])] = item

    page = await _claim(store, now)

    assert page.claims == ()


async def test_finalize_is_atomic_and_idempotent_after_lost_response() -> None:
    store, backend = _store()
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    lifecycle_key = _seed_lifecycle(
        store,
        backend,
        request_id="finalize",
        lease_expires_at=int(now.timestamp()) - 1,
    )
    _seed_capacity(store, backend, request_id="finalize")
    claim = (await _claim(store, now)).claims[0]
    terminal = QueryTerminalAudit(
        status="failed",
        failure_code="athena_query_failed",
        execution_id="execution-finalize",
        athena_state="FAILED",
        observed_scan_bytes=400,
        accounted_scan_bytes=400,
        engine_execution_ms=25,
        cancellation_requested=False,
        scan_accounting="actual",
    )
    backend.fail_after_transaction = True

    assert await store.finalize_query_reconciliation(
        claim=claim,
        terminal_audit=terminal,
        now=now,
    )
    assert await store.finalize_query_reconciliation(
        claim=claim,
        terminal_audit=terminal,
        now=now,
    )

    lifecycle = backend.rows[lifecycle_key]
    assert lifecycle["status"] == "failed"
    assert lifecycle["audit_pending"] is True
    assert lifecycle["terminal_audit"]["accounted_scan_bytes"] == 400
    counters = [
        item["reserved_scan_bytes"]
        for item in backend.rows.values()
        if item.get("entity_type") == "query_scan_counter"
    ]
    assert counters == [400, 400]
    assert all(
        item.get("entity_type") != "query_admission_slot"
        for item in backend.rows.values()
    )


async def test_defer_and_audit_ack_are_claim_scoped_and_retry_safe() -> None:
    store, backend = _store()
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    lifecycle_key = _seed_lifecycle(
        store,
        backend,
        request_id="defer",
        lease_expires_at=int(now.timestamp()) - 1,
    )
    claim = (await _claim(store, now)).claims[0]
    stale = QueryLifecycleClaim(
        lease=claim.lease,
        claim_token="stale-claim",
        status=claim.status,
        execution_id=claim.execution_id,
    )

    assert not await store.defer_query_reconciliation(
        claim=stale,
        now=now,
    )
    assert await store.defer_query_reconciliation(claim=claim, now=now)
    assert await store.defer_query_reconciliation(claim=claim, now=now)
    assert (
        backend.rows[lifecycle_key]["reconciliation_deferred_token"]
        == claim.claim_token
    )

    reclaimed = (
        await _claim(store, now + timedelta(seconds=16))
    ).claims[0]
    terminal = QueryTerminalAudit(
        status="cancelled",
        failure_code="query_cancelled_after_interruption",
        execution_id="execution-defer",
        athena_state="CANCELLED",
        observed_scan_bytes=250,
        accounted_scan_bytes=250,
        engine_execution_ms=20,
        cancellation_requested=True,
        scan_accounting="actual",
    )
    _seed_capacity(store, backend, request_id="defer")
    assert await store.finalize_query_reconciliation(
        claim=reclaimed,
        terminal_audit=terminal,
        now=now + timedelta(seconds=16),
    )

    pending = (
        await _claim(store, now + timedelta(seconds=32), owner="audit-worker")
    ).claims[0]
    assert pending.status == "cancelled"
    assert pending.terminal_audit == terminal
    stale_terminal = QueryLifecycleClaim(
        lease=pending.lease,
        claim_token="stale-audit-claim",
        status=pending.status,
        execution_id=pending.execution_id,
        terminal_audit=pending.terminal_audit,
    )
    assert not await store.ack_query_reconciliation_audit(
        claim=stale_terminal,
        now=now + timedelta(seconds=32),
    )
    assert await store.ack_query_reconciliation_audit(
        claim=pending,
        now=now + timedelta(seconds=32),
    )
    assert await store.ack_query_reconciliation_audit(
        claim=pending,
        now=now + timedelta(seconds=32),
    )
    assert "audit_pending" not in backend.rows[lifecycle_key]
