"""Focused orchestration tests for the canonical query service."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from src.gateway.models import AuthMethod, Principal, TenantRole
from src.gateway.query.athena import (
    AthenaExecutionError,
    AthenaQueryResult,
    AthenaQueryTermination,
)
from src.gateway.query.admission import (
    QueryAdmissionError,
    QueryAdmissionLease,
)
from src.gateway.query.models import (
    AthenaDatasource,
    AthenaRoleBinding,
    AthenaRoleBindings,
)
from src.gateway.query.reconciliation import (
    QueryLifecycleClaim,
    QueryTerminalAudit,
)
from src.gateway.query.service import QueryService, QueryServiceError
from src.gateway.security.audit_trail import AuditEventType


ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"


def _datasource(
    *,
    enabled: bool = True,
    role_arn: str = ROLE_ARN,
) -> AthenaDatasource:
    return AthenaDatasource(
        datasource_id="warehouse",
        tenant_id="tenant-a",
        project_id="project-a",
        name="Analytics warehouse",
        role_arn=role_arn,
        region="us-east-1",
        catalog="AwsDataCatalog",
        database="analytics",
        workgroup="axon_read_only",
        enabled=enabled,
    )


def _principal(
    *,
    tenant_id: str = "tenant-a",
    project_ids: frozenset[str] = frozenset({"project-a"}),
    role: TenantRole = TenantRole.TENANT_MEMBER,
    scopes: frozenset[str] = frozenset(),
) -> Principal:
    return Principal(
        principal_id="principal:analyst",
        tenant_id=tenant_id,
        subject="subject:analyst",
        issuer="https://idp.example.test",
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        project_ids=project_ids,
        scopes=scopes,
    )


def _bindings(
    *,
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
    role_arn: str = ROLE_ARN,
) -> AthenaRoleBindings:
    return AthenaRoleBindings(
        (
            AthenaRoleBinding(
                tenant_id=tenant_id,
                project_id=project_id,
                role_arn=role_arn,
            ),
        )
    )


def _result() -> AthenaQueryResult:
    return AthenaQueryResult(
        query_execution_id="execution-123",
        columns=(
            {"name": "order_id", "type": "varchar"},
            {"name": "total", "type": "decimal(10,2)"},
        ),
        rows=(("order-1", "12.50"),),
        row_count=1,
        truncated=False,
        data_scanned_bytes=2048,
        engine_execution_ms=25,
        result_bytes=12,
    )


class _Repository:
    def __init__(
        self,
        datasource: AthenaDatasource | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.datasource = datasource
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    async def get(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> AthenaDatasource | None:
        self.calls.append((tenant_id, project_id, datasource_id))
        if self.error is not None:
            raise self.error
        return self.datasource


class _Executor:
    def __init__(
        self,
        result: AthenaQueryResult | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result or _result()
        self.error = error
        self.calls: list[tuple[Any, AthenaDatasource, dict[str, Any]]] = []

    async def execute(
        self,
        query: Any,
        datasource: AthenaDatasource,
        **kwargs: Any,
    ) -> AthenaQueryResult:
        self.calls.append((query, datasource, kwargs))
        if self.error is not None:
            raise self.error
        on_started = kwargs.get("on_started")
        if on_started is not None:
            await on_started(self.result.query_execution_id)
        return self.result


class _AuditTrail:
    def __init__(
        self,
        *,
        durable_enabled: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.durable_enabled = durable_enabled
        self.error = error
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> object:
        if self.error is not None:
            raise self.error
        self.records.append(kwargs)
        return object()


class _Admission:
    def __init__(
        self,
        *,
        acquire_error: Exception | None = None,
        finalize_error: Exception | None = None,
    ) -> None:
        self.acquire_error = acquire_error
        self.finalize_error = finalize_error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ack_calls: list[QueryLifecycleClaim] = []
        self.lease = QueryAdmissionLease(
            tenant_id="tenant-a",
            project_id="project-a",
            principal_id="principal:analyst",
            request_id="request-123",
            datasource_id="warehouse",
            query_sha256="a" * 64,
            lease_token="lease-token",
            window_start=1_700_000_000,
            lease_expires_at=4_000_000_000,
            project_slot=0,
            principal_slot=0,
            reserved_scan_bytes=4096,
        )

    async def acquire(self, **kwargs: Any) -> QueryAdmissionLease:
        self.calls.append(("acquire", kwargs))
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.lease

    async def mark_started(
        self,
        lease: QueryAdmissionLease,
        execution_id: str,
    ) -> None:
        self.calls.append(
            (
                "started",
                {"lease": lease, "execution_id": execution_id},
            )
        )

    async def finalize(
        self,
        lease: QueryAdmissionLease,
        **kwargs: Any,
    ) -> QueryLifecycleClaim | None:
        self.calls.append(("finalize", {"lease": lease, **kwargs}))
        if self.finalize_error is not None:
            raise self.finalize_error
        terminal = kwargs.get("terminal_audit")
        if not isinstance(terminal, QueryTerminalAudit):
            return None
        return QueryLifecycleClaim(
            lease=lease,
            claim_token="query-service-test",
            status=terminal.status,
            execution_id=terminal.execution_id,
            terminal_audit=terminal,
        )

    async def ack_audit(self, claim: QueryLifecycleClaim) -> None:
        self.ack_calls.append(claim)


def _service(
    *,
    repository: _Repository | None = None,
    bindings: AthenaRoleBindings | None = None,
    executor: _Executor | None = None,
    audit: _AuditTrail | None = None,
    require_durable_audit: bool = True,
    admission: _Admission | None = None,
    require_durable_admission: bool = False,
) -> tuple[QueryService, _Repository, _Executor, _AuditTrail]:
    resolved_repository = repository or _Repository(_datasource())
    resolved_executor = executor or _Executor()
    resolved_audit = audit or _AuditTrail()
    service = QueryService(
        repository=resolved_repository,
        bindings=bindings or _bindings(),
        executor=resolved_executor,
        audit_trail=resolved_audit,
        require_durable_audit=require_durable_audit,
        admission=admission,
        require_durable_admission=require_durable_admission,
    )
    return (
        service,
        resolved_repository,
        resolved_executor,
        resolved_audit,
    )


async def test_durable_admission_tracks_started_and_terminal_state() -> None:
    admission = _Admission()
    service, _, _, _ = _service(
        admission=admission,
        require_durable_admission=True,
    )

    response = await service.execute(
        principal=_principal(),
        tenant_id="tenant-a",
        project_id="project-a",
        datasource_id="warehouse",
        sql="SELECT * FROM orders",
        request_id="request-123",
    )

    assert response["query_execution_id"] == "execution-123"
    assert [name for name, _ in admission.calls] == [
        "acquire",
        "started",
        "finalize",
    ]
    assert admission.calls[2][1]["status"] == "succeeded"
    assert admission.calls[2][1]["actual_scan_bytes"] == 2048
    terminal = admission.calls[2][1]["terminal_audit"]
    assert terminal.row_count == 1
    assert terminal.result_bytes == 12
    assert len(admission.ack_calls) == 1
    assert admission.ack_calls[0].terminal_audit == terminal


async def test_terminal_audit_failure_leaves_replayable_evidence_pending() -> None:
    class _FailTerminalAudit(_AuditTrail):
        async def record(self, **kwargs: Any) -> object:
            if len(self.records) == 1:
                raise RuntimeError("audit unavailable")
            return await super().record(**kwargs)

    admission = _Admission()
    audit = _FailTerminalAudit()
    service, _, _, _ = _service(
        admission=admission,
        audit=audit,
        require_durable_admission=True,
    )

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    assert raised.value.code == "query_audit_unavailable"
    finalization = admission.calls[-1][1]
    assert isinstance(
        finalization["terminal_audit"],
        QueryTerminalAudit,
    )
    assert admission.ack_calls == []


async def test_required_admission_fails_closed_before_execution() -> None:
    service, _, executor, audit = _service(
        require_durable_admission=True,
    )

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    assert raised.value.code == "query_admission_unavailable"
    assert executor.calls == []
    assert [record["event_type"] for record in audit.records] == [
        AuditEventType.QUERY_REQUEST
    ]


async def test_admission_denial_is_durably_audited() -> None:
    admission = _Admission(
        acquire_error=QueryAdmissionError(
            429,
            "query_admission_exceeded",
            "Query capacity is exhausted.",
        )
    )
    service, _, executor, audit = _service(admission=admission)

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    assert raised.value.status_code == 429
    assert executor.calls == []
    assert [record["event_type"] for record in audit.records] == [
        AuditEventType.QUERY_REQUEST,
        AuditEventType.QUERY_REJECTED,
    ]
    assert audit.records[-1]["data"]["reason"] == (
        "query_admission_exceeded"
    )


async def test_execution_failure_releases_reserved_capacity() -> None:
    admission = _Admission()
    executor = _Executor(
        error=AthenaExecutionError(
            "athena_query_failed",
            "Athena failed.",
        )
    )
    service, _, _, _ = _service(
        executor=executor,
        admission=admission,
    )

    with pytest.raises(QueryServiceError):
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    assert [name for name, _ in admission.calls] == [
        "acquire",
        "finalize",
    ]
    assert admission.calls[-1][1]["status"] == "failed"
    assert admission.calls[-1][1]["actual_scan_bytes"] == 0


async def test_terminal_failure_charges_reported_scan_and_audits_accounting() -> None:
    admission = _Admission()
    executor = _Executor(
        error=AthenaExecutionError(
            "athena_query_failed",
            "Athena failed.",
            query_execution_id="execution-123",
            athena_state="FAILED",
            data_scanned_bytes=1536,
            engine_execution_ms=31,
        )
    )
    service, _, _, audit = _service(
        executor=executor,
        admission=admission,
    )

    with pytest.raises(QueryServiceError):
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    finalization = admission.calls[-1][1]
    assert finalization["status"] == "failed"
    assert finalization["actual_scan_bytes"] == 1536
    assert finalization["execution_id"] == "execution-123"
    terminal_audit = audit.records[-1]["data"]
    assert terminal_audit["status"] == "failed"
    assert terminal_audit["query_execution_id"] == "execution-123"
    assert terminal_audit["athena_state"] == "FAILED"
    assert terminal_audit["data_scanned_bytes"] == 1536
    assert terminal_audit["accounted_scan_bytes"] == 1536
    assert terminal_audit["engine_execution_ms"] == 31
    assert terminal_audit["scan_accounting"] == "actual"
    assert terminal_audit["lifecycle_finalized"] is True


async def test_terminal_failure_without_statistics_keeps_full_reservation() -> None:
    admission = _Admission()
    executor = _Executor(
        error=AthenaExecutionError(
            "athena_status_failed",
            "Athena statistics were unavailable.",
            query_execution_id="execution-123",
            athena_state="FAILED",
        )
    )
    service, _, _, audit = _service(
        executor=executor,
        admission=admission,
    )

    with pytest.raises(QueryServiceError):
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    finalization = admission.calls[-1][1]
    assert finalization["actual_scan_bytes"] == 4096
    assert audit.records[-1]["data"]["data_scanned_bytes"] is None
    assert audit.records[-1]["data"]["accounted_scan_bytes"] == 4096
    assert audit.records[-1]["data"]["scan_accounting"] == (
        "reserved_fallback"
    )


async def test_ambiguous_start_failure_keeps_full_reservation() -> None:
    admission = _Admission()
    executor = _Executor(
        error=AthenaExecutionError(
            "athena_start_failed",
            "Athena start response was unavailable.",
            execution_may_have_started=True,
        )
    )
    service, _, _, audit = _service(
        executor=executor,
        admission=admission,
    )

    with pytest.raises(QueryServiceError):
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    finalization = admission.calls[-1][1]
    assert finalization["execution_id"] is None
    assert finalization["actual_scan_bytes"] == 4096
    assert audit.records[-1]["data"]["execution_may_have_started"] is True
    assert audit.records[-1]["data"]["scan_accounting"] == (
        "reserved_fallback"
    )


async def test_unknown_started_failure_holds_reservation_for_reconciliation() -> None:
    admission = _Admission()
    executor = _Executor(
        error=AthenaExecutionError(
            "athena_status_failed",
            "Athena status failed.",
            query_execution_id="execution-123",
        )
    )
    service, _, _, audit = _service(
        executor=executor,
        admission=admission,
    )

    with pytest.raises(QueryServiceError):
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    assert [name for name, _ in admission.calls] == ["acquire"]
    terminal_audit = audit.records[-1]["data"]
    assert terminal_audit["status"] == "reconciliation_pending"
    assert terminal_audit["accounted_scan_bytes"] == 4096
    assert terminal_audit["scan_accounting"] == "reservation_held"
    assert terminal_audit["lifecycle_finalized"] is False


async def test_request_cancellation_is_accounted_and_completely_audited() -> None:
    class _CancelledExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_calls: list[dict[str, Any]] = []

        async def execute(
            self,
            query: Any,
            datasource: AthenaDatasource,
            **kwargs: Any,
        ) -> AthenaQueryResult:
            self.calls.append((query, datasource, kwargs))
            await kwargs["on_started"]("execution-123")
            raise asyncio.CancelledError

        async def cancel(
            self,
            _datasource: AthenaDatasource,
            **kwargs: Any,
        ) -> AthenaQueryTermination:
            self.cancel_calls.append(kwargs)
            return AthenaQueryTermination(
                query_execution_id="execution-123",
                state="CANCELLED",
                terminal=True,
                data_scanned_bytes=768,
                engine_execution_ms=17,
                cancellation_requested=True,
            )

    admission = _Admission()
    executor = _CancelledExecutor()
    service, _, _, audit = _service(
        executor=executor,
        admission=admission,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="request-123",
        )

    assert len(executor.cancel_calls) == 1
    finalization = admission.calls[-1][1]
    assert finalization["status"] == "cancelled"
    assert finalization["actual_scan_bytes"] == 768
    assert finalization["failure_code"] == "query_cancelled"
    terminal_audit = audit.records[-1]["data"]
    assert terminal_audit == {
        "datasource_id": "warehouse",
        "query_sha256": hashlib.sha256(
            b"SELECT * FROM orders"
        ).hexdigest(),
        "status": "cancelled",
        "failure_code": "query_cancelled",
        "query_execution_id": "execution-123",
        "athena_state": "CANCELLED",
        "data_scanned_bytes": 768,
        "accounted_scan_bytes": 768,
        "engine_execution_ms": 17,
        "cancellation_requested": True,
        "execution_may_have_started": True,
        "scan_accounting": "actual",
        "lifecycle_finalized": True,
        "reconciled": False,
    }


async def test_success_authorizes_audits_and_forwards_canonical_query() -> None:
    service, repository, executor, audit = _service()

    response = await service.execute(
        principal=_principal(),
        tenant_id="tenant-a",
        project_id="project-a",
        datasource_id="warehouse",
        sql="select order_id, total from orders",
        max_rows=50,
        request_id="request-123",
    )

    assert repository.calls == [
        ("tenant-a", "project-a", "warehouse")
    ]
    assert len(executor.calls) == 1
    query, datasource, execution_context = executor.calls[0]
    assert query.sql == "SELECT order_id, total FROM orders"
    assert datasource.datasource_id == "warehouse"
    assert execution_context == {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "principal_id": "principal:analyst",
        "request_id": "request-123",
        "max_rows": 50,
    }
    assert [record["event_type"] for record in audit.records] == [
        AuditEventType.QUERY_REQUEST,
        AuditEventType.QUERY_RESULT,
    ]
    assert audit.records[0]["data"] == {
        "datasource_id": "warehouse",
        "query_sha256": query.sha256,
        "table_count": 1,
        "requested_max_rows": 50,
    }
    assert audit.records[1]["data"]["status"] == "succeeded"
    assert all(
        "select" not in repr(record["data"]).casefold()
        for record in audit.records
    )
    assert response["request_id"] == "request-123"
    assert response["datasource_id"] == "warehouse"
    assert response["project_id"] == "project-a"
    assert response["rows"] == [["order-1", "12.50"]]


@pytest.mark.parametrize(
    "principal",
    [
        _principal(tenant_id="tenant-b"),
        _principal(project_ids=frozenset()),
    ],
)
async def test_concealed_authorization_denial_precedes_repository_lookup(
    principal: Principal,
) -> None:
    service, repository, executor, audit = _service()

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=principal,
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
        )

    assert raised.value.status_code == 404
    assert raised.value.code == "resource_not_found"
    assert repository.calls == []
    assert executor.calls == []
    assert audit.records == []


async def test_service_principal_requires_query_select_scope() -> None:
    denied = _principal(
        role=TenantRole.SERVICE,
        scopes=frozenset({"model.list"}),
    )
    allowed = _principal(
        role=TenantRole.SERVICE,
        scopes=frozenset({"query.select"}),
    )
    service, repository, executor, _ = _service()

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=denied,
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
        )
    assert raised.value.status_code == 403
    assert raised.value.code == "query_not_authorized"

    await service.execute(
        principal=allowed,
        tenant_id="tenant-a",
        project_id="project-a",
        datasource_id="warehouse",
        sql="SELECT * FROM orders",
    )
    assert repository.calls == [
        ("tenant-a", "project-a", "warehouse")
    ]
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    "bindings",
    [
        AthenaRoleBindings(),
        _bindings(tenant_id="tenant-b"),
        _bindings(project_id="project-b"),
        _bindings(
            role_arn=(
                "arn:aws:iam::123456789012:role/"
                "unapproved-athena-project-a"
            )
        ),
    ],
)
async def test_unapproved_binding_fails_before_audit_or_execution(
    bindings: AthenaRoleBindings,
) -> None:
    service, repository, executor, audit = _service(bindings=bindings)

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "datasource_binding_invalid"
    assert len(repository.calls) == 1
    assert executor.calls == []
    assert audit.records == []


async def test_policy_rejection_is_audited_without_query_text() -> None:
    service, _, executor, audit = _service()
    sql = "DELETE FROM orders"

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql=sql,
            request_id="rejected-123",
        )

    assert raised.value.status_code == 400
    assert raised.value.code == "query_policy_rejected"
    assert executor.calls == []
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record["event_type"] is AuditEventType.QUERY_REJECTED
    assert record["request_id"] == "rejected-123"
    assert record["data"] == {
        "datasource_id": "warehouse",
        "query_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        "reason": "query_policy_rejected",
    }
    assert sql not in repr(record)


async def test_missing_durable_audit_fails_closed_before_execution() -> None:
    audit = _AuditTrail(durable_enabled=False)
    service, _, executor, _ = _service(audit=audit)

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "query_audit_unavailable"
    assert executor.calls == []
    assert audit.records == []


async def test_audit_append_failure_prevents_execution() -> None:
    audit = _AuditTrail(error=RuntimeError("audit unavailable"))
    service, _, executor, _ = _service(audit=audit)

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
        )

    assert raised.value.code == "query_audit_unavailable"
    assert executor.calls == []


async def test_executor_failure_produces_terminal_failure_audit() -> None:
    executor = _Executor(
        error=AthenaExecutionError(
            "athena_scan_limit_exceeded",
            "Athena query exceeded the configured scan limit.",
        )
    )
    service, _, _, audit = _service(executor=executor)

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id="failed-123",
        )

    assert raised.value.status_code == 502
    assert raised.value.code == "athena_scan_limit_exceeded"
    assert [record["event_type"] for record in audit.records] == [
        AuditEventType.QUERY_REQUEST,
        AuditEventType.QUERY_RESULT,
    ]
    assert audit.records[-1]["data"]["status"] == "failed"
    assert (
        audit.records[-1]["data"]["failure_code"]
        == "athena_scan_limit_exceeded"
    )


async def test_unexpected_executor_failure_is_sanitized_and_audited() -> None:
    executor = _Executor(error=ValueError("sensitive SDK response"))
    service, _, _, audit = _service(executor=executor)

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "query_execution_unavailable"
    assert "sensitive" not in raised.value.message
    assert audit.records[-1]["data"]["failure_code"] == (
        "query_execution_unavailable"
    )


@pytest.mark.parametrize("request_id", ["", " padded", "bad\nid", "x" * 129])
async def test_invalid_request_id_is_rejected_before_authorization(
    request_id: str,
) -> None:
    service, repository, executor, audit = _service()

    with pytest.raises(QueryServiceError) as raised:
        await service.execute(
            principal=_principal(),
            tenant_id="tenant-a",
            project_id="project-a",
            datasource_id="warehouse",
            sql="SELECT * FROM orders",
            request_id=request_id,
        )

    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_request_id"
    assert repository.calls == []
    assert executor.calls == []
    assert audit.records == []
