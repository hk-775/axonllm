"""Distributed query admission and lifecycle regression tests."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer

from src.gateway.models import RateLimitResult
from src.gateway.persistence import DynamoPersistence
from src.gateway.query.admission import (
    QueryAdmissionController,
    QueryAdmissionError,
    QueryAdmissionLimits,
)
from src.gateway.query.reconciliation import (
    QueryLifecycleClaim,
    QueryTerminalAudit,
)


class _Backend:
    enabled = True

    def __init__(self) -> None:
        self.rate_allowed = True
        self.reserve_result: object = {
            "allowed": True,
            "lease_token": "lease-token",
            "window_start": 1_700_000_000,
            "lease_expires_at": 4_000_000_000,
            "project_slot": 1,
            "principal_slot": 0,
        }
        self.rate_calls: list[dict[str, Any]] = []
        self.reserve_calls: list[dict[str, Any]] = []
        self.started_calls: list[dict[str, Any]] = []
        self.finalize_calls: list[dict[str, Any]] = []
        self.ack_calls: list[dict[str, Any]] = []

    async def consume_rate_limit_window(
        self,
        **kwargs: Any,
    ) -> RateLimitResult:
        self.rate_calls.append(kwargs)
        return RateLimitResult(
            allowed=self.rate_allowed,
            limit=10,
            remaining=9 if self.rate_allowed else 0,
            reset_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            retry_after_seconds=None if self.rate_allowed else 30,
        )

    async def reserve_query_capacity(
        self,
        **kwargs: Any,
    ) -> object:
        self.reserve_calls.append(kwargs)
        return self.reserve_result

    async def mark_query_started(self, **kwargs: Any) -> bool:
        self.started_calls.append(kwargs)
        return True

    async def finalize_query_capacity(self, **kwargs: Any) -> bool:
        self.finalize_calls.append(kwargs)
        return True

    async def ack_query_reconciliation_audit(
        self,
        **kwargs: Any,
    ) -> bool:
        self.ack_calls.append(kwargs)
        return True


def _controller(
    backend: _Backend | None = None,
) -> tuple[QueryAdmissionController, _Backend]:
    resolved = backend or _Backend()
    return (
        QueryAdmissionController(
            resolved,
            limits=QueryAdmissionLimits(
                project_rpm=10,
                principal_rpm=5,
                project_concurrency=2,
                principal_concurrency=1,
                project_scan_bytes_per_minute=10_000,
                principal_scan_bytes_per_minute=5_000,
                lease_seconds=60,
            ),
            max_scan_bytes_per_query=1_000,
        ),
        resolved,
    )


async def test_controller_reserves_starts_and_finalizes_lifecycle() -> None:
    controller, backend = _controller()

    lease = await controller.acquire(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal:analyst",
        request_id="request-123",
        datasource_id="warehouse",
        query_sha256="a" * 64,
    )
    await controller.mark_started(lease, "execution-123")
    terminal = QueryTerminalAudit(
        status="succeeded",
        failure_code=None,
        execution_id="execution-123",
        athena_state="SUCCEEDED",
        observed_scan_bytes=400,
        accounted_scan_bytes=400,
        engine_execution_ms=25,
        cancellation_requested=False,
        scan_accounting="actual",
        row_count=1,
        truncated=False,
        result_bytes=12,
    )
    claim = await controller.finalize(
        lease,
        status="succeeded",
        actual_scan_bytes=400,
        execution_id="execution-123",
        terminal_audit=terminal,
    )
    assert isinstance(claim, QueryLifecycleClaim)
    await controller.ack_audit(claim)

    assert backend.rate_calls[0]["namespace"] == "athena-query"
    assert backend.rate_calls[0]["user_limit"] == 5
    assert backend.rate_calls[0]["project_limit"] == 10
    assert backend.reserve_calls[0]["reserved_scan_bytes"] == 1_000
    assert backend.reserve_calls[0]["project_concurrency"] == 2
    assert backend.started_calls[0]["execution_id"] == "execution-123"
    assert backend.finalize_calls[0]["actual_scan_bytes"] == 400
    assert backend.finalize_calls[0]["status"] == "succeeded"
    assert backend.finalize_calls[0]["terminal_audit"] == terminal
    assert backend.finalize_calls[0]["audit_claim_token"].startswith(
        "query-service-"
    )
    assert backend.ack_calls[0]["claim"] == claim


async def test_controller_denies_rate_limit_before_capacity() -> None:
    controller, backend = _controller()
    backend.rate_allowed = False

    with pytest.raises(QueryAdmissionError) as raised:
        await controller.acquire(
            tenant_id="tenant-a",
            project_id="project-a",
            principal_id="principal:analyst",
            request_id="request-123",
            datasource_id="warehouse",
            query_sha256="a" * 64,
        )

    assert raised.value.status_code == 429
    assert raised.value.code == "query_rate_limit_exceeded"
    assert raised.value.retry_after_seconds == 30
    assert backend.reserve_calls == []


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("duplicate_request", 409, "duplicate_query_request"),
        ("project_concurrency", 429, "query_admission_exceeded"),
        ("principal_concurrency", 429, "query_admission_exceeded"),
        ("project_scan_budget", 429, "query_admission_exceeded"),
        ("principal_scan_budget", 429, "query_admission_exceeded"),
    ],
)
async def test_controller_maps_capacity_denials(
    reason: str,
    status: int,
    code: str,
) -> None:
    controller, backend = _controller()
    backend.reserve_result = {
        "allowed": False,
        "reason": reason,
        "retry_after_seconds": 20,
    }

    with pytest.raises(QueryAdmissionError) as raised:
        await controller.acquire(
            tenant_id="tenant-a",
            project_id="project-a",
            principal_id="principal:analyst",
            request_id="request-123",
            datasource_id="warehouse",
            query_sha256="a" * 64,
        )

    assert raised.value.status_code == status
    assert raised.value.code == code


async def test_controller_fails_closed_on_invalid_backend_state() -> None:
    controller, backend = _controller()
    backend.reserve_result = None

    with pytest.raises(QueryAdmissionError) as raised:
        await controller.acquire(
            tenant_id="tenant-a",
            project_id="project-a",
            principal_id="principal:analyst",
            request_id="request-123",
            datasource_id="warehouse",
            query_sha256="a" * 64,
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "query_admission_unavailable"


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_rpm": 31},
        {"principal_concurrency": 6},
        {"principal_scan_bytes_per_minute": 6 * 1024**3},
        {"lease_seconds": 29},
    ],
)
def test_limits_reject_inconsistent_values(
    changes: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        QueryAdmissionLimits(**changes)


class _TransactionCancelled(RuntimeError):
    def __init__(self, item_count: int, failed_index: int) -> None:
        reasons = [{"Code": "None"} for _ in range(item_count)]
        reasons[failed_index] = {"Code": "ConditionalCheckFailed"}
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": reasons,
        }
        super().__init__("transaction cancelled")


class _Client:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.fail_index: int | None = None

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(copy.deepcopy(kwargs))
        if self.fail_index is not None:
            raise _TransactionCancelled(
                len(kwargs["TransactItems"]),
                self.fail_index,
            )


class _Table:
    def __init__(self, client: _Client) -> None:
        self.meta = SimpleNamespace(client=client)
        self.updates: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.item: dict[str, Any] | None = None

    def update_item(self, **kwargs: Any) -> None:
        self.updates.append(copy.deepcopy(kwargs))

    def delete_item(self, **kwargs: Any) -> None:
        self.deletes.append(copy.deepcopy(kwargs))

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": copy.deepcopy(self.item)} if self.item else {}


def _store() -> tuple[DynamoPersistence, _Client, _Table]:
    store = DynamoPersistence(table_name="state")
    store._enabled = True
    client = _Client()
    table = _Table(client)
    store._table = table
    return store, client, table


def _decode(values: dict[str, Any]) -> dict[str, Any]:
    deserializer = TypeDeserializer()
    return {
        name: deserializer.deserialize(value)
        for name, value in values.items()
    }


async def _reserve(
    store: DynamoPersistence,
    **changes: Any,
) -> dict | None:
    values = {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "principal_id": "principal:analyst",
        "request_id": "request-123",
        "datasource_id": "warehouse",
        "query_sha256": "a" * 64,
        "reserved_scan_bytes": 1_000,
        "project_concurrency": 2,
        "principal_concurrency": 1,
        "project_scan_limit": 10_000,
        "principal_scan_limit": 5_000,
        "window_seconds": 60,
        "lease_seconds": 60,
        "now": datetime(2026, 8, 10, tzinfo=timezone.utc),
    }
    values.update(changes)
    return await store.reserve_query_capacity(**values)


async def test_dynamo_reservation_is_one_five_item_transaction() -> None:
    store, client, _ = _store()

    decision = await _reserve(store)

    assert decision is not None
    assert decision["allowed"] is True
    operations = client.transactions[0]["TransactItems"]
    assert len(operations) == 5
    lifecycle = _decode(operations[0]["Put"]["Item"])
    assert lifecycle["entity_type"] == "query_lifecycle"
    assert lifecycle["status"] == "accepted"
    assert lifecycle["request_id"] == "request-123"
    assert lifecycle["lease_expires_at"] > int(
        datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp()
    )
    assert lifecycle["project_slot"] == decision["project_slot"]
    assert lifecycle["principal_slot"] == decision["principal_slot"]
    assert "attribute_not_exists(PK)" in (
        operations[0]["Put"]["ConditionExpression"]
    )
    for operation in operations[1:3]:
        assert "lease_expires_at < :now" in (
            operation["Put"]["ConditionExpression"]
        )
    for operation in operations[3:]:
        assert "reserved_scan_bytes <= :remaining" in (
            operation["Update"]["ConditionExpression"]
        )


@pytest.mark.parametrize(
    ("failed_index", "reason"),
    [
        (0, "duplicate_request"),
        (3, "project_scan_budget"),
        (4, "principal_scan_budget"),
    ],
)
async def test_dynamo_reservation_classifies_terminal_denials(
    failed_index: int,
    reason: str,
) -> None:
    store, client, _ = _store()
    client.fail_index = failed_index

    decision = await _reserve(store)

    assert decision is not None
    assert decision["allowed"] is False
    assert decision["reason"] == reason


async def test_dynamo_reservation_exhausts_all_project_slots() -> None:
    store, client, _ = _store()
    client.fail_index = 1

    decision = await _reserve(store, project_concurrency=3)

    assert decision is not None
    assert decision["reason"] == "project_concurrency"
    assert len(client.transactions) == 3


async def test_dynamo_started_and_terminal_updates_are_conditional() -> None:
    store, client, table = _store()
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    table.item = {
        "lease_token": "lease-token",
        "request_id": "request-123",
    }
    terminal = QueryTerminalAudit(
        status="succeeded",
        failure_code=None,
        execution_id="execution-123",
        athena_state="SUCCEEDED",
        observed_scan_bytes=400,
        accounted_scan_bytes=400,
        engine_execution_ms=25,
        cancellation_requested=False,
        scan_accounting="actual",
        row_count=1,
        truncated=False,
        result_bytes=12,
    )

    started = await store.mark_query_started(
        tenant_id="tenant-a",
        project_id="project-a",
        request_id="request-123",
        lease_token="lease-token",
        execution_id="execution-123",
        now=now,
    )
    finalized = await store.finalize_query_capacity(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal:analyst",
        request_id="request-123",
        lease_token="lease-token",
        window_start=1_700_000_000,
        project_slot=1,
        principal_slot=0,
        reserved_scan_bytes=1_000,
        actual_scan_bytes=400,
        status="succeeded",
        execution_id="execution-123",
        failure_code=None,
        now=now,
        terminal_audit=terminal,
        audit_claim_token="query-service-test",
    )

    assert started is True
    assert "lease_token = :lease_token" in (
        table.updates[0]["ConditionExpression"]
    )
    assert finalized is True
    operations = client.transactions[0]["TransactItems"]
    assert len(operations) == 5
    lifecycle_values = _decode(
        operations[0]["Update"]["ExpressionAttributeValues"]
    )
    assert lifecycle_values[":terminal_status"] == "succeeded"
    assert lifecycle_values[":audit_pending"] is True
    assert lifecycle_values[":audit_claim_token"] == "query-service-test"
    assert lifecycle_values[":terminal_audit"]["row_count"] == 1
    assert lifecycle_values[":terminal_audit"]["result_bytes"] == 12
    for operation in operations[1:3]:
        values = _decode(
            operation["Update"]["ExpressionAttributeValues"]
        )
        assert values[":refund"] == -600
    for operation in operations[3:]:
        delete = operation["Delete"]
        assert "lease_token = :lease_token" in delete[
            "ConditionExpression"
        ]
        values = _decode(delete["ExpressionAttributeValues"])
        assert values[":lease_token"] == "lease-token"
        assert values[":request_id"] == "request-123"
    assert table.deletes == []
    assert all(
        "lease_token = :lease_token" in item["ConditionExpression"]
        for item in table.deletes
    )
