"""Query-boundary, interruption, and reconciliation launch domain."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker

from launch_domains.common import LaunchSession, copied_ownership, owned_id


OPERATIONS = framework.DOMAIN_OPERATIONS["query"]
CONTROL_GATE = "queryBoundaryLimitsAndReconciliation"
CONTROL_NAME = "query-after-reservation"


def _failure(code: str) -> worker.DomainTaskFailure:
    return worker.DomainTaskFailure(code, retryable=True)


def _parameters(task: worker.ActionTask) -> Mapping[str, Any]:
    value = task.payload.get("parameters")
    if type(value) is not dict:
        raise worker.HandlerContractError from None
    required = {
        "tenantId",
        "projectId",
        "datasourceId",
        "selectSql",
        "maxRows",
        "scanLimitBytes",
    }
    if set(value) != required:
        raise worker.HandlerContractError from None
    return value


def _request(
    parameters: Mapping[str, Any],
    *,
    sql: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "action": "query",
        "datasource_id": parameters["datasourceId"],
        "sql": sql,
        "max_rows": parameters["maxRows"],
        "request_id": request_id,
    }


def _client_error(observation: Any) -> int:
    status = observation.status_code
    if not isinstance(status, int) or not 400 <= status < 500:
        raise _failure("QueryBoundaryNotRejected")
    return status


def _lifecycle(session: LaunchSession) -> list[Mapping[str, Any]]:
    return [
        observation.payload
        for observation in session.observations("query-lifecycle")
        if observation.kind == "query-lifecycle"
    ]


def _session_state(task: worker.ActionTask, fence_token: int) -> dict[str, Any]:
    binding = task.payload.get("binding")
    release = task.payload.get("release")
    if type(binding) is not dict or type(release) is not dict:
        raise worker.HandlerContractError from None
    commit = release.get("commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise worker.HandlerContractError from None
    return {
        "binding": dict(binding),
        "releaseCommit": commit,
        "controlGate": CONTROL_GATE,
        "fenceToken": fence_token,
    }


def _cleanup_task(
    *,
    owner: framework.OwnerBinding,
    state: Mapping[str, Any],
) -> worker.ActionTask:
    binding = state.get("binding")
    commit = state.get("releaseCommit")
    fence = state.get("fenceToken")
    if (
        type(binding) is not dict
        or not isinstance(commit, str)
        or isinstance(fence, bool)
        or not isinstance(fence, int)
        or fence < 1
    ):
        raise worker.HandlerContractError from None
    digest = hashlib.sha256(f"{owner.owner_id}:{CONTROL_GATE}:cleanup".encode("ascii")).hexdigest()
    return worker.ActionTask(
        payload={
            "owner": {"expiresAt": owner.expires_at_text},
            "binding": dict(binding),
            "release": {"commit": commit},
            "parameters": {},
        },
        gate=CONTROL_GATE,
        operation="cleanup",
        owner_id=owner.owner_id,
        correlation_id=digest[:32],
        idempotency_key=digest,
        expires_at=owner.expires_at,
        fence_token=None,
        request_sha256="0" * 64,
    )


class QueryDomain:
    """Execute real query requests and trust only runtime/ledger evidence."""

    def __init__(
        self,
        session_factory: Callable[..., LaunchSession] = LaunchSession,
    ) -> None:
        self._session_factory = session_factory

    def _session(
        self,
        *,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        fence_token: int | None = None,
    ) -> LaunchSession:
        return self._session_factory(
            task=task,
            context=context,
            control_gate=CONTROL_GATE,
            fence_token=fence_token,
        )

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        parameters = _parameters(task)
        next_state = dict(state)
        completed = next_state.get("completed", [])
        if type(completed) is not list or completed != list(OPERATIONS[: len(completed)]):
            raise worker.HandlerContractError from None
        if len(completed) >= len(OPERATIONS) or OPERATIONS[len(completed)] != operation:
            raise worker.DomainTaskFailure("DomainActionOutOfOrder")
        next_ownership = copied_ownership(ownership)
        session = self._session(task=task, context=context)

        if operation == "reject-query-boundaries":
            if context.fence_token is None:
                raise worker.HandlerContractError from None
            prefix = task.owner_id[:16]
            mutation = session.invoke(
                _request(
                    parameters,
                    sql="DELETE FROM axonllm_launch_rehearsal_boundary",
                    request_id=f"{prefix}-mutation",
                ),
                operation=operation,
            )
            select_sql = parameters["selectSql"].rstrip().rstrip(";")
            multiple = session.invoke(
                _request(
                    parameters,
                    sql=f"{select_sql}; SELECT 1",
                    request_id=f"{prefix}-multiple",
                ),
                operation=operation,
            )
            out_of_scope = session.invoke(
                _request(
                    parameters,
                    sql="SELECT * FROM axonllm_launch_rehearsal_out_of_scope",
                    request_id=f"{prefix}-out-of-scope",
                ),
                operation=operation,
            )
            selected = session.invoke(
                _request(
                    parameters,
                    sql=parameters["selectSql"],
                    request_id=f"{prefix}-select",
                ),
                operation=operation,
            )
            body = selected.body
            if selected.status_code != 200 or type(body) is not dict:
                raise _failure("QuerySelectEvidenceUnavailable")
            row_count = body.get("row_count")
            statistics = body.get("statistics")
            scanned = statistics.get("data_scanned_bytes") if type(statistics) is dict else None
            if (
                isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count < 0
                or row_count > parameters["maxRows"]
                or isinstance(scanned, bool)
                or not isinstance(scanned, int)
                or scanned < 0
                or scanned > parameters["scanLimitBytes"]
            ):
                raise _failure("QuerySelectEvidenceUnavailable")
            evidence = {
                "mutationStatusCode": _client_error(mutation),
                "multipleStatementsStatusCode": _client_error(multiple),
                "outOfDatasourceStatusCode": _client_error(out_of_scope),
                "requestedMaxRows": parameters["maxRows"],
                "returnedRowCount": row_count,
                "scanLimitBytes": parameters["scanLimitBytes"],
                "observedBytesScanned": scanned,
            }
            next_state.update(_session_state(task, context.fence_token) | {"boundaryEvidence": evidence})
        elif operation == "interrupt-query":
            request_id = owned_id(task, "interrupted-query")
            fixture_id = owned_id(task, CONTROL_NAME)
            session.write_control(
                control_type="checkpoint",
                name=CONTROL_NAME,
                parameters={"hold_seconds": 30},
                active=True,
            )
            invocation = session.invoke(
                _request(
                    parameters,
                    sql=parameters["selectSql"],
                    request_id=request_id,
                ),
                operation=operation,
                timeout_seconds=2.0,
            )
            if not invocation.transport_error:
                raise _failure("QueryInterruptionNotObserved")
            observations = _lifecycle(session)
            phases = {value.get("phase") for value in observations if value.get("request_id") == request_id}
            if not {"reserved", "interrupted"}.issubset(phases):
                raise _failure("QueryInterruptionEvidenceUnavailable")
            next_ownership["fixtureIds"] = sorted({*next_ownership["fixtureIds"], fixture_id})
            next_state.update(
                {
                    "checkpointActive": True,
                    "fixtureId": fixture_id,
                    "interruptedRequestId": request_id,
                }
            )
            evidence = {"interruptedRequestId": request_id}
        elif operation == "verify-terminal-reconciliation":
            request_id = next_state.get("interruptedRequestId")
            reconciled = [
                value
                for value in _lifecycle(session)
                if value.get("request_id") == request_id and value.get("phase") == "reconciled"
            ]
            if len(reconciled) != 1:
                raise _failure("QueryReconciliationEvidenceUnavailable")
            terminal = reconciled[0].get("terminal_state")
            units = reconciled[0].get("reservation_units")
            if terminal not in {"CANCELLED", "FAILED"} or units != 0:
                raise _failure("QueryReconciliationEvidenceUnavailable")
            evidence = {
                "terminalState": terminal,
                "reservationUnitsAfter": units,
                "durableResultAuditCount": len(reconciled),
            }
            next_state["reconciliationEvidence"] = evidence
        else:
            request_id = owned_id(task, "unavailable-binding")
            invocation = session.invoke(
                _request(
                    parameters,
                    sql=parameters["selectSql"],
                    request_id=request_id,
                ),
                operation=operation,
            )
            deferred = [
                value
                for value in _lifecycle(session)
                if value.get("request_id") == request_id and value.get("phase") == "deferred"
            ]
            if (
                invocation.status_code != 503
                or len(deferred) != 1
                or deferred[0].get("terminal_state") != "DEFERRED"
                or deferred[0].get("reservation_units") != 0
            ):
                raise _failure("DeferredAccountingEvidenceUnavailable")
            reservation_released = deferred[0]["reservation_units"] != 0
            evidence = {
                "unavailableBindingState": deferred[0]["terminal_state"],
                "unavailableBindingReservationReleased": reservation_released,
            }
            next_state["deferredEvidence"] = evidence

        next_state["completed"] = [*completed, operation]
        return framework.DomainActionResult(
            evidence=evidence,
            state=next_state,
            ownership=next_ownership,
        )

    def cleanup(
        self,
        *,
        owner: framework.OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainCleanupResult:
        next_state = dict(state)
        if "fenceToken" in next_state:
            task = _cleanup_task(owner=owner, state=next_state)
            session = self._session(
                task=task,
                context=context,
                fence_token=next_state["fenceToken"],
            )
            session.write_control(
                control_type="checkpoint",
                name=CONTROL_NAME,
                parameters={"hold_seconds": 30},
                active=False,
            )
            if (
                session.ledger.read_active_checkpoint(
                    session.binding,
                    CONTROL_NAME,
                )
                is not None
            ):
                raise _failure("RehearsalControlRemovalUnverified")
            next_state["checkpointActive"] = False
        return framework.DomainCleanupResult(
            state=next_state,
            ownership={
                "faultIds": [],
                "fixtureIds": [],
                "dlqCorrelationIds": [],
                "snapshots": {"model": None, "tenantConfig": None},
            },
            verified_complete=True,
            cleared_fault_ids=list(ownership["faultIds"]),
            cleared_fixture_ids=list(ownership["fixtureIds"]),
            removed_dlq_correlation_ids=list(ownership["dlqCorrelationIds"]),
        )


def create_domain(
    *,
    aws: worker.AwsTransport,
    region: str,
    lease_table_arn: str,
) -> QueryDomain:
    del aws, region, lease_table_arn
    return QueryDomain()
