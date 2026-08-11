"""Canonical authorization, audit, and execution for read-only queries."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from src.gateway.auth.authorization import (
    Action,
    AuthorizationDenied,
    ResourceRef,
    require_authorized,
)
from src.gateway.models import Principal
from src.gateway.security.audit_trail import (
    AuditEventType,
    AuditTrail,
)

from .admission import (
    QueryAdmissionController,
    QueryAdmissionError,
    QueryAdmissionLease,
)
from .athena import (
    AthenaExecutionError,
    AthenaExecutor,
    AthenaQueryResult,
)
from .models import AthenaRoleBindings
from .repository import (
    DatasourceRepository,
    DatasourceStoreUnavailable,
)
from .sql_policy import QueryPolicyError, validate_athena_select


class QueryServiceError(RuntimeError):
    """Safe query-plane error carrying an HTTP-compatible status."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


_ATHENA_STATUS = {
    "invalid_query_limit": 400,
    "athena_query_timeout": 504,
    "athena_query_failed": 502,
    "athena_scan_limit_exceeded": 502,
    "athena_start_failed": 502,
    "athena_status_failed": 502,
    "athena_results_failed": 502,
}


class QueryService:
    """Execute only project-bound, audited `query.select` operations."""

    def __init__(
        self,
        *,
        repository: DatasourceRepository,
        bindings: AthenaRoleBindings,
        executor: AthenaExecutor,
        audit_trail: AuditTrail,
        require_durable_audit: bool = True,
        admission: QueryAdmissionController | None = None,
        require_durable_admission: bool = False,
    ) -> None:
        self.repository = repository
        self.bindings = bindings
        self.executor = executor
        self.audit_trail = audit_trail
        self.require_durable_audit = require_durable_audit
        self.admission = admission
        self.require_durable_admission = require_durable_admission

    def _require_audit(self) -> None:
        if (
            self.require_durable_audit
            and not self.audit_trail.durable_enabled
        ):
            raise QueryServiceError(
                503,
                "query_audit_unavailable",
                "Durable query audit is unavailable.",
            )

    def _require_admission(self) -> None:
        if self.require_durable_admission and self.admission is None:
            raise QueryServiceError(
                503,
                "query_admission_unavailable",
                "Distributed query admission is unavailable.",
            )

    async def _record(
        self,
        event_type: AuditEventType,
        *,
        principal: Principal,
        project_id: str,
        request_id: str,
        data: dict[str, Any],
    ) -> None:
        self._require_audit()
        try:
            await self.audit_trail.record(
                event_type=event_type,
                user_id=principal.principal_id,
                project_id=project_id,
                request_id=request_id,
                data=data,
                tenant_id=principal.tenant_id,
            )
        except Exception as exc:
            raise QueryServiceError(
                503,
                "query_audit_unavailable",
                "Durable query audit is unavailable.",
            ) from exc

    @staticmethod
    def _request_id(value: str | None) -> str:
        if value is None:
            return f"qry_{uuid.uuid4().hex}"
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 for character in value)
        ):
            raise QueryServiceError(
                400,
                "invalid_request_id",
                "request_id is invalid.",
            )
        return value

    @staticmethod
    def _identity(value: object, name: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 for character in value)
        ):
            raise QueryServiceError(
                400,
                "invalid_query_request",
                f"{name} must be a non-empty identifier.",
            )
        return value

    async def execute(
        self,
        *,
        principal: Principal,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        sql: object,
        max_rows: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = self._identity(tenant_id, "tenant_id")
        project_id = self._identity(project_id, "project_id")
        datasource_id = self._identity(
            datasource_id,
            "datasource_id",
        )
        resolved_request_id = self._request_id(request_id)
        resource = ResourceRef(
            resource_type="datasource",
            resource_id=datasource_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        try:
            require_authorized(
                principal,
                Action.QUERY_SELECT,
                resource,
            )
        except AuthorizationDenied as exc:
            raise QueryServiceError(
                exc.decision.status_code,
                "resource_not_found"
                if exc.decision.conceal_resource
                else "query_not_authorized",
                "The requested resource was not found."
                if exc.decision.conceal_resource
                else "The principal is not authorized to run queries.",
            ) from exc
        try:
            datasource = await self.repository.get(
                tenant_id,
                project_id,
                datasource_id,
            )
        except DatasourceStoreUnavailable as exc:
            raise QueryServiceError(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            ) from exc
        if datasource is None:
            raise QueryServiceError(
                404,
                "resource_not_found",
                "The requested resource was not found.",
            )
        if not datasource.enabled:
            raise QueryServiceError(
                403,
                "datasource_disabled",
                "The datasource is disabled.",
            )
        if not self.bindings.allows(
            tenant_id,
            project_id,
            datasource.role_arn,
        ):
            raise QueryServiceError(
                503,
                "datasource_binding_invalid",
                "Datasource role binding is not approved by the deployment.",
            )

        try:
            validated = validate_athena_select(sql, datasource)
        except QueryPolicyError as exc:
            raw_hash = hashlib.sha256(
                str(sql).encode("utf-8", errors="replace")
            ).hexdigest()
            await self._record(
                AuditEventType.QUERY_REJECTED,
                principal=principal,
                project_id=project_id,
                request_id=resolved_request_id,
                data={
                    "datasource_id": datasource_id,
                    "query_sha256": raw_hash,
                    "reason": "query_policy_rejected",
                },
            )
            raise QueryServiceError(
                400,
                "query_policy_rejected",
                str(exc),
            ) from exc

        await self._record(
            AuditEventType.QUERY_REQUEST,
            principal=principal,
            project_id=project_id,
            request_id=resolved_request_id,
            data={
                "datasource_id": datasource_id,
                "query_sha256": validated.sha256,
                "table_count": validated.table_count,
                "requested_max_rows": max_rows,
            },
        )
        self._require_admission()
        lease: QueryAdmissionLease | None = None
        execution_id: str | None = None
        if self.admission is not None:
            try:
                lease = await self.admission.acquire(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    principal_id=principal.principal_id,
                    request_id=resolved_request_id,
                    datasource_id=datasource_id,
                    query_sha256=validated.sha256,
                )
            except QueryAdmissionError as exc:
                await self._record(
                    AuditEventType.QUERY_REJECTED,
                    principal=principal,
                    project_id=project_id,
                    request_id=resolved_request_id,
                    data={
                        "datasource_id": datasource_id,
                        "query_sha256": validated.sha256,
                        "reason": exc.code,
                    },
                )
                raise QueryServiceError(
                    exc.status_code,
                    exc.code,
                    exc.message,
                ) from exc

        async def _mark_started(value: str) -> None:
            nonlocal execution_id
            execution_id = value
            if self.admission is not None and lease is not None:
                await self.admission.mark_started(lease, value)

        try:
            execution_kwargs: dict[str, Any] = {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "principal_id": principal.principal_id,
                "request_id": resolved_request_id,
                "max_rows": max_rows,
            }
            if lease is not None:
                execution_kwargs["on_started"] = _mark_started
            result = await self.executor.execute(
                validated,
                datasource,
                **execution_kwargs,
            )
        except asyncio.CancelledError:
            if self.admission is not None and lease is not None:
                await asyncio.shield(
                    self.admission.finalize(
                        lease,
                        status="cancelled",
                        actual_scan_bytes=0,
                        execution_id=execution_id,
                        failure_code="query_cancelled",
                    )
                )
            raise
        except AthenaExecutionError as exc:
            if self.admission is not None and lease is not None:
                try:
                    await self.admission.finalize(
                        lease,
                        status="failed",
                        actual_scan_bytes=0,
                        execution_id=execution_id,
                        failure_code=exc.code,
                    )
                except QueryAdmissionError as admission_exc:
                    raise QueryServiceError(
                        admission_exc.status_code,
                        admission_exc.code,
                        admission_exc.message,
                    ) from admission_exc
            await self._record(
                AuditEventType.QUERY_RESULT,
                principal=principal,
                project_id=project_id,
                request_id=resolved_request_id,
                data={
                    "datasource_id": datasource_id,
                    "query_sha256": validated.sha256,
                    "status": "failed",
                    "failure_code": exc.code,
                },
            )
            raise QueryServiceError(
                _ATHENA_STATUS.get(exc.code, 503),
                exc.code,
                exc.message,
            ) from exc
        except Exception as exc:
            if self.admission is not None and lease is not None:
                try:
                    await self.admission.finalize(
                        lease,
                        status="failed",
                        actual_scan_bytes=0,
                        execution_id=execution_id,
                        failure_code="query_execution_unavailable",
                    )
                except QueryAdmissionError as admission_exc:
                    raise QueryServiceError(
                        admission_exc.status_code,
                        admission_exc.code,
                        admission_exc.message,
                    ) from admission_exc
            await self._record(
                AuditEventType.QUERY_RESULT,
                principal=principal,
                project_id=project_id,
                request_id=resolved_request_id,
                data={
                    "datasource_id": datasource_id,
                    "query_sha256": validated.sha256,
                    "status": "failed",
                    "failure_code": "query_execution_unavailable",
                },
            )
            raise QueryServiceError(
                503,
                "query_execution_unavailable",
                "Query execution is temporarily unavailable.",
            ) from exc
        if self.admission is not None and lease is not None:
            try:
                await self.admission.finalize(
                    lease,
                    status="succeeded",
                    actual_scan_bytes=result.data_scanned_bytes,
                    execution_id=result.query_execution_id,
                )
            except QueryAdmissionError as exc:
                raise QueryServiceError(
                    exc.status_code,
                    exc.code,
                    exc.message,
                ) from exc
        await self._record_result(
            principal=principal,
            project_id=project_id,
            request_id=resolved_request_id,
            datasource_id=datasource_id,
            query_sha256=validated.sha256,
            result=result,
        )
        response = result.to_dict()
        response.update(
            {
                "request_id": resolved_request_id,
                "datasource_id": datasource_id,
                "project_id": project_id,
            }
        )
        return response

    async def _record_result(
        self,
        *,
        principal: Principal,
        project_id: str,
        request_id: str,
        datasource_id: str,
        query_sha256: str,
        result: AthenaQueryResult,
    ) -> None:
        await self._record(
            AuditEventType.QUERY_RESULT,
            principal=principal,
            project_id=project_id,
            request_id=request_id,
            data={
                "datasource_id": datasource_id,
                "query_sha256": query_sha256,
                "status": "succeeded",
                "query_execution_id": result.query_execution_id,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "data_scanned_bytes": result.data_scanned_bytes,
                "engine_execution_ms": result.engine_execution_ms,
                "result_bytes": result.result_bytes,
            },
        )
