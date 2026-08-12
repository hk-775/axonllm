"""Network-free tests for initialization and query launch domains."""

from __future__ import annotations

import hashlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import launch_activity_domains as framework
import launch_activity_worker as worker
from launch_domains import initialization, query
from launch_domains.common import RuntimeObservation


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=2)
OWNER = "a" * 64
REGION = "us-east-1"
ACCOUNT = "123456789012"
RUNTIME = "AxonLLMRuntime-abcdefghij"


def _ownership(
    *,
    faults: list[str] | None = None,
    fixtures: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "faultIds": faults or [],
        "fixtureIds": fixtures or [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _task(operation: str) -> worker.ActionTask:
    gate = worker.ACTION_TO_GATE[operation]
    parameters: dict[str, Any]
    if gate == initialization.CONTROL_GATE:
        parameters = {
            "startupDeadlineSeconds": 3,
            "faultTtlSeconds": 60,
        }
    else:
        parameters = {
            "tenantId": "tenant-a",
            "projectId": "project-a",
            "datasourceId": "datasource-a",
            "selectSql": "SELECT id FROM allowed_table",
            "maxRows": 10,
            "scanLimitBytes": 4096,
        }
    return worker.ActionTask(
        payload={
            "owner": {"expiresAt": EXPIRES.isoformat(timespec="seconds")},
            "release": {"commit": "c" * 40},
            "binding": {
                "tenantId": "tenant-a",
                "projectId": "project-a",
                "runtimeArn": (f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME}"),
                "runtimeEndpointArn": (
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME}/runtime-endpoint/production"
                ),
            },
            "parameters": parameters,
        },
        gate=gate,
        operation=operation,
        owner_id=OWNER,
        correlation_id=hashlib.sha256(f"{OWNER}:{gate}:{operation}".encode()).hexdigest()[:32],
        idempotency_key=hashlib.sha256(f"{OWNER}:{operation}".encode()).hexdigest(),
        expires_at=EXPIRES,
        fence_token=17,
        request_sha256="d" * 64,
    )


def _context() -> worker.HandlerContext:
    return worker.HandlerContext(
        aws=FakeTransport(),
        region=REGION,
        state_store=SimpleNamespace(),
        owner_state=None,
        cancellation=worker.CancellationToken(threading.Event()),
        fence_token=17,
    )


class FakeTransport:
    """AWS transport that makes any accidental network use fail."""

    def call(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("unexpected AWS transport call")


class FakeOpener:
    """HTTP opener marker used by the fake launch session."""

    def open(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unexpected HTTP open")


class FakeLedger:
    def __init__(self) -> None:
        self.observations: list[SimpleNamespace] = []
        self.fault_active = False
        self.checkpoint_active = False
        self.unavailable = False

    def add(self, kind: str, **payload: Any) -> None:
        self.observations.append(SimpleNamespace(kind=kind, payload=payload))

    def read_active_fault(self, _binding: Any, name: str) -> object | None:
        assert name == initialization.CONTROL_NAME
        return object() if self.fault_active else None

    def read_active_checkpoint(
        self,
        _binding: Any,
        name: str,
    ) -> object | None:
        assert name == query.CONTROL_NAME
        return object() if self.checkpoint_active else None


class FakeSession:
    def __init__(
        self,
        *,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        ledger: FakeLedger,
        calls: list[str],
        control_gate: str,
        fence_token: int | None = None,
        opener: FakeOpener | None = None,
    ) -> None:
        del context
        self.task = task
        self.ledger = ledger
        self.calls = calls
        self.control_gate = control_gate
        self.fence_token = task.fence_token if fence_token is None else fence_token
        self.opener = opener or FakeOpener()
        self.binding = SimpleNamespace(
            correlation_id=hashlib.sha256(f"{task.owner_id}:{control_gate}:runtime-control".encode()).hexdigest()[:32]
        )

    def write_control(
        self,
        *,
        control_type: str,
        name: str,
        parameters: dict[str, Any],
        active: bool,
    ) -> int:
        self.calls.append(f"control:{control_type}:{name}:{active}")
        if control_type == "fault":
            assert parameters == {"delay_seconds": 3}
            self.ledger.fault_active = active
        else:
            assert parameters == {"hold_seconds": 30}
            self.ledger.checkpoint_active = active
        return 1

    def observations(self, *required: str) -> tuple[Any, ...]:
        self.calls.append(f"observations:{','.join(required)}")
        if self.ledger.unavailable:
            raise worker.DomainTaskFailure(
                "RehearsalEvidenceUnavailable",
                retryable=True,
            )
        if not set(required).issubset({value.kind for value in self.ledger.observations}):
            raise worker.DomainTaskFailure(
                "RehearsalEvidenceUnavailable",
                retryable=True,
            )
        return tuple(self.ledger.observations)

    def invoke(
        self,
        payload: dict[str, Any],
        *,
        operation: str,
        routing_strategy: str | None = None,
        dependency: str | None = None,
        timeout_seconds: float = 30,
    ) -> RuntimeObservation:
        del routing_strategy, dependency, timeout_seconds
        self.calls.append(f"invoke:{operation}:{payload['action']}")
        if operation == "induce-initialization-timeout":
            self.ledger.add(
                "startup-attempt",
                boot_id=self.binding.correlation_id,
                phase="started",
                runtime_id="runtime-old",
            )
            self.ledger.add(
                "startup-attempt",
                boot_id=self.binding.correlation_id,
                phase="timed-out",
                exit_code=124,
                runtime_id="runtime-old",
            )
            return RuntimeObservation(None, None, transport_error=True)
        if operation in {
            "observe-runtime-replacement",
            "verify-replacement-ready",
        }:
            self.ledger.add(
                "startup-attempt",
                boot_id=self.binding.correlation_id,
                phase="ready",
                runtime_id="runtime-new",
            )
            return RuntimeObservation(
                200,
                {"ready": True, "status": "ready"},
            )
        if operation == "reject-query-boundaries":
            sql = payload["sql"]
            if sql == "SELECT id FROM allowed_table":
                return RuntimeObservation(
                    200,
                    {
                        "row_count": 2,
                        "statistics": {"data_scanned_bytes": 1024},
                    },
                )
            return RuntimeObservation(
                403 if "out_of_scope" in sql else 400,
                {"detail": {"code": "query_policy_rejected"}},
            )
        if operation == "interrupt-query":
            request_id = payload["request_id"]
            self.ledger.add(
                "query-lifecycle",
                phase="reserved",
                request_id=request_id,
                reservation_units=4096,
            )
            self.ledger.add(
                "query-lifecycle",
                phase="interrupted",
                request_id=request_id,
            )
            self.ledger.add(
                "query-lifecycle",
                phase="reconciled",
                request_id=request_id,
                reservation_units=0,
                terminal_state="CANCELLED",
            )
            return RuntimeObservation(None, None, transport_error=True)
        if operation == "verify-deferred-accounting":
            request_id = payload["request_id"]
            self.ledger.add(
                "query-lifecycle",
                phase="deferred",
                request_id=request_id,
                reservation_units=0,
                terminal_state="DEFERRED",
            )
            return RuntimeObservation(
                503,
                {"detail": {"code": "datasource_binding_invalid"}},
            )
        raise AssertionError(f"unexpected operation {operation}")


def _factory(
    ledger: FakeLedger,
    calls: list[str],
):
    def build(**kwargs: Any) -> FakeSession:
        return FakeSession(
            **kwargs,
            ledger=ledger,
            calls=calls,
        )

    return build


def _run_actions(
    domain: Any,
    operations: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    state: dict[str, Any] = {}
    ownership = _ownership()
    evidence: list[dict[str, Any]] = []
    for operation in operations:
        result = domain.handle_action(
            operation=operation,
            task=_task(operation),
            context=_context(),
            state=state,
            ownership=ownership,
        )
        state = dict(result.state)
        ownership = dict(result.ownership)
        evidence.append(dict(result.evidence))
    return state, ownership, evidence


def test_initialization_success_order_and_actual_replacement_evidence() -> None:
    ledger = FakeLedger()
    calls: list[str] = []
    domain = initialization.InitializationDomain(_factory(ledger, calls))

    state, ownership, evidence = _run_actions(
        domain,
        initialization.OPERATIONS,
    )

    assert state["completed"] == list(initialization.OPERATIONS)
    assert state["fenceToken"] == 17
    assert state["timedOutRuntimeId"] == "runtime-old"
    assert state["replacementRuntimeId"] == "runtime-new"
    assert state["controlActive"] is False
    assert ownership == _ownership()
    assert evidence == [
        {
            "startupDeadlineSeconds": 3,
            "timedOutRuntimeId": "runtime-old",
        },
        {"timeoutExitCode": 124},
        {"replacementRuntimeId": "runtime-new"},
        {"replacementReadyStatusCode": 200},
    ]
    assert calls.index("invoke:induce-initialization-timeout:readiness") < calls.index("observations:startup-attempt")
    assert "control:fault:startup-delay:False" in calls


def test_query_success_uses_real_requests_and_ledger_results() -> None:
    ledger = FakeLedger()
    calls: list[str] = []
    domain = query.QueryDomain(_factory(ledger, calls))

    state, ownership, evidence = _run_actions(
        domain,
        query.OPERATIONS,
    )

    assert state["completed"] == list(query.OPERATIONS)
    assert state["fenceToken"] == 17
    assert ownership["fixtureIds"] == [f"{OWNER}:{query.CONTROL_NAME}"]
    assert evidence[0] == {
        "mutationStatusCode": 400,
        "multipleStatementsStatusCode": 400,
        "outOfDatasourceStatusCode": 403,
        "requestedMaxRows": 10,
        "returnedRowCount": 2,
        "scanLimitBytes": 4096,
        "observedBytesScanned": 1024,
    }
    assert evidence[2] == {
        "terminalState": "CANCELLED",
        "reservationUnitsAfter": 0,
        "durableResultAuditCount": 1,
    }
    assert evidence[3] == {
        "unavailableBindingState": "DEFERRED",
        "unavailableBindingReservationReleased": False,
    }
    assert calls.index("control:checkpoint:query-after-reservation:True") < calls.index("invoke:interrupt-query:query")


@pytest.mark.parametrize(
    ("domain", "operation"),
    [
        (
            initialization.InitializationDomain(_factory(FakeLedger(), [])),
            "observe-exit-124",
        ),
        (
            query.QueryDomain(_factory(FakeLedger(), [])),
            "interrupt-query",
        ),
    ],
)
def test_domains_reject_out_of_order_actions(
    domain: Any,
    operation: str,
) -> None:
    with pytest.raises(worker.DomainTaskFailure) as raised:
        domain.handle_action(
            operation=operation,
            task=_task(operation),
            context=_context(),
            state={},
            ownership=_ownership(),
        )
    assert raised.value.code == "DomainActionOutOfOrder"


def test_initialization_fails_closed_when_ledger_evidence_unavailable() -> None:
    ledger = FakeLedger()
    calls: list[str] = []
    domain = initialization.InitializationDomain(_factory(ledger, calls))
    first = domain.handle_action(
        operation=initialization.OPERATIONS[0],
        task=_task(initialization.OPERATIONS[0]),
        context=_context(),
        state={},
        ownership=_ownership(),
    )
    ledger.unavailable = True

    with pytest.raises(worker.DomainTaskFailure) as raised:
        domain.handle_action(
            operation=initialization.OPERATIONS[1],
            task=_task(initialization.OPERATIONS[1]),
            context=_context(),
            state=first.state,
            ownership=first.ownership,
        )
    assert raised.value.code == "RehearsalEvidenceUnavailable"


def test_cleanup_is_idempotent_verifies_controls_and_clears_exact_ownership() -> None:
    init_ledger = FakeLedger()
    query_ledger = FakeLedger()
    init_domain = initialization.InitializationDomain(_factory(init_ledger, []))
    query_domain = query.QueryDomain(_factory(query_ledger, []))
    init_state, _, _ = _run_actions(
        init_domain,
        initialization.OPERATIONS[:1],
    )
    query_state, query_ownership, _ = _run_actions(
        query_domain,
        query.OPERATIONS[:2],
    )
    owner = framework.OwnerBinding(
        owner_id=OWNER,
        expires_at=EXPIRES,
        expires_at_text=EXPIRES.isoformat(timespec="seconds"),
    )

    init_result = init_domain.cleanup(
        owner=owner,
        context=_context(),
        state=init_state,
        ownership=_ownership(faults=[f"{OWNER}:{initialization.CONTROL_NAME}"]),
    )
    query_result = query_domain.cleanup(
        owner=owner,
        context=_context(),
        state=query_state,
        ownership=query_ownership,
    )
    repeated = query_domain.cleanup(
        owner=owner,
        context=_context(),
        state=query_result.state,
        ownership=query_result.ownership,
    )

    assert init_ledger.fault_active is False
    assert query_ledger.checkpoint_active is False
    assert init_result.cleared_fault_ids == [f"{OWNER}:{initialization.CONTROL_NAME}"]
    assert query_result.cleared_fixture_ids == [f"{OWNER}:{query.CONTROL_NAME}"]
    assert repeated.cleared_fixture_ids == []
    assert repeated.ownership == _ownership()
