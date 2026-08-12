#!/usr/bin/env python3
"""Compose concrete launch-rehearsal domains behind the Activity worker API."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias

import launch_activity_worker as worker


OWNER_STATE_SCHEMA = "axonllm.agentcore-launch-domain-state/v1"
MAINTENANCE_RESULT_SCHEMA = "axonllm.agentcore-launch-domain-maintenance-result/v1"
WATCHDOG_NAMESPACE = "AxonLLM/LaunchCoordinator"
WATCHDOG_METRIC_NAME = "WatchdogHeartbeat"
WATCHDOG_COORDINATOR = "AxonLLMLaunchCoordinator"
MAINTENANCE_PAGE_LIMIT = 10
MAX_MAINTENANCE_PAGES = worker.MAX_MAINTENANCE_PAGES
WATCHDOG_TIMEOUT_SECONDS = 8.0

DOMAIN_OPERATIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "initialization": (
            "induce-initialization-timeout",
            "observe-exit-124",
            "observe-runtime-replacement",
            "verify-replacement-ready",
        ),
        "query": (
            "reject-query-boundaries",
            "interrupt-query",
            "verify-terminal-reconciliation",
            "verify-deferred-accounting",
        ),
        "recovery": (
            "restore-state",
            "cutover-restored-state",
            "verify-restored-state",
            "rollback-primary-state",
            "verify-primary-state",
        ),
        "security": (
            "deliver-security-events",
            "verify-outbox-drained",
            "force-dead-letter",
            "verify-dead-letter-alarm",
            "redrive-dead-letter",
            "verify-redelivery",
        ),
        "routing": (
            "exercise-routing-strategies",
            "verify-routing-decisions",
        ),
        "provider_fallback": (
            "inject-primary-provider-fault",
            "verify-provider-fallback",
            "clear-primary-provider-fault",
            "verify-primary-provider-recovery",
        ),
        "control_plane": (
            "inject-control-plane-fault",
            "verify-control-plane-fail-closed",
            "clear-control-plane-fault",
            "verify-control-plane-recovery",
        ),
    }
)
DOMAIN_ORDER = tuple(DOMAIN_OPERATIONS)
CLEANUP_ORDER = tuple(reversed(DOMAIN_ORDER))
OPERATION_TO_DOMAIN = MappingProxyType(
    {operation: domain for domain, operations in DOMAIN_OPERATIONS.items() for operation in operations}
)
DEFAULT_DOMAIN_MODULES: Mapping[str, str] = MappingProxyType(
    {
        "initialization": "launch_domains.initialization",
        "query": "launch_domains.query",
        "recovery": "launch_domains.recovery",
        "security": "launch_domains.security",
        "routing": "launch_domains.routing",
        "provider_fallback": "launch_domains.provider_fallback",
        "control_plane": "launch_domains.control_plane",
    }
)

FAULT_ADD_OPERATIONS = frozenset(
    {
        "induce-initialization-timeout",
        "inject-primary-provider-fault",
        "inject-control-plane-fault",
    }
)
FAULT_REMOVE_OPERATIONS = frozenset(
    {
        "observe-runtime-replacement",
        "clear-primary-provider-fault",
        "clear-control-plane-fault",
    }
)
FIXTURE_ADD_OPERATIONS = frozenset(
    {
        "restore-state",
        "interrupt-query",
        "deliver-security-events",
        "exercise-routing-strategies",
    }
)
DLQ_ADD_OPERATIONS = frozenset({"force-dead-letter"})
DLQ_REMOVE_OPERATIONS = frozenset({"redrive-dead-letter", "verify-redelivery"})

_DOMAIN_OWNERSHIP_FIELDS = frozenset({"faultIds", "fixtureIds", "dlqCorrelationIds", "snapshots"})
_OWNER_OWNERSHIP_FIELDS = frozenset(
    {
        "ownerId",
        "expiresAt",
        "faultIds",
        "fixtureIds",
        "dlqCorrelationIds",
        "snapshots",
    }
)
_CLEANUP_FIELDS = frozenset(
    {
        "status",
        "baselineOwnership",
        "completedDomains",
        "restoredSnapshotRefs",
        "clearedFaultIds",
        "clearedFixtureIds",
        "redrivenDlqCorrelationIds",
        "removedDlqCorrelationIds",
        "primaryStateSelected",
        "productionEndpointStatus",
    }
)
_SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")

JsonObject: TypeAlias = dict[str, worker.JsonValue]


@dataclass(frozen=True)
class OwnerBinding:
    """Validated durable owner identity supplied to cleanup implementations."""

    owner_id: str
    expires_at: datetime
    expires_at_text: str


@dataclass(frozen=True)
class DomainActionResult:
    """A domain action's evidence and complete replacement domain state."""

    evidence: Mapping[str, worker.JsonValue]
    state: Mapping[str, worker.JsonValue]
    ownership: Mapping[str, worker.JsonValue]


@dataclass(frozen=True)
class DomainCleanupResult:
    """Verified cleanup partition for one domain."""

    state: Mapping[str, worker.JsonValue]
    ownership: Mapping[str, worker.JsonValue]
    verified_complete: bool
    restored_snapshot_refs: Sequence[str] = ()
    cleared_fault_ids: Sequence[str] = ()
    cleared_fixture_ids: Sequence[str] = ()
    redriven_dlq_correlation_ids: Sequence[str] = ()
    removed_dlq_correlation_ids: Sequence[str] = ()
    primary_state_selected: bool | None = None
    production_endpoint_status: str | None = None


class LaunchDomain(Protocol):
    """Concrete contract implemented by each isolated scenario domain."""

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> DomainActionResult: ...

    def cleanup(
        self,
        *,
        owner: OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> DomainCleanupResult: ...


def _assert_registry() -> None:
    operations = [operation for domain_operations in DOMAIN_OPERATIONS.values() for operation in domain_operations]
    if (
        len(operations) != 29
        or len(operations) != len(set(operations))
        or set(operations) != set(worker.ACTION_OPERATIONS)
        or set(DOMAIN_OPERATIONS) != set(DEFAULT_DOMAIN_MODULES)
    ):
        raise RuntimeError("launch domain registry does not match worker operations")


_assert_registry()


def _validate_json_tree(value: Any, *, depth: int = 0) -> None:
    if depth > worker.MAX_JSON_DEPTH:
        raise worker.HandlerContractError from None
    if value is None or type(value) in {bool, int, float, str}:
        if isinstance(value, float) and not math.isfinite(value):
            raise worker.HandlerContractError from None
        if isinstance(value, str) and len(value.encode("utf-8")) > worker.MAX_TASK_INPUT_BYTES:
            raise worker.HandlerContractError from None
        return
    if type(value) is list:
        if len(value) > 4096:
            raise worker.HandlerContractError from None
        for item in value:
            _validate_json_tree(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 1024:
            raise worker.HandlerContractError from None
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 256
                or any(ord(character) < 32 or ord(character) == 127 for character in key)
                or worker.SECRET_FIELD.fullmatch(key) is not None
            ):
                raise worker.HandlerContractError from None
            _validate_json_tree(item, depth=depth + 1)
        return
    raise worker.HandlerContractError from None


def _normalize_object(
    value: Any,
    *,
    maximum_bytes: int,
) -> JsonObject:
    if type(value) is not dict:
        raise worker.HandlerContractError from None
    _validate_json_tree(value)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise worker.HandlerContractError from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise worker.HandlerContractError from None
    normalized = json.loads(encoded)
    if type(normalized) is not dict:
        raise worker.HandlerContractError from None
    return normalized


def _exact(value: Mapping[str, Any], fields: frozenset[str] | set[str]) -> None:
    if set(value) != set(fields):
        raise worker.HandlerContractError from None


def _utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 40:
        raise worker.HandlerContractError from None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise worker.HandlerContractError from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise worker.HandlerContractError from None
    return parsed.astimezone(timezone.utc)


def _owned_identifier(value: Any, owner_id: str) -> str:
    if not isinstance(value, str) or worker.SAFE_ID.fullmatch(value) is None or not value.startswith(f"{owner_id}:"):
        raise worker.HandlerContractError from None
    return value


def _inventory(
    value: Any,
    *,
    owner_id: str | None = None,
    maximum: int = 256,
) -> list[str]:
    if type(value) not in {list, tuple} or len(value) > maximum:
        raise worker.HandlerContractError from None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise worker.HandlerContractError from None
        if owner_id is not None:
            item = _owned_identifier(item, owner_id)
        elif not item or len(item) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in item):
            raise worker.HandlerContractError from None
        result.append(item)
    if result != sorted(result) or len(result) != len(set(result)):
        raise worker.HandlerContractError from None
    return result


def _snapshot(value: Any, owner_id: str) -> JsonObject | None:
    if value is None:
        return None
    normalized = _normalize_object(value, maximum_bytes=2048)
    _exact(normalized, {"ref", "sha256", "revision"})
    reference = _owned_identifier(normalized["ref"], owner_id)
    digest = normalized["sha256"]
    revision = normalized["revision"]
    if (
        not isinstance(digest, str)
        or worker.SHA256.fullmatch(digest) is None
        or type(revision) is not int
        or revision < 0
        or revision > 2**63 - 1
    ):
        raise worker.HandlerContractError from None
    return {"ref": reference, "sha256": digest, "revision": revision}


def _domain_ownership(value: Any, owner_id: str) -> JsonObject:
    normalized = _normalize_object(value, maximum_bytes=32 * 1024)
    _exact(normalized, _DOMAIN_OWNERSHIP_FIELDS)
    snapshots = normalized["snapshots"]
    if type(snapshots) is not dict:
        raise worker.HandlerContractError from None
    _exact(snapshots, {"model", "tenantConfig"})
    return {
        "faultIds": _inventory(normalized["faultIds"], owner_id=owner_id),
        "fixtureIds": _inventory(normalized["fixtureIds"], owner_id=owner_id),
        "dlqCorrelationIds": _inventory(
            normalized["dlqCorrelationIds"],
            owner_id=owner_id,
        ),
        "snapshots": {
            "model": _snapshot(snapshots["model"], owner_id),
            "tenantConfig": _snapshot(snapshots["tenantConfig"], owner_id),
        },
    }


def _owner_ownership(
    value: Any,
    *,
    owner_id: str,
    expires_at: datetime,
    expires_at_text: str | None = None,
) -> JsonObject:
    normalized = _normalize_object(value, maximum_bytes=48 * 1024)
    _exact(normalized, _OWNER_OWNERSHIP_FIELDS)
    if normalized["ownerId"] != owner_id:
        raise worker.HandlerContractError from None
    stored_expiry = normalized["expiresAt"]
    if not isinstance(stored_expiry, str) or _utc_timestamp(stored_expiry) != expires_at:
        raise worker.HandlerContractError from None
    if expires_at_text is not None and stored_expiry != expires_at_text:
        raise worker.HandlerContractError from None
    domain = _domain_ownership(
        {
            "faultIds": normalized["faultIds"],
            "fixtureIds": normalized["fixtureIds"],
            "dlqCorrelationIds": normalized["dlqCorrelationIds"],
            "snapshots": normalized["snapshots"],
        },
        owner_id,
    )
    return {
        "ownerId": owner_id,
        "expiresAt": stored_expiry,
        **domain,
    }


def _empty_domain_ownership() -> JsonObject:
    return {
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _empty_cleanup() -> JsonObject:
    return {
        "status": "NOT_STARTED",
        "baselineOwnership": None,
        "completedDomains": [],
        "restoredSnapshotRefs": [],
        "clearedFaultIds": [],
        "clearedFixtureIds": [],
        "redrivenDlqCorrelationIds": [],
        "removedDlqCorrelationIds": [],
        "primaryStateSelected": False,
        "productionEndpointStatus": "UNKNOWN",
    }


def _aggregate_ownership(
    *,
    owner_id: str,
    expires_at_text: str,
    domains: Mapping[str, Any],
) -> JsonObject:
    inventories = {
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
    }
    snapshots: dict[str, JsonObject | None] = {
        "model": None,
        "tenantConfig": None,
    }
    for domain_name in DOMAIN_ORDER:
        entry = domains[domain_name]
        ownership = entry["ownership"]
        for name in inventories:
            inventories[name].extend(ownership[name])
        for name in snapshots:
            candidate = ownership["snapshots"][name]
            if candidate is not None:
                if snapshots[name] is not None:
                    raise worker.HandlerContractError from None
                snapshots[name] = candidate
    for values in inventories.values():
        if len(values) != len(set(values)):
            raise worker.HandlerContractError from None
        values.sort()
    return {
        "ownerId": owner_id,
        "expiresAt": expires_at_text,
        **inventories,
        "snapshots": snapshots,
    }


def _new_state(owner_id: str, expires_at_text: str) -> JsonObject:
    domains = {
        domain: {
            "state": {},
            "ownership": _empty_domain_ownership(),
        }
        for domain in DOMAIN_ORDER
    }
    return {
        "schema": OWNER_STATE_SCHEMA,
        "ownership": _aggregate_ownership(
            owner_id=owner_id,
            expires_at_text=expires_at_text,
            domains=domains,
        ),
        "domains": domains,
        "cleanup": _empty_cleanup(),
    }


def _validate_cleanup_progress(
    value: Any,
    *,
    current_ownership: JsonObject,
    owner_id: str,
    expires_at: datetime,
    expires_at_text: str,
) -> JsonObject:
    cleanup = _normalize_object(value, maximum_bytes=64 * 1024)
    _exact(cleanup, _CLEANUP_FIELDS)
    status = cleanup["status"]
    if status not in {"NOT_STARTED", "IN_PROGRESS", "COMPLETE"}:
        raise worker.HandlerContractError from None
    completed = cleanup["completedDomains"]
    if type(completed) is not list or completed != list(CLEANUP_ORDER[: len(completed)]):
        raise worker.HandlerContractError from None
    baseline_raw = cleanup["baselineOwnership"]
    baseline = (
        None
        if baseline_raw is None
        else _owner_ownership(
            baseline_raw,
            owner_id=owner_id,
            expires_at=expires_at,
            expires_at_text=expires_at_text,
        )
    )
    restored = _inventory(cleanup["restoredSnapshotRefs"], owner_id=owner_id)
    cleared_faults = _inventory(cleanup["clearedFaultIds"], owner_id=owner_id)
    cleared_fixtures = _inventory(cleanup["clearedFixtureIds"], owner_id=owner_id)
    redriven = _inventory(cleanup["redrivenDlqCorrelationIds"], owner_id=owner_id)
    removed = _inventory(cleanup["removedDlqCorrelationIds"], owner_id=owner_id)
    if set(redriven).intersection(removed):
        raise worker.HandlerContractError from None
    primary_selected = cleanup["primaryStateSelected"]
    endpoint_status = cleanup["productionEndpointStatus"]
    if (
        type(primary_selected) is not bool
        or not isinstance(endpoint_status, str)
        or _SAFE_STATUS.fullmatch(endpoint_status) is None
    ):
        raise worker.HandlerContractError from None
    normalized = {
        "status": status,
        "baselineOwnership": baseline,
        "completedDomains": list(completed),
        "restoredSnapshotRefs": restored,
        "clearedFaultIds": cleared_faults,
        "clearedFixtureIds": cleared_fixtures,
        "redrivenDlqCorrelationIds": redriven,
        "removedDlqCorrelationIds": removed,
        "primaryStateSelected": primary_selected,
        "productionEndpointStatus": endpoint_status,
    }
    if status == "NOT_STARTED":
        if normalized != _empty_cleanup():
            raise worker.HandlerContractError from None
        return normalized
    if baseline is None:
        raise worker.HandlerContractError from None
    if (status == "IN_PROGRESS" and len(completed) >= len(CLEANUP_ORDER)) or (
        status == "COMPLETE" and len(completed) != len(CLEANUP_ORDER)
    ):
        raise worker.HandlerContractError from None

    current_faults = set(current_ownership["faultIds"])
    current_fixtures = set(current_ownership["fixtureIds"])
    current_dlq = set(current_ownership["dlqCorrelationIds"])
    if (
        current_faults.intersection(cleared_faults)
        or current_faults.union(cleared_faults) != set(baseline["faultIds"])
        or current_fixtures.intersection(cleared_fixtures)
        or current_fixtures.union(cleared_fixtures) != set(baseline["fixtureIds"])
        or current_dlq.intersection(redriven)
        or current_dlq.intersection(removed)
        or current_dlq.union(redriven, removed) != set(baseline["dlqCorrelationIds"])
    ):
        raise worker.HandlerContractError from None
    current_snapshots = current_ownership["snapshots"]
    baseline_snapshots = baseline["snapshots"]
    current_refs: set[str] = set()
    baseline_refs: set[str] = set()
    for name in ("model", "tenantConfig"):
        current = current_snapshots[name]
        original = baseline_snapshots[name]
        if current is not None:
            if current != original:
                raise worker.HandlerContractError from None
            current_refs.add(current["ref"])
        if original is not None:
            baseline_refs.add(original["ref"])
    if current_refs.intersection(restored) or current_refs.union(restored) != baseline_refs:
        raise worker.HandlerContractError from None
    if status == "COMPLETE" and (
        completed != list(CLEANUP_ORDER)
        or any(current_ownership[name] for name in ("faultIds", "fixtureIds", "dlqCorrelationIds"))
        or any(current_snapshots.values())
        or primary_selected is not True
        or endpoint_status != "READY"
    ):
        raise worker.HandlerContractError from None
    return normalized


def validate_owner_state(
    value: Any,
    *,
    owner_id: str,
    expires_at: datetime,
    expires_at_text: str | None = None,
) -> JsonObject:
    """Validate and normalize the complete durable owner-state contract."""

    normalized = _normalize_object(value, maximum_bytes=worker.MAX_OWNER_STATE_BYTES)
    _exact(normalized, {"schema", "ownership", "domains", "cleanup"})
    if normalized["schema"] != OWNER_STATE_SCHEMA:
        raise worker.HandlerContractError from None
    domains_raw = normalized["domains"]
    if type(domains_raw) is not dict or set(domains_raw) != set(DOMAIN_ORDER):
        raise worker.HandlerContractError from None
    ownership = _owner_ownership(
        normalized["ownership"],
        owner_id=owner_id,
        expires_at=expires_at,
        expires_at_text=expires_at_text,
    )
    canonical_expiry = ownership["expiresAt"]
    domains: dict[str, JsonObject] = {}
    for domain_name in DOMAIN_ORDER:
        entry = domains_raw[domain_name]
        if type(entry) is not dict:
            raise worker.HandlerContractError from None
        _exact(entry, {"state", "ownership"})
        domains[domain_name] = {
            "state": _normalize_object(
                entry["state"],
                maximum_bytes=worker.MAX_OWNER_STATE_BYTES,
            ),
            "ownership": _domain_ownership(entry["ownership"], owner_id),
        }
    aggregate = _aggregate_ownership(
        owner_id=owner_id,
        expires_at_text=canonical_expiry,
        domains=domains,
    )
    if aggregate != ownership:
        raise worker.HandlerContractError from None
    cleanup = _validate_cleanup_progress(
        normalized["cleanup"],
        current_ownership=ownership,
        owner_id=owner_id,
        expires_at=expires_at,
        expires_at_text=canonical_expiry,
    )
    result = {
        "schema": OWNER_STATE_SCHEMA,
        "ownership": ownership,
        "domains": domains,
        "cleanup": cleanup,
    }
    return _normalize_object(result, maximum_bytes=worker.MAX_OWNER_STATE_BYTES)


def build_result_envelope(
    task: worker.ActionTask,
    *,
    evidence: Mapping[str, worker.JsonValue],
    ownership: Mapping[str, worker.JsonValue],
) -> JsonObject:
    """Build an exact, bounded, secret-free result from a validated bound task."""

    if not isinstance(task, worker.ActionTask):
        raise worker.HandlerContractError from None
    expected_evidence = (
        worker.CLEANUP_EVIDENCE_FIELDS if task.is_cleanup else worker.ACTION_EVIDENCE_FIELDS[task.operation]
    )
    normalized_evidence = _normalize_object(
        evidence,
        maximum_bytes=worker.MAX_EVIDENCE_BYTES,
    )
    _exact(normalized_evidence, expected_evidence)
    owner_payload = task.payload.get("owner")
    if type(owner_payload) is not dict:
        raise worker.HandlerContractError from None
    expires_at_text = owner_payload.get("expiresAt")
    if not isinstance(expires_at_text, str):
        raise worker.HandlerContractError from None
    normalized_ownership = _owner_ownership(
        ownership,
        owner_id=task.owner_id,
        expires_at=task.expires_at,
        expires_at_text=expires_at_text,
    )
    binding = _normalize_object(
        task.payload.get("binding"),
        maximum_bytes=worker.MAX_HANDLER_OUTPUT_BYTES,
    )
    result = {
        "schema": worker.RESULT_SCHEMA,
        "gate": task.gate,
        "operation": task.operation,
        "ownerId": task.owner_id,
        "correlationId": task.correlation_id,
        "idempotencyKey": task.idempotency_key,
        "status": "SUCCEEDED",
        "binding": binding,
        "evidence": normalized_evidence,
        "ownership": normalized_ownership,
    }
    return _normalize_object(result, maximum_bytes=worker.MAX_HANDLER_OUTPUT_BYTES)


def _owner_binding(
    owner_state: worker.OwnerState,
    state: JsonObject,
) -> OwnerBinding:
    ownership = state["ownership"]
    return OwnerBinding(
        owner_id=owner_state.owner_id,
        expires_at=owner_state.expires_at,
        expires_at_text=ownership["expiresAt"],
    )


def _state_json(value: JsonObject) -> tuple[str, str]:
    normalized = _normalize_object(value, maximum_bytes=worker.MAX_OWNER_STATE_BYTES)
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_owner_record(owner_state: Any) -> worker.OwnerState:
    if (
        not isinstance(owner_state, worker.OwnerState)
        or worker.SHA256.fullmatch(owner_state.owner_id) is None
        or type(owner_state.revision) is not int
        or owner_state.revision < 0
        or owner_state.expires_at.tzinfo is None
        or owner_state.expires_at.utcoffset() is None
        or owner_state.expires_at.utcoffset().total_seconds() != 0
        or worker.SHA256.fullmatch(owner_state.sha256) is None
    ):
        raise worker.HandlerContractError from None
    normalized = _normalize_object(
        owner_state.value,
        maximum_bytes=worker.MAX_OWNER_STATE_BYTES,
    )
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != owner_state.sha256:
        raise worker.HandlerContractError from None
    return owner_state


def _domain_has_effects(ownership: Mapping[str, Any]) -> bool:
    return bool(
        ownership["faultIds"]
        or ownership["fixtureIds"]
        or ownership["dlqCorrelationIds"]
        or any(ownership["snapshots"].values())
    )


def _validate_action_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    operation: str,
) -> None:
    previous_faults = set(previous["faultIds"])
    current_faults = set(current["faultIds"])
    previous_fixtures = set(previous["fixtureIds"])
    current_fixtures = set(current["fixtureIds"])
    previous_dlq = set(previous["dlqCorrelationIds"])
    current_dlq = set(current["dlqCorrelationIds"])
    if (
        (current_faults - previous_faults and operation not in FAULT_ADD_OPERATIONS)
        or (previous_faults - current_faults and operation not in FAULT_REMOVE_OPERATIONS)
        or (current_fixtures - previous_fixtures and operation not in FIXTURE_ADD_OPERATIONS)
        or previous_fixtures - current_fixtures
        or (current_dlq - previous_dlq and operation not in DLQ_ADD_OPERATIONS)
        or (previous_dlq - current_dlq and operation not in DLQ_REMOVE_OPERATIONS)
    ):
        raise worker.HandlerContractError from None
    for name in ("model", "tenantConfig"):
        prior = previous["snapshots"][name]
        if prior is not None and current["snapshots"][name] != prior:
            raise worker.HandlerContractError from None


def _cleanup_partition(
    *,
    owner: OwnerBinding,
    current: JsonObject,
    result: DomainCleanupResult,
) -> tuple[JsonObject, JsonObject]:
    if not isinstance(result, DomainCleanupResult) or type(result.verified_complete) is not bool:
        raise worker.HandlerContractError from None
    next_state = _normalize_object(
        result.state,
        maximum_bytes=worker.MAX_OWNER_STATE_BYTES,
    )
    remaining = _domain_ownership(result.ownership, owner.owner_id)
    restored = _inventory(
        result.restored_snapshot_refs,
        owner_id=owner.owner_id,
    )
    cleared_faults = _inventory(
        result.cleared_fault_ids,
        owner_id=owner.owner_id,
    )
    cleared_fixtures = _inventory(
        result.cleared_fixture_ids,
        owner_id=owner.owner_id,
    )
    redriven = _inventory(
        result.redriven_dlq_correlation_ids,
        owner_id=owner.owner_id,
    )
    removed = _inventory(
        result.removed_dlq_correlation_ids,
        owner_id=owner.owner_id,
    )
    current_faults = set(current["faultIds"])
    remaining_faults = set(remaining["faultIds"])
    current_fixtures = set(current["fixtureIds"])
    remaining_fixtures = set(remaining["fixtureIds"])
    current_dlq = set(current["dlqCorrelationIds"])
    remaining_dlq = set(remaining["dlqCorrelationIds"])
    if (
        remaining_faults.intersection(cleared_faults)
        or remaining_faults.union(cleared_faults) != current_faults
        or remaining_fixtures.intersection(cleared_fixtures)
        or remaining_fixtures.union(cleared_fixtures) != current_fixtures
        or set(redriven).intersection(removed)
        or remaining_dlq.intersection(redriven)
        or remaining_dlq.intersection(removed)
        or remaining_dlq.union(redriven, removed) != current_dlq
    ):
        raise worker.HandlerContractError from None
    current_refs: set[str] = set()
    remaining_refs: set[str] = set()
    for name in ("model", "tenantConfig"):
        before = current["snapshots"][name]
        after = remaining["snapshots"][name]
        if before is not None:
            current_refs.add(before["ref"])
        if after is not None:
            if after != before:
                raise worker.HandlerContractError from None
            remaining_refs.add(after["ref"])
    if (
        remaining_refs.intersection(restored)
        or remaining_refs.union(restored) != current_refs
        or (result.verified_complete and _domain_has_effects(remaining))
    ):
        raise worker.HandlerContractError from None
    if result.primary_state_selected is not None and type(result.primary_state_selected) is not bool:
        raise worker.HandlerContractError from None
    endpoint_status = result.production_endpoint_status
    if endpoint_status is not None and (
        not isinstance(endpoint_status, str) or _SAFE_STATUS.fullmatch(endpoint_status) is None
    ):
        raise worker.HandlerContractError from None
    verification = {
        "restoredSnapshotRefs": restored,
        "clearedFaultIds": cleared_faults,
        "clearedFixtureIds": cleared_fixtures,
        "redrivenDlqCorrelationIds": redriven,
        "removedDlqCorrelationIds": removed,
        "primaryStateSelected": result.primary_state_selected,
        "productionEndpointStatus": endpoint_status,
        "verifiedComplete": result.verified_complete,
    }
    return (
        {"state": next_state, "ownership": remaining},
        verification,
    )


def _merge_cleanup_progress(
    state: JsonObject,
    *,
    domain_name: str,
    domain_entry: JsonObject,
    verification: JsonObject,
) -> JsonObject:
    updated = _normalize_object(state, maximum_bytes=worker.MAX_OWNER_STATE_BYTES)
    updated["domains"][domain_name] = domain_entry
    cleanup = updated["cleanup"]
    for state_name, verification_name in (
        ("restoredSnapshotRefs", "restoredSnapshotRefs"),
        ("clearedFaultIds", "clearedFaultIds"),
        ("clearedFixtureIds", "clearedFixtureIds"),
        ("redrivenDlqCorrelationIds", "redrivenDlqCorrelationIds"),
        ("removedDlqCorrelationIds", "removedDlqCorrelationIds"),
    ):
        combined = [*cleanup[state_name], *verification[verification_name]]
        if len(combined) != len(set(combined)):
            raise worker.HandlerContractError from None
        cleanup[state_name] = sorted(combined)
    primary = verification["primaryStateSelected"]
    if primary is not None:
        if cleanup["primaryStateSelected"] and not primary:
            raise worker.HandlerContractError from None
        cleanup["primaryStateSelected"] = primary
    endpoint = verification["productionEndpointStatus"]
    if endpoint is not None:
        if cleanup["productionEndpointStatus"] == "READY" and endpoint != "READY":
            raise worker.HandlerContractError from None
        cleanup["productionEndpointStatus"] = endpoint
    updated["ownership"] = _aggregate_ownership(
        owner_id=updated["ownership"]["ownerId"],
        expires_at_text=updated["ownership"]["expiresAt"],
        domains=updated["domains"],
    )
    return updated


class LaunchActivityDomains:
    """Exact registry and aggregate implementation of LaunchActivityHandler."""

    def __init__(self, domains: Mapping[str, LaunchDomain]) -> None:
        if not isinstance(domains, Mapping) or set(domains) != set(DOMAIN_ORDER):
            raise worker.ConfigurationError from None
        normalized: dict[str, LaunchDomain] = {}
        for domain_name in DOMAIN_ORDER:
            implementation = domains[domain_name]
            if not callable(getattr(implementation, "handle_action", None)) or not callable(
                getattr(implementation, "cleanup", None)
            ):
                raise worker.ConfigurationError from None
            normalized[domain_name] = implementation
        self.domains: Mapping[str, LaunchDomain] = MappingProxyType(normalized)

    @staticmethod
    def _action_state(
        task: worker.ActionTask,
        context: worker.HandlerContext,
    ) -> JsonObject:
        owner_state = _validate_owner_record(context.owner_state)
        if owner_state.owner_id != task.owner_id or owner_state.expires_at != task.expires_at:
            raise worker.HandlerContractError from None
        owner_payload = task.payload.get("owner")
        if type(owner_payload) is not dict or not isinstance(
            owner_payload.get("expiresAt"),
            str,
        ):
            raise worker.HandlerContractError from None
        expires_at_text = owner_payload["expiresAt"]
        if owner_state.value == {}:
            if owner_state.revision != 0:
                raise worker.HandlerContractError from None
            return _new_state(task.owner_id, expires_at_text)
        return validate_owner_state(
            owner_state.value,
            owner_id=task.owner_id,
            expires_at=task.expires_at,
            expires_at_text=expires_at_text,
        )

    def handle_action(
        self,
        task: worker.ActionTask,
        context: worker.HandlerContext,
    ) -> worker.HandlerOutcome:
        if task.is_cleanup or task.operation not in OPERATION_TO_DOMAIN:
            raise worker.HandlerContractError from None
        context.cancellation.raise_if_cancelled()
        state = self._action_state(task, context)
        if state["cleanup"]["status"] != "NOT_STARTED":
            raise worker.DomainTaskFailure("CleanupAlreadyStarted")
        domain_name = OPERATION_TO_DOMAIN[task.operation]
        entry = state["domains"][domain_name]
        result = self.domains[domain_name].handle_action(
            operation=task.operation,
            task=task,
            context=context,
            state=entry["state"],
            ownership=entry["ownership"],
        )
        context.cancellation.raise_if_cancelled()
        if not isinstance(result, DomainActionResult):
            raise worker.HandlerContractError from None
        evidence = _normalize_object(
            result.evidence,
            maximum_bytes=worker.MAX_EVIDENCE_BYTES,
        )
        _exact(evidence, worker.ACTION_EVIDENCE_FIELDS[task.operation])
        next_entry = {
            "state": _normalize_object(
                result.state,
                maximum_bytes=worker.MAX_OWNER_STATE_BYTES,
            ),
            "ownership": _domain_ownership(result.ownership, task.owner_id),
        }
        next_state = _normalize_object(
            state,
            maximum_bytes=worker.MAX_OWNER_STATE_BYTES,
        )
        next_state["domains"][domain_name] = next_entry
        next_state["ownership"] = _aggregate_ownership(
            owner_id=task.owner_id,
            expires_at_text=state["ownership"]["expiresAt"],
            domains=next_state["domains"],
        )
        _validate_action_transition(
            state["ownership"],
            next_state["ownership"],
            operation=task.operation,
        )
        next_state = validate_owner_state(
            next_state,
            owner_id=task.owner_id,
            expires_at=task.expires_at,
            expires_at_text=state["ownership"]["expiresAt"],
        )
        output = build_result_envelope(
            task,
            evidence=evidence,
            ownership=next_state["ownership"],
        )
        return worker.HandlerOutcome(output=output, state=next_state)

    @staticmethod
    def _begin_cleanup(
        state: JsonObject,
        *,
        expected_ownership: Mapping[str, worker.JsonValue] | None,
        owner: OwnerBinding,
    ) -> tuple[JsonObject, bool]:
        cleanup = state["cleanup"]
        baseline = state["ownership"] if cleanup["status"] == "NOT_STARTED" else cleanup["baselineOwnership"]
        if baseline is None:
            raise worker.HandlerContractError from None
        if expected_ownership is not None:
            expected = _owner_ownership(
                expected_ownership,
                owner_id=owner.owner_id,
                expires_at=owner.expires_at,
                expires_at_text=owner.expires_at_text,
            )
            if expected != baseline:
                raise worker.HandlerContractError from None
        if cleanup["status"] != "NOT_STARTED":
            return state, False
        updated = _normalize_object(
            state,
            maximum_bytes=worker.MAX_OWNER_STATE_BYTES,
        )
        updated["cleanup"]["status"] = "IN_PROGRESS"
        updated["cleanup"]["baselineOwnership"] = updated["ownership"]
        return updated, True

    def _run_cleanup(
        self,
        *,
        owner_state: worker.OwnerState,
        state: JsonObject,
        context: worker.HandlerContext,
        expected_ownership: Mapping[str, worker.JsonValue] | None,
        persist: Callable[[worker.OwnerState, JsonObject], worker.OwnerState] | None,
    ) -> tuple[JsonObject, bool, worker.OwnerState]:
        owner = _owner_binding(owner_state, state)
        state, started = self._begin_cleanup(
            state,
            expected_ownership=expected_ownership,
            owner=owner,
        )
        current_owner = owner_state
        if started and persist is not None:
            current_owner = persist(current_owner, state)
        if state["cleanup"]["status"] == "COMPLETE":
            return state, True, current_owner

        completed = state["cleanup"]["completedDomains"]
        for domain_name in CLEANUP_ORDER[len(completed) :]:
            context.cancellation.raise_if_cancelled()
            entry = state["domains"][domain_name]
            domain_context = worker.HandlerContext(
                aws=context.aws,
                region=context.region,
                state_store=context.state_store,
                owner_state=current_owner,
                cancellation=context.cancellation,
                fence_token=None,
            )
            result = self.domains[domain_name].cleanup(
                owner=owner,
                context=domain_context,
                state=entry["state"],
                ownership=entry["ownership"],
            )
            context.cancellation.raise_if_cancelled()
            next_entry, verification = _cleanup_partition(
                owner=owner,
                current=entry["ownership"],
                result=result,
            )
            state = _merge_cleanup_progress(
                state,
                domain_name=domain_name,
                domain_entry=next_entry,
                verification=verification,
            )
            verified_complete = bool(verification["verifiedComplete"])
            is_last = domain_name == CLEANUP_ORDER[-1]
            terminal_ready = (
                state["cleanup"]["primaryStateSelected"] is True
                and state["cleanup"]["productionEndpointStatus"] == "READY"
                and not _domain_has_effects(state["ownership"])
            )
            if verified_complete and (not is_last or terminal_ready):
                state["cleanup"]["completedDomains"].append(domain_name)
                if is_last:
                    state["cleanup"]["status"] = "COMPLETE"
            state = validate_owner_state(
                state,
                owner_id=owner.owner_id,
                expires_at=owner.expires_at,
                expires_at_text=owner.expires_at_text,
            )
            if persist is not None:
                current_owner = persist(current_owner, state)
            if not verified_complete or (is_last and not terminal_ready):
                return state, False, current_owner
        return state, state["cleanup"]["status"] == "COMPLETE", current_owner

    @staticmethod
    def _cleanup_evidence(state: JsonObject) -> JsonObject:
        cleanup = state["cleanup"]
        if cleanup["status"] != "COMPLETE" or _domain_has_effects(state["ownership"]):
            raise worker.DomainTaskFailure("CleanupIncomplete", retryable=True)
        return {
            "restoredSnapshotRefs": cleanup["restoredSnapshotRefs"],
            "clearedFaultIds": cleanup["clearedFaultIds"],
            "clearedFixtureIds": cleanup["clearedFixtureIds"],
            "redrivenDlqCorrelationIds": cleanup["redrivenDlqCorrelationIds"],
            "removedDlqCorrelationIds": cleanup["removedDlqCorrelationIds"],
            "primaryStateSelected": cleanup["primaryStateSelected"],
            "productionEndpointStatus": cleanup["productionEndpointStatus"],
            "faultsRemaining": len(state["ownership"]["faultIds"]),
            "fixturesRemaining": len(state["ownership"]["fixtureIds"]),
            "correlatedDlqMessagesRemaining": len(state["ownership"]["dlqCorrelationIds"]),
        }

    def handle_cleanup(
        self,
        task: worker.ActionTask,
        context: worker.HandlerContext,
    ) -> worker.HandlerOutcome:
        if not task.is_cleanup:
            raise worker.HandlerContractError from None
        context.cancellation.raise_if_cancelled()
        state = self._action_state(task, context)
        parameters = task.payload.get("parameters")
        if type(parameters) is not dict or type(parameters.get("ownership")) is not dict:
            raise worker.HandlerContractError from None
        owner_state = context.owner_state
        if owner_state is None:
            raise worker.HandlerContractError from None
        state, complete, _ = self._run_cleanup(
            owner_state=owner_state,
            state=state,
            context=context,
            expected_ownership=parameters["ownership"],
            persist=None,
        )
        if not complete:
            raise worker.DomainTaskFailure("CleanupIncomplete", retryable=True)
        evidence = self._cleanup_evidence(state)
        output = build_result_envelope(
            task,
            evidence=evidence,
            ownership=state["ownership"],
        )
        return worker.HandlerOutcome(output=output, state=state)

    @staticmethod
    def _bounded_cursor(value: Any) -> JsonObject | None:
        if value is None:
            return None
        normalized = _normalize_object(value, maximum_bytes=1024)
        if set(normalized) not in (
            {"leaseKey"},
            {"leaseKey", "recordType", "ownerExpiresAtEpoch"},
        ):
            raise worker.HandlerContractError from None
        key = normalized["leaseKey"]
        if type(key) is not dict:
            raise worker.HandlerContractError from None
        _exact(key, {"S"})
        text = key["S"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 512
            or not text.startswith("owner#")
            or any(ord(character) < 32 or ord(character) == 127 for character in text)
        ):
            raise worker.HandlerContractError from None
        result: JsonObject = {"leaseKey": {"S": text}}
        if "recordType" in normalized:
            record_type = normalized["recordType"]
            expiry = normalized["ownerExpiresAtEpoch"]
            if type(record_type) is not dict or type(expiry) is not dict:
                raise worker.HandlerContractError from None
            _exact(record_type, {"S"})
            _exact(expiry, {"N"})
            epoch = expiry["N"]
            if (
                record_type["S"] != "OWNER"
                or not isinstance(epoch, str)
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", epoch)
                is None
            ):
                raise worker.HandlerContractError from None
            result["recordType"] = {"S": "OWNER"}
            result["ownerExpiresAtEpoch"] = {"N": epoch}
        return result

    @staticmethod
    def _commit_maintenance_state(
        state_store: worker.DurableStateStore,
        previous: worker.OwnerState,
        state: JsonObject,
    ) -> worker.OwnerState:
        encoded, digest = _state_json(state)
        committed = state_store.commit_owner(
            previous=previous,
            next_state_json=encoded,
            next_state_sha256=digest,
            next_revision=previous.revision + 1,
        )
        if (
            not isinstance(committed, worker.OwnerState)
            or committed.owner_id != previous.owner_id
            or committed.expires_at != previous.expires_at
            or committed.revision != previous.revision + 1
            or committed.sha256 != digest
        ):
            raise worker.HandlerContractError from None
        return committed

    def cleanup_expired_page(
        self,
        context: worker.HandlerContext,
        *,
        cursor: Mapping[str, Any] | None = None,
        page_number: int = 1,
    ) -> JsonObject:
        """Clean one bounded page and return the cursor required to resume."""

        context.cancellation.raise_if_cancelled()
        if type(page_number) is not int or not 1 <= page_number <= MAX_MAINTENANCE_PAGES:
            raise worker.HandlerContractError from None
        bounded_cursor = self._bounded_cursor(cursor)
        page = context.state_store.list_expired_owners(
            limit=MAINTENANCE_PAGE_LIMIT,
            cursor=bounded_cursor,
        )
        if not isinstance(page, worker.ExpiredOwnerPage) or len(page.owners) > MAINTENANCE_PAGE_LIMIT:
            raise worker.HandlerContractError from None
        completed_count = 0
        incomplete_count = 0
        failed_count = 0
        for owner_state in page.owners:
            context.cancellation.raise_if_cancelled()
            try:
                owner_state = _validate_owner_record(owner_state)
                state = validate_owner_state(
                    owner_state.value,
                    owner_id=owner_state.owner_id,
                    expires_at=owner_state.expires_at,
                )
                state, complete, _ = self._run_cleanup(
                    owner_state=owner_state,
                    state=state,
                    context=context,
                    expected_ownership=None,
                    persist=lambda previous, next_state: self._commit_maintenance_state(
                        context.state_store,
                        previous,
                        next_state,
                    ),
                )
                if complete:
                    self._cleanup_evidence(state)
                    completed_count += 1
                else:
                    incomplete_count += 1
            except worker.ShutdownError:
                raise
            except Exception:
                failed_count += 1
        next_cursor = self._bounded_cursor(page.cursor)
        result = {
            "schema": MAINTENANCE_RESULT_SCHEMA,
            "operation": "cleanup-expired",
            "page": page_number,
            "pageLimit": MAINTENANCE_PAGE_LIMIT,
            "scannedOwners": len(page.owners),
            "completedOwners": completed_count,
            "incompleteOwners": incomplete_count,
            "failedOwners": failed_count,
            "nextCursor": next_cursor,
        }
        return _normalize_object(
            result,
            maximum_bytes=worker.MAX_HANDLER_OUTPUT_BYTES,
        )

    def handle_cleanup_expired(
        self,
        task: worker.MaintenanceTask,
        context: worker.HandlerContext,
    ) -> Mapping[str, worker.JsonValue]:
        if task.operation != "cleanup-expired":
            raise worker.HandlerContractError from None
        previous_page = task.payload.get("page", 0)
        if type(previous_page) is not int or not 0 <= previous_page < MAX_MAINTENANCE_PAGES:
            raise worker.DomainTaskFailure(
                "CleanupPageLimitExceeded",
                retryable=True,
            )
        result = self.cleanup_expired_page(
            context,
            cursor=task.payload.get("cursor"),
            page_number=previous_page + 1,
        )
        if result["failedOwners"]:
            raise worker.DomainTaskFailure("CleanupOwnerFailed")
        if result["incompleteOwners"]:
            raise worker.DomainTaskFailure(
                "CleanupIncomplete",
                retryable=True,
            )
        return result

    def handle_watchdog(
        self,
        task: worker.MaintenanceTask,
        context: worker.HandlerContext,
    ) -> Mapping[str, worker.JsonValue]:
        if task.operation != "watchdog":
            raise worker.HandlerContractError from None
        context.cancellation.raise_if_cancelled()
        response = context.aws.call(
            "cloudwatch",
            "put_metric_data",
            region=context.region,
            parameters={
                "Namespace": WATCHDOG_NAMESPACE,
                "MetricData": [
                    {
                        "MetricName": WATCHDOG_METRIC_NAME,
                        "Dimensions": [
                            {
                                "Name": "Coordinator",
                                "Value": WATCHDOG_COORDINATOR,
                            }
                        ],
                        "Value": 1.0,
                        "Unit": "Count",
                    }
                ],
            },
            timeout_seconds=WATCHDOG_TIMEOUT_SECONDS,
        )
        if not isinstance(response, Mapping):
            raise worker.AwsTransportError(
                "cloudwatch",
                "put_metric_data",
                "InvalidResponse",
            )
        context.cancellation.raise_if_cancelled()
        result = {
            "schema": MAINTENANCE_RESULT_SCHEMA,
            "operation": "watchdog",
            "heartbeatPublished": True,
            "namespace": WATCHDOG_NAMESPACE,
            "metricName": WATCHDOG_METRIC_NAME,
            "coordinator": WATCHDOG_COORDINATOR,
        }
        return _normalize_object(
            result,
            maximum_bytes=worker.MAX_HANDLER_OUTPUT_BYTES,
        )


def _load_default_domains(
    *,
    aws: worker.AwsTransport,
    region: str,
    lease_table_arn: str,
) -> dict[str, LaunchDomain]:
    factories: dict[str, Callable[..., LaunchDomain]] = {}
    implementations: dict[str, LaunchDomain] = {}
    try:
        for domain_name in DOMAIN_ORDER:
            module = importlib.import_module(DEFAULT_DOMAIN_MODULES[domain_name])
            factory = getattr(module, "create_domain")
            if not callable(factory):
                raise TypeError
            factories[domain_name] = factory
        for domain_name in DOMAIN_ORDER:
            factory = factories[domain_name]
            implementations[domain_name] = factory(
                aws=aws,
                region=region,
                lease_table_arn=lease_table_arn,
            )
    except Exception as exc:
        raise worker.ConfigurationError from exc
    return implementations


def create_handler(
    *,
    aws: worker.AwsTransport,
    region: str,
    lease_table_arn: str,
) -> LaunchActivityDomains:
    """Load all concrete domains or fail closed before polling any activity."""

    implementations = _load_default_domains(
        aws=aws,
        region=region,
        lease_table_arn=lease_table_arn,
    )
    return LaunchActivityDomains(implementations)
