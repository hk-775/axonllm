"""Focused tests for durable recovery of interrupted Athena queries."""

from __future__ import annotations

import asyncio
from typing import Any

from src.gateway.query.admission import QueryAdmissionLease
from src.gateway.query.athena import AthenaQueryTermination
from src.gateway.query.models import (
    AthenaDatasource,
    AthenaRoleBinding,
    AthenaRoleBindings,
)
from src.gateway.query.reconciliation import (
    QueryLifecycleClaim,
    QueryLifecyclePage,
    QueryLifecycleReconciler,
    QueryReconciliationResult,
    QueryReconciliationWorker,
    QueryTerminalAudit,
)


ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"


def _datasource(*, enabled: bool = True) -> AthenaDatasource:
    return AthenaDatasource(
        datasource_id="warehouse",
        tenant_id="tenant-a",
        project_id="project-a",
        name="Analytics warehouse",
        role_arn=ROLE_ARN,
        region="us-east-1",
        catalog="AwsDataCatalog",
        database="analytics",
        workgroup="axon_read_only",
        enabled=enabled,
    )


def _bindings() -> AthenaRoleBindings:
    return AthenaRoleBindings(
        (
            AthenaRoleBinding(
                tenant_id="tenant-a",
                project_id="project-a",
                role_arn=ROLE_ARN,
            ),
        )
    )


def _lease(
    request_id: str,
    *,
    reserved_scan_bytes: int = 4096,
) -> QueryAdmissionLease:
    return QueryAdmissionLease(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal:analyst",
        request_id=request_id,
        datasource_id="warehouse",
        query_sha256=request_id[0] * 64,
        lease_token=f"lease-{request_id}",
        window_start=1_700_000_000,
        lease_expires_at=1_700_000_060,
        project_slot=1,
        principal_slot=0,
        reserved_scan_bytes=reserved_scan_bytes,
    )


class _Store:
    enabled = True

    def __init__(self, pages: list[QueryLifecyclePage]) -> None:
        self.pages = list(pages)
        self.claim_calls: list[dict[str, Any]] = []
        self.finalize_calls: list[dict[str, Any]] = []
        self.defer_calls: list[dict[str, Any]] = []
        self.ack_calls: list[dict[str, Any]] = []

    async def claim_query_reconciliation_page(
        self,
        **kwargs: Any,
    ) -> QueryLifecyclePage:
        self.claim_calls.append(kwargs)
        return self.pages.pop(0)

    async def finalize_query_reconciliation(
        self,
        **kwargs: Any,
    ) -> bool:
        self.finalize_calls.append(kwargs)
        return True

    async def defer_query_reconciliation(
        self,
        **kwargs: Any,
    ) -> bool:
        self.defer_calls.append(kwargs)
        return True

    async def ack_query_reconciliation_audit(
        self,
        **kwargs: Any,
    ) -> bool:
        self.ack_calls.append(kwargs)
        return True


class _Repository:
    def __init__(self, datasource: AthenaDatasource | None = None) -> None:
        self.datasource = datasource if datasource is not None else _datasource()

    async def get(
        self,
        _tenant_id: str,
        _project_id: str,
        _datasource_id: str,
    ) -> Any:
        return self.datasource


class _MissingRepository:
    async def get(
        self,
        _tenant_id: str,
        _project_id: str,
        _datasource_id: str,
    ) -> None:
        return None


class _Executor:
    def __init__(self, terminations: list[AthenaQueryTermination]) -> None:
        self.terminations = list(terminations)
        self.cancel_calls: list[dict[str, Any]] = []

    async def cancel(self, _datasource: Any, **kwargs: Any) -> Any:
        self.cancel_calls.append(kwargs)
        return self.terminations.pop(0)


class _Audit:
    durable_enabled = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> object:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.records.append(kwargs)
        return object()


def _reconciler(
    store: _Store,
    executor: _Executor,
    audit: _Audit,
    *,
    bindings: AthenaRoleBindings | None = None,
    repository: Any | None = None,
) -> QueryLifecycleReconciler:
    return QueryLifecycleReconciler(
        store=store,
        repository=repository or _Repository(),
        bindings=bindings or _bindings(),
        executor=executor,
        audit_trail=audit,
        page_size=10,
    )


async def test_reconciles_accepted_and_running_queries_with_safe_accounting() -> None:
    accepted = QueryLifecycleClaim(
        lease=_lease("accepted"),
        claim_token="claim-accepted",
        status="accepted",
    )
    running = QueryLifecycleClaim(
        lease=_lease("running"),
        claim_token="claim-running",
        status="running",
        execution_id="execution-123",
    )
    store = _Store(
        [QueryLifecyclePage(claims=(accepted, running))]
    )
    executor = _Executor(
        [
            AthenaQueryTermination(
                query_execution_id="execution-123",
                state="CANCELLED",
                terminal=True,
                data_scanned_bytes=768,
                engine_execution_ms=29,
                cancellation_requested=True,
            )
        ]
    )
    audit = _Audit()

    result = await _reconciler(store, executor, audit).run()

    assert result.claimed == 2
    assert result.finalized == 2
    assert result.audited == 2
    assert result.failed == 0
    assert len(store.finalize_calls) == 2
    accepted_terminal = store.finalize_calls[0]["terminal_audit"]
    assert accepted_terminal.status == "failed"
    assert accepted_terminal.accounted_scan_bytes == 4096
    assert accepted_terminal.scan_accounting == "reserved_fallback"
    running_terminal = store.finalize_calls[1]["terminal_audit"]
    assert running_terminal.status == "cancelled"
    assert running_terminal.observed_scan_bytes == 768
    assert running_terminal.accounted_scan_bytes == 768
    assert running_terminal.cancellation_requested is True
    assert executor.cancel_calls[0]["execution_id"] == "execution-123"
    assert len(store.ack_calls) == 2
    assert [record["data"]["reconciled"] for record in audit.records] == [
        True,
        True,
    ]


async def test_nonterminal_cancellation_is_deferred_without_refund_or_audit() -> None:
    claim = QueryLifecycleClaim(
        lease=_lease("running"),
        claim_token="claim-running",
        status="running",
        execution_id="execution-123",
    )
    store = _Store([QueryLifecyclePage(claims=(claim,))])
    executor = _Executor(
        [
            AthenaQueryTermination(
                query_execution_id="execution-123",
                state="RUNNING",
                terminal=False,
                data_scanned_bytes=512,
                engine_execution_ms=10,
                cancellation_requested=True,
            )
        ]
    )
    audit = _Audit()

    result = await _reconciler(store, executor, audit).run()

    assert result.deferred == 1
    assert store.finalize_calls == []
    assert len(store.defer_calls) == 1
    assert audit.records == []


async def test_missing_datasource_defers_without_assuming_execution_stopped() -> None:
    claim = QueryLifecycleClaim(
        lease=_lease("missing"),
        claim_token="claim-missing",
        status="running",
        execution_id="execution-123",
    )
    store = _Store([QueryLifecyclePage(claims=(claim,))])
    executor = _Executor([])
    audit = _Audit()

    result = await _reconciler(
        store,
        executor,
        audit,
        repository=_MissingRepository(),
    ).run()

    assert result.deferred == 1
    assert executor.cancel_calls == []
    assert store.finalize_calls == []
    assert len(store.defer_calls) == 1
    assert audit.records == []


async def test_unbound_datasource_defers_without_assuming_execution_stopped() -> None:
    claim = QueryLifecycleClaim(
        lease=_lease("unbound"),
        claim_token="claim-unbound",
        status="running",
        execution_id="execution-123",
    )
    store = _Store([QueryLifecyclePage(claims=(claim,))])
    executor = _Executor([])
    audit = _Audit()

    result = await _reconciler(
        store,
        executor,
        audit,
        bindings=AthenaRoleBindings(),
    ).run()

    assert result.deferred == 1
    assert executor.cancel_calls == []
    assert store.finalize_calls == []
    assert len(store.defer_calls) == 1
    assert audit.records == []


async def test_disabled_bound_datasource_is_cancelled_and_finalized() -> None:
    claim = QueryLifecycleClaim(
        lease=_lease("disabled"),
        claim_token="claim-disabled",
        status="running",
        execution_id="execution-123",
    )
    store = _Store([QueryLifecyclePage(claims=(claim,))])
    executor = _Executor(
        [
            AthenaQueryTermination(
                query_execution_id="execution-123",
                state="CANCELLED",
                terminal=True,
                data_scanned_bytes=256,
                engine_execution_ms=12,
                cancellation_requested=True,
            )
        ]
    )
    audit = _Audit()
    result = await _reconciler(
        store,
        executor,
        audit,
        repository=_Repository(_datasource(enabled=False)),
    ).run()

    assert result.finalized == 1
    assert result.audited == 1
    assert len(executor.cancel_calls) == 1
    terminal = store.finalize_calls[0]["terminal_audit"]
    assert terminal.status == "cancelled"
    assert terminal.accounted_scan_bytes == 256
    assert len(store.ack_calls) == 1


async def test_pending_terminal_audit_is_replayed_without_recancelling() -> None:
    terminal = QueryTerminalAudit(
        status="cancelled",
        failure_code="query_cancelled_after_interruption",
        execution_id="execution-123",
        athena_state="CANCELLED",
        observed_scan_bytes=640,
        accounted_scan_bytes=640,
        engine_execution_ms=21,
        cancellation_requested=True,
        scan_accounting="actual",
    )
    claim = QueryLifecycleClaim(
        lease=_lease("terminal"),
        claim_token="claim-terminal",
        status="cancelled",
        execution_id="execution-123",
        terminal_audit=terminal,
    )
    store = _Store([QueryLifecyclePage(claims=(claim,))])
    executor = _Executor([])
    audit = _Audit()

    result = await _reconciler(store, executor, audit).run()

    assert result.audited == 1
    assert result.finalized == 0
    assert executor.cancel_calls == []
    assert store.finalize_calls == []
    assert len(store.ack_calls) == 1
    assert audit.records[0]["data"]["data_scanned_bytes"] == 640


async def test_audit_failure_leaves_terminal_evidence_pending_for_next_run() -> None:
    active = QueryLifecycleClaim(
        lease=_lease("running"),
        claim_token="claim-running",
        status="running",
        execution_id="execution-123",
    )
    termination = AthenaQueryTermination(
        query_execution_id="execution-123",
        state="FAILED",
        terminal=True,
        data_scanned_bytes=900,
        engine_execution_ms=34,
        cancellation_requested=False,
    )
    first_store = _Store([QueryLifecyclePage(claims=(active,))])
    failed_audit = _Audit(fail=True)

    first = await _reconciler(
        first_store,
        _Executor([termination]),
        failed_audit,
    ).run()

    assert first.failed == 1
    assert len(first_store.finalize_calls) == 1
    assert first_store.ack_calls == []

    terminal = first_store.finalize_calls[0]["terminal_audit"]
    pending = QueryLifecycleClaim(
        lease=active.lease,
        claim_token="claim-audit-retry",
        status=terminal.status,
        execution_id=terminal.execution_id,
        terminal_audit=terminal,
    )
    retry_store = _Store([QueryLifecyclePage(claims=(pending,))])
    recovered_audit = _Audit()

    retry = await _reconciler(
        retry_store,
        _Executor([]),
        recovered_audit,
    ).run()

    assert retry.audited == 1
    assert len(recovered_audit.records) == 1
    assert len(retry_store.ack_calls) == 1


async def test_reconciliation_paginates_with_one_bounded_worker_lease() -> None:
    store = _Store(
        [
            QueryLifecyclePage(claims=(), next_cursor="page-2"),
            QueryLifecyclePage(claims=()),
        ]
    )
    reconciler = _reconciler(store, _Executor([]), _Audit())

    result = await reconciler.run(cursor="page-1")

    assert result.pages == 2
    assert result.next_cursor is None
    assert [call["cursor"] for call in store.claim_calls] == [
        "page-1",
        "page-2",
    ]
    assert {call["owner_token"] for call in store.claim_calls} == {
        store.claim_calls[0]["owner_token"]
    }
    assert all(call["claim_seconds"] == 60 for call in store.claim_calls)
    assert all(call["limit"] == 10 for call in store.claim_calls)


async def test_periodic_worker_runs_immediately_retries_and_stops_cleanly(
    monkeypatch,
) -> None:
    reconciler = _reconciler(
        _Store([QueryLifecyclePage(claims=())]),
        _Executor([]),
        _Audit(),
    )
    calls: list[str | None] = []
    retried = asyncio.Event()

    async def run(
        *,
        cursor: str | None = None,
        max_pages: int = 10,
    ) -> QueryReconciliationResult:
        assert max_pages == 2
        calls.append(cursor)
        if len(calls) == 1:
            raise RuntimeError("transient persistence failure")
        retried.set()
        return QueryReconciliationResult(
            claimed=0,
            finalized=0,
            audited=0,
            deferred=0,
            lost_claims=0,
            failed=0,
            pages=1,
            next_cursor="next-page",
        )

    monkeypatch.setattr(reconciler, "run", run)
    worker = QueryReconciliationWorker(
        reconciler,
        interval_seconds=0.01,
        max_pages=2,
    )

    await worker.start()
    assert calls == [None]
    await asyncio.wait_for(retried.wait(), timeout=1)
    assert worker.running is True

    await worker.stop()
    await worker.stop()

    assert worker.running is False
    assert calls[:2] == [None, None]
