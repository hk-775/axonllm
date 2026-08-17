"""Network-free contracts for the launch Activity domain framework."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import launch_activity_domains as domains
import launch_activity_worker as worker


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=2)
OWNER = "a" * 64
EXPIRY_TEXT = EXPIRES.isoformat(timespec="seconds")
REGION = "us-east-1"
LEASE_TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-launch-rehearsal-leases"


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _empty_ownership() -> dict[str, Any]:
    return {
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _owner_ownership(
    *,
    faults: list[str] | None = None,
    fixtures: list[str] | None = None,
    dlq: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ownerId": OWNER,
        "expiresAt": EXPIRY_TEXT,
        "faultIds": faults or [],
        "fixtureIds": fixtures or [],
        "dlqCorrelationIds": dlq or [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _evidence(operation: str) -> dict[str, Any]:
    return {
        name: ([] if name.endswith(("Phases", "Providers", "Strategies")) else 1)
        for name in worker.ACTION_EVIDENCE_FIELDS[operation]
    }


def _task(
    operation: str = "observe-exit-124",
) -> worker.ActionTask:
    cleanup = operation == "cleanup"
    gate = "cleanup" if cleanup else worker.ACTION_TO_GATE[operation]
    return worker.ActionTask(
        payload={
            "owner": {"expiresAt": EXPIRY_TEXT},
            "binding": {
                "runtimeArn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/AxonLLMRuntime-abcdefghij"),
                "stateTableArn": ("arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-agentcore-state"),
            },
            "parameters": ({"ownership": _owner_ownership()} if cleanup else {"operationInput": "bound"}),
        },
        gate=gate,
        operation=operation,
        owner_id=OWNER,
        correlation_id=hashlib.sha256(f"{OWNER}:{gate}:{operation}".encode()).hexdigest()[:32],
        idempotency_key=hashlib.sha256(f"{OWNER}:{operation}:idempotency".encode()).hexdigest(),
        expires_at=EXPIRES,
        fence_token=None if cleanup else 11,
        request_sha256="b" * 64,
    )


def _owner_state(
    value: dict[str, Any] | None = None,
    *,
    revision: int = 0,
) -> worker.OwnerState:
    state = {} if value is None else deepcopy(value)
    encoded = _canonical(state)
    return worker.OwnerState(
        owner_id=OWNER,
        expires_at=EXPIRES,
        revision=revision,
        value=state,
        sha256=hashlib.sha256(encoded.encode()).hexdigest(),
    )


class FakeAws:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, Any], float]] = []
        self.response: Any = {}

    def call(
        self,
        service: str,
        operation: str,
        *,
        region: str,
        parameters: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                service,
                operation,
                region,
                deepcopy(parameters),
                timeout_seconds,
            )
        )
        return self.response


class FakeStore:
    def __init__(self) -> None:
        self.pages: list[
            tuple[
                dict[str, Any] | None,
                worker.ExpiredOwnerPage,
            ]
        ] = []
        self.commits: list[worker.OwnerState] = []

    def list_expired_owners(
        self,
        *,
        limit: int,
        cursor: dict[str, Any] | None,
    ) -> worker.ExpiredOwnerPage:
        assert limit == domains.MAINTENANCE_PAGE_LIMIT
        expected_cursor, page = self.pages.pop(0)
        assert cursor == expected_cursor
        return page

    def commit_owner(
        self,
        *,
        previous: worker.OwnerState,
        next_state_json: str,
        next_state_sha256: str,
        next_revision: int,
    ) -> worker.OwnerState:
        assert hashlib.sha256(next_state_json.encode()).hexdigest() == (next_state_sha256)
        assert next_revision == previous.revision + 1
        committed = worker.OwnerState(
            owner_id=previous.owner_id,
            expires_at=previous.expires_at,
            revision=next_revision,
            value=json.loads(next_state_json),
            sha256=next_state_sha256,
        )
        self.commits.append(committed)
        return committed


class FakeDomain:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.action_ownership: dict[str, dict[str, Any]] = {}
        self.action_evidence: dict[str, dict[str, Any]] = {}
        self.cleanup_results: list[domains.DomainCleanupResult | Exception] = []
        self.cancel_on_cleanup: threading.Event | None = None

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: dict[str, Any],
        ownership: dict[str, Any],
    ) -> domains.DomainActionResult:
        del task, context
        self.calls.append(f"action:{self.name}:{operation}")
        return domains.DomainActionResult(
            evidence=deepcopy(self.action_evidence.get(operation, _evidence(operation))),
            state={"lastOperation": operation, **state},
            ownership=deepcopy(self.action_ownership.get(operation, ownership)),
        )

    def cleanup(
        self,
        *,
        owner: domains.OwnerBinding,
        context: worker.HandlerContext,
        state: dict[str, Any],
        ownership: dict[str, Any],
    ) -> domains.DomainCleanupResult:
        del context
        assert owner.owner_id == OWNER
        self.calls.append(f"cleanup:{self.name}")
        if self.cancel_on_cleanup is not None:
            self.cancel_on_cleanup.set()
        if self.cleanup_results:
            result = self.cleanup_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        snapshots = [item["ref"] for item in ownership["snapshots"].values() if item is not None]
        return domains.DomainCleanupResult(
            state=deepcopy(state),
            ownership=_empty_ownership(),
            verified_complete=True,
            restored_snapshot_refs=sorted(snapshots),
            cleared_fault_ids=list(ownership["faultIds"]),
            cleared_fixture_ids=list(ownership["fixtureIds"]),
            removed_dlq_correlation_ids=list(ownership["dlqCorrelationIds"]),
            primary_state_selected=(True if self.name == "recovery" else None),
            production_endpoint_status=("READY" if self.name == "initialization" else None),
        )


def _implementations(
    calls: list[str] | None = None,
) -> tuple[
    dict[str, FakeDomain],
    list[str],
]:
    observed = calls if calls is not None else []
    return (
        {name: FakeDomain(name, observed) for name in domains.DOMAIN_ORDER},
        observed,
    )


def _context(
    owner_state: worker.OwnerState | None,
    *,
    aws: FakeAws | None = None,
    store: FakeStore | None = None,
    event: threading.Event | None = None,
) -> worker.HandlerContext:
    return worker.HandlerContext(
        aws=aws or FakeAws(),
        region=REGION,
        state_store=store or FakeStore(),
        owner_state=owner_state,
        cancellation=worker.CancellationToken(event or threading.Event()),
        fence_token=11 if owner_state is not None else None,
    )


def _run_action(
    handler: domains.LaunchActivityDomains,
    operation: str,
    owner_state: worker.OwnerState | None = None,
) -> worker.HandlerOutcome:
    return handler.handle_action(
        _task(operation),
        _context(owner_state or _owner_state()),
    )


def _state_with_fault(
    handler: domains.LaunchActivityDomains,
    implementations: dict[str, FakeDomain],
) -> dict[str, Any]:
    fault_id = f"{OWNER}:provider-fault"
    implementations["provider_fallback"].action_ownership["inject-primary-provider-fault"] = {
        **_empty_ownership(),
        "faultIds": [fault_id],
    }
    outcome = _run_action(
        handler,
        "inject-primary-provider-fault",
    )
    assert outcome.state is not None
    return deepcopy(outcome.state)


def _cleanup_task_for(state: dict[str, Any]) -> worker.ActionTask:
    task = _task("cleanup")
    payload = deepcopy(task.payload)
    payload["parameters"]["ownership"] = deepcopy(state["ownership"])
    return worker.ActionTask(
        payload=payload,
        gate=task.gate,
        operation=task.operation,
        owner_id=task.owner_id,
        correlation_id=task.correlation_id,
        idempotency_key=task.idempotency_key,
        expires_at=task.expires_at,
        fence_token=task.fence_token,
        request_sha256=task.request_sha256,
    )


def test_registry_has_exact_six_domain_and_25_operation_coverage() -> None:
    assert tuple(domains.DOMAIN_OPERATIONS) == domains.DOMAIN_ORDER
    assert len(domains.DOMAIN_ORDER) == 6
    assert len(domains.OPERATION_TO_DOMAIN) == 25
    assert set(domains.OPERATION_TO_DOMAIN) == set(worker.ACTION_OPERATIONS)
    assert domains.CLEANUP_ORDER == tuple(reversed(domains.DOMAIN_ORDER))


@pytest.mark.parametrize(
    "operation",
    sorted(worker.ACTION_OPERATIONS),
)
def test_every_operation_dispatches_to_exact_injected_domain(
    operation: str,
) -> None:
    implementations, calls = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    outcome = _run_action(handler, operation)
    expected_domain = domains.OPERATION_TO_DOMAIN[operation]
    assert calls == [f"action:{expected_domain}:{operation}"]
    assert outcome.state is not None
    assert outcome.state["schema"] == domains.OWNER_STATE_SCHEMA
    assert set(outcome.state["domains"]) == set(domains.DOMAIN_ORDER)
    assert outcome.output["ownership"] == outcome.state["ownership"]
    worker._validate_handler_output(_task(operation), outcome.output)


def test_injected_registry_must_be_exact_and_callable() -> None:
    implementations, _ = _implementations()
    implementations.pop("security")
    with pytest.raises(worker.ConfigurationError):
        domains.LaunchActivityDomains(implementations)

    implementations, _ = _implementations()
    implementations["unknown"] = implementations["security"]
    with pytest.raises(worker.ConfigurationError):
        domains.LaunchActivityDomains(implementations)

    implementations, _ = _implementations()
    implementations["security"] = object()
    with pytest.raises(worker.ConfigurationError):
        domains.LaunchActivityDomains(implementations)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda state: state.update({"schema": "wrong"}),
        lambda state: state.update({"unknown": True}),
        lambda state: state["domains"].pop("security"),
        lambda state: state["ownership"]["faultIds"].append(f"{OWNER}:unattributed"),
        lambda state: state["cleanup"].update({"completedDomains": ["initialization"]}),
    ],
)
def test_owner_state_schema_and_derived_ownership_are_strict(
    tamper,
) -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    first = _run_action(handler, "observe-exit-124")
    assert first.state is not None
    invalid = deepcopy(first.state)
    tamper(invalid)
    with pytest.raises(worker.HandlerContractError):
        domains.validate_owner_state(
            invalid,
            owner_id=OWNER,
            expires_at=EXPIRES,
            expires_at_text=EXPIRY_TEXT,
        )


def test_owner_record_digest_and_utc_expiry_are_strict() -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    first = _run_action(handler, "observe-exit-124")
    assert first.state is not None
    forged = _owner_state(first.state, revision=1)
    forged = worker.OwnerState(
        owner_id=forged.owner_id,
        expires_at=forged.expires_at,
        revision=forged.revision,
        value=forged.value,
        sha256="c" * 64,
    )
    with pytest.raises(worker.HandlerContractError):
        handler.handle_action(
            _task("observe-exit-124"),
            _context(forged),
        )

    offset_state = deepcopy(first.state)
    offset_state["ownership"]["expiresAt"] = "2026-08-12T10:00:00-04:00"
    with pytest.raises(worker.HandlerContractError):
        domains.validate_owner_state(
            offset_state,
            owner_id=OWNER,
            expires_at=EXPIRES,
        )


def test_action_ownership_changes_are_domain_scoped_and_operation_limited() -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    fault_id = f"{OWNER}:fault"
    implementations["initialization"].action_ownership["observe-exit-124"] = {
        **_empty_ownership(),
        "faultIds": [fault_id],
    }
    with pytest.raises(worker.HandlerContractError):
        _run_action(handler, "observe-exit-124")

    implementations["initialization"].action_ownership["induce-initialization-timeout"] = {
        **_empty_ownership(),
        "faultIds": [fault_id],
    }
    outcome = _run_action(
        handler,
        "induce-initialization-timeout",
    )
    assert outcome.state is not None
    assert outcome.state["ownership"]["faultIds"] == [fault_id]
    assert outcome.state["domains"]["initialization"]["ownership"]["faultIds"] == [fault_id]


def test_result_envelope_rejects_secrets_and_output_bounds() -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    implementations["initialization"].action_evidence["observe-exit-124"] = {
        "timeoutExitCode": {"apiKey": "must-not-escape"}
    }
    with pytest.raises(worker.HandlerContractError):
        _run_action(handler, "observe-exit-124")

    implementations["initialization"].action_evidence["observe-exit-124"] = {
        "timeoutExitCode": "x" * (worker.MAX_EVIDENCE_BYTES + 1)
    }
    with pytest.raises(worker.HandlerContractError):
        _run_action(handler, "observe-exit-124")


def test_cleanup_is_reverse_order_verified_and_terminal_only() -> None:
    implementations, calls = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    state = _state_with_fault(handler, implementations)
    task = _cleanup_task_for(state)
    outcome = handler.handle_cleanup(
        task,
        _context(_owner_state(state, revision=1)),
    )
    assert calls[-len(domains.CLEANUP_ORDER) :] == [f"cleanup:{name}" for name in domains.CLEANUP_ORDER]
    assert outcome.state is not None
    assert outcome.state["cleanup"]["status"] == "COMPLETE"
    assert outcome.state["ownership"] == _owner_ownership()
    assert outcome.output["evidence"] == {
        "restoredSnapshotRefs": [],
        "clearedFaultIds": [f"{OWNER}:provider-fault"],
        "clearedFixtureIds": [],
        "redrivenDlqCorrelationIds": [],
        "removedDlqCorrelationIds": [],
        "primaryStateSelected": True,
        "productionEndpointStatus": "READY",
        "faultsRemaining": 0,
        "fixturesRemaining": 0,
        "correlatedDlqMessagesRemaining": 0,
    }
    worker._validate_handler_output(task, outcome.output)


def test_partial_cleanup_never_claims_success_and_retry_is_idempotent() -> None:
    implementations, calls = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    state = _state_with_fault(handler, implementations)
    fault_id = f"{OWNER}:provider-fault"
    implementations["provider_fallback"].cleanup_results.append(
        domains.DomainCleanupResult(
            state={"attempt": 1},
            ownership={
                **_empty_ownership(),
                "faultIds": [fault_id],
            },
            verified_complete=False,
        )
    )
    task = _cleanup_task_for(state)
    context = _context(_owner_state(state, revision=1))
    with pytest.raises(
        worker.DomainTaskFailure,
        match="CleanupIncomplete",
    ):
        handler.handle_cleanup(task, context)
    assert context.owner_state is not None
    assert context.owner_state.value == state
    assert calls[-2:] == [
        "cleanup:control_plane",
        "cleanup:provider_fallback",
    ]

    outcome = handler.handle_cleanup(task, context)
    assert outcome.state is not None
    assert outcome.state["cleanup"]["status"] == "COMPLETE"
    assert outcome.output["evidence"]["clearedFaultIds"] == [fault_id]


def test_cleanup_is_cancellation_aware_and_retains_owned_effects() -> None:
    implementations, calls = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    state = _state_with_fault(handler, implementations)
    event = threading.Event()
    implementations["control_plane"].cancel_on_cleanup = event
    with pytest.raises(worker.ShutdownError):
        handler.handle_cleanup(
            _cleanup_task_for(state),
            _context(
                _owner_state(state, revision=1),
                event=event,
            ),
        )
    assert calls[-1] == "cleanup:control_plane"
    assert state["ownership"]["faultIds"] == [f"{OWNER}:provider-fault"]


def test_cleanup_rejects_fabricated_clearance_or_remaining_effects() -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    state = _state_with_fault(handler, implementations)
    implementations["provider_fallback"].cleanup_results.append(
        domains.DomainCleanupResult(
            state={},
            ownership=_empty_ownership(),
            verified_complete=True,
            cleared_fault_ids=[f"{OWNER}:foreign-fault"],
        )
    )
    with pytest.raises(worker.HandlerContractError):
        handler.handle_cleanup(
            _cleanup_task_for(state),
            _context(_owner_state(state, revision=1)),
        )

    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    state = _state_with_fault(handler, implementations)
    fault_id = f"{OWNER}:provider-fault"
    implementations["provider_fallback"].cleanup_results.append(
        domains.DomainCleanupResult(
            state={},
            ownership={
                **_empty_ownership(),
                "faultIds": [fault_id],
            },
            verified_complete=True,
        )
    )
    with pytest.raises(worker.HandlerContractError):
        handler.handle_cleanup(
            _cleanup_task_for(state),
            _context(_owner_state(state, revision=1)),
        )


def test_expired_maintenance_persists_partial_state_and_resumes() -> None:
    implementations, calls = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    state = _state_with_fault(handler, implementations)
    fault_id = f"{OWNER}:provider-fault"
    implementations["provider_fallback"].cleanup_results.append(
        domains.DomainCleanupResult(
            state={"attempt": 1},
            ownership={
                **_empty_ownership(),
                "faultIds": [fault_id],
            },
            verified_complete=False,
        )
    )
    store = FakeStore()
    cursor = {"leaseKey": {"S": "owner#resume"}}
    store.pages.append(
        (
            None,
            worker.ExpiredOwnerPage(
                owners=(_owner_state(state, revision=1),),
                cursor=cursor,
            ),
        )
    )
    context = _context(None, store=store)
    first = handler.cleanup_expired_page(context)
    assert first == {
        "schema": domains.MAINTENANCE_RESULT_SCHEMA,
        "operation": "cleanup-expired",
        "page": 1,
        "pageLimit": domains.MAINTENANCE_PAGE_LIMIT,
        "scannedOwners": 1,
        "completedOwners": 0,
        "incompleteOwners": 1,
        "failedOwners": 0,
        "nextCursor": cursor,
    }
    partial = store.commits[-1]
    assert partial.value["ownership"]["faultIds"] == [fault_id]
    assert partial.value["cleanup"]["status"] == "IN_PROGRESS"
    assert partial.value["cleanup"]["completedDomains"] == ["control_plane"]

    store.pages.append(
        (
            cursor,
            worker.ExpiredOwnerPage(
                owners=(partial,),
                cursor=None,
            ),
        )
    )
    second = handler.cleanup_expired_page(
        context,
        cursor=first["nextCursor"],
        page_number=2,
    )
    assert second["page"] == 2
    assert second["completedOwners"] == 1
    assert second["nextCursor"] is None
    assert store.commits[-1].value["cleanup"]["status"] == "COMPLETE"
    resumed_calls = calls[calls.index("cleanup:provider_fallback") + 1 :]
    assert "cleanup:control_plane" not in resumed_calls


def test_expired_maintenance_rejects_invalid_state_without_domain_work() -> None:
    implementations, calls = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    invalid = _owner_state(
        {
            "schema": domains.OWNER_STATE_SCHEMA,
            "apiKey": "must-not-be-used",
        },
        revision=1,
    )
    store = FakeStore()
    store.pages.append(
        (
            None,
            worker.ExpiredOwnerPage(
                owners=(invalid,),
                cursor=None,
            ),
        )
    )
    with pytest.raises(
        worker.DomainTaskFailure,
        match="CleanupOwnerFailed",
    ):
        handler.handle_cleanup_expired(
            worker.MaintenanceTask(
                payload={},
                operation="cleanup-expired",
            ),
            _context(None, store=store),
        )
    assert calls == []
    assert store.commits == []


def test_expired_maintenance_resumes_from_task_cursor_and_bounds_pages() -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    cursor = {"leaseKey": {"S": "owner#resume"}}
    store = FakeStore()
    store.pages.append(
        (
            cursor,
            worker.ExpiredOwnerPage(owners=(), cursor=None),
        )
    )
    result = handler.handle_cleanup_expired(
        worker.MaintenanceTask(
            payload={
                "schema": worker.MAINTENANCE_SCHEMA,
                "operation": "cleanup-expired",
                "cursor": cursor,
                "page": 7,
            },
            operation="cleanup-expired",
        ),
        _context(None, store=store),
    )
    assert result["page"] == 8
    assert store.pages == []

    with pytest.raises(
        worker.DomainTaskFailure,
        match="CleanupPageLimitExceeded",
    ):
        handler.handle_cleanup_expired(
            worker.MaintenanceTask(
                payload={
                    "schema": worker.MAINTENANCE_SCHEMA,
                    "operation": "cleanup-expired",
                    "cursor": cursor,
                    "page": domains.MAX_MAINTENANCE_PAGES,
                },
                operation="cleanup-expired",
            ),
            _context(None, store=FakeStore()),
        )


@pytest.mark.parametrize(
    "cursor",
    [
        {"other": {"S": "owner#x"}},
        {"leaseKey": {"N": "1"}},
        {"leaseKey": {"S": "x" * 513}},
        {"leaseKey": {"S": "owner#x", "apiKey": "secret"}},
    ],
)
def test_maintenance_cursor_is_exact_and_bounded(
    cursor: dict[str, Any],
) -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    with pytest.raises(worker.HandlerContractError):
        handler.cleanup_expired_page(
            _context(None),
            cursor=cursor,
        )


def test_watchdog_publishes_exact_metric_and_bounded_status() -> None:
    implementations, _ = _implementations()
    handler = domains.LaunchActivityDomains(implementations)
    aws = FakeAws()
    result = handler.handle_watchdog(
        worker.MaintenanceTask(payload={}, operation="watchdog"),
        _context(None, aws=aws),
    )
    assert aws.calls == [
        (
            "cloudwatch",
            "put_metric_data",
            REGION,
            {
                "Namespace": "AxonLLM/LaunchCoordinator",
                "MetricData": [
                    {
                        "MetricName": "WatchdogHeartbeat",
                        "Dimensions": [
                            {
                                "Name": "Coordinator",
                                "Value": "AxonLLMLaunchCoordinator",
                            }
                        ],
                        "Value": 1.0,
                        "Unit": "Count",
                    }
                ],
            },
            domains.WATCHDOG_TIMEOUT_SECONDS,
        )
    ]
    assert result == {
        "schema": domains.MAINTENANCE_RESULT_SCHEMA,
        "operation": "watchdog",
        "heartbeatPublished": True,
        "namespace": "AxonLLM/LaunchCoordinator",
        "metricName": "WatchdogHeartbeat",
        "coordinator": "AxonLLMLaunchCoordinator",
    }
    assert len(_canonical(result).encode()) < 1024


def test_default_factory_fails_closed_when_any_domain_is_missing(
    monkeypatch,
) -> None:
    created: list[str] = []

    def import_module(name: str) -> ModuleType:
        if name.endswith(".routing"):
            raise ModuleNotFoundError(name)
        module = ModuleType(name)

        def create_domain(**_kwargs):
            created.append(name)
            implementations, _ = _implementations()
            return implementations[name.removeprefix("launch_domains.")]

        module.create_domain = create_domain
        return module

    monkeypatch.setattr(domains.importlib, "import_module", import_module)
    with pytest.raises(worker.ConfigurationError):
        domains.create_handler(
            aws=FakeAws(),
            region=REGION,
            lease_table_arn=LEASE_TABLE_ARN,
        )
    assert created == []
