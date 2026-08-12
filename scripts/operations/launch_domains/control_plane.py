"""Fenced control-plane dependency-fault launch domain."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker
from launch_domains import common


OPERATIONS = framework.DOMAIN_OPERATIONS["control_plane"]
FAULT_SUFFIX = "control-plane-dependency-unavailable"
_MUTATION_REVISION_SENTINEL = (1 << 63) - 1


def _ownership(
    value: Mapping[str, worker.JsonValue],
    *,
    fault_id: str | None,
) -> dict[str, worker.JsonValue]:
    result = common.copied_ownership(value)
    result["faultIds"] = [] if fault_id is None else [fault_id]
    return result


def _status(observation: common.RuntimeObservation, expected: int) -> int:
    if observation.transport_error or observation.status_code != expected:
        raise worker.DomainTaskFailure(
            "ControlPlaneProbeUnavailable",
            retryable=True,
        )
    return expected


def _dependency_observations(
    session: common.LaunchSession,
    *,
    dependency: str,
    outcome: str,
    minimum: int,
) -> None:
    observations = session.observations("dependency-call")
    matching = [
        item
        for item in observations
        if item.kind == "dependency-call"
        and item.payload.get("dependency") == dependency
        and item.payload.get("outcome") == outcome
        and item.payload.get("request_id") == session.binding.correlation_id
        and item.payload.get("status_code") == (503 if outcome == "unavailable" else 200)
    ]
    if len(matching) < minimum:
        raise worker.DomainTaskFailure(
            "ControlPlaneEvidenceUnavailable",
            retryable=True,
        )


class ControlPlaneDomain:
    """Exercise one correlation-scoped dependency fault."""

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        parameters = task.payload["parameters"]
        if not isinstance(parameters, Mapping):
            raise worker.HandlerContractError from None
        dependency = parameters.get("dependency")
        if not isinstance(dependency, str):
            raise worker.HandlerContractError from None
        session = common.LaunchSession(task=task, context=context)
        fault_id = common.owned_id(task, FAULT_SUFFIX)

        if operation == "inject-control-plane-fault":
            revision = session.write_control(
                control_type="fault",
                name="dependency-unavailable",
                parameters={"dependency": dependency},
                active=True,
            )
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
                extra={
                    "dependency": dependency,
                    "faultActive": True,
                    "fenceToken": session.binding.fence_token,
                    "controlRevision": revision,
                    "controlTask": {
                        "binding": dict(task.payload["binding"]),
                        "parameters": dict(parameters),
                        "release": dict(task.payload["release"]),
                    },
                },
            )
            return framework.DomainActionResult(
                evidence={"faultedDependency": dependency},
                state=next_state,
                ownership=_ownership(ownership, fault_id=fault_id),
            )

        if state.get("dependency") != dependency or state.get("fenceToken") != session.binding.fence_token:
            raise worker.DomainTaskFailure("ControlPlaneFaultBindingMismatch")

        if operation == "verify-control-plane-fail-closed":
            ready = _status(
                session.invoke(
                    {"action": "readiness"},
                    operation=operation,
                    dependency=dependency,
                ),
                503,
            )
            read = _status(
                session.invoke(
                    {"action": "get_tenant_config"},
                    operation=operation,
                    dependency=dependency,
                ),
                503,
            )
            mutation = _status(
                session.invoke(
                    {
                        "action": "update_tenant_config",
                        "expected_revision": _MUTATION_REVISION_SENTINEL,
                        "config": {"rate_limit_rpm": 1},
                    },
                    operation=operation,
                    dependency=dependency,
                ),
                503,
            )
            _dependency_observations(
                session,
                dependency=dependency,
                outcome="unavailable",
                minimum=3,
            )
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
            )
            return framework.DomainActionResult(
                evidence={
                    "readyDuringFaultStatusCode": ready,
                    "readDuringFaultStatusCode": read,
                    "mutationDuringFaultStatusCode": mutation,
                },
                state=next_state,
                ownership=dict(ownership),
            )

        if operation == "clear-control-plane-fault":
            revision = session.write_control(
                control_type="fault",
                name="dependency-unavailable",
                parameters={"dependency": dependency},
                active=False,
            )
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
                extra={"faultActive": False, "controlRevision": revision},
            )
            return framework.DomainActionResult(
                evidence={},
                state=next_state,
                ownership=_ownership(ownership, fault_id=None),
            )

        if operation == "verify-control-plane-recovery":
            ready = _status(
                session.invoke(
                    {"action": "readiness"},
                    operation=operation,
                    dependency=dependency,
                ),
                200,
            )
            read = _status(
                session.invoke(
                    {"action": "get_tenant_config"},
                    operation=operation,
                    dependency=dependency,
                ),
                200,
            )
            _dependency_observations(
                session,
                dependency=dependency,
                outcome="available",
                minimum=2,
            )
            next_state = common.completed_state(
                state,
                operations=OPERATIONS,
                operation=operation,
            )
            return framework.DomainActionResult(
                evidence={
                    "readyAfterRecoveryStatusCode": ready,
                    "readAfterRecoveryStatusCode": read,
                },
                state=next_state,
                ownership=dict(ownership),
            )
        raise worker.HandlerContractError from None

    def cleanup(
        self,
        *,
        owner: framework.OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainCleanupResult:
        fault_id = f"{owner.owner_id}:{FAULT_SUFFIX}"
        faults = ownership.get("faultIds")
        if type(faults) is not list or any(item != fault_id for item in faults):
            raise worker.HandlerContractError from None
        if faults or state.get("faultActive") is True:
            fence_token = state.get("fenceToken")
            dependency = state.get("dependency")
            if isinstance(fence_token, bool) or not isinstance(fence_token, int) or not isinstance(dependency, str):
                raise worker.DomainTaskFailure(
                    "ControlPlaneCleanupBindingUnavailable",
                    retryable=True,
                )
            task = _cleanup_task(
                owner=owner,
                state=state,
                fence_token=fence_token,
            )
            session = common.LaunchSession(
                task=task,
                context=context,
                control_gate=worker.ACTION_TO_GATE["inject-control-plane-fault"],
                fence_token=fence_token,
            )
            session.write_control(
                control_type="fault",
                name="dependency-unavailable",
                parameters={"dependency": dependency},
                active=False,
            )
        next_state = dict(state)
        next_state["faultActive"] = False
        return framework.DomainCleanupResult(
            state=next_state,
            ownership={
                **common.copied_ownership(ownership),
                "faultIds": [],
            },
            verified_complete=True,
            cleared_fault_ids=list(faults),
        )


def _cleanup_task(
    *,
    owner: framework.OwnerBinding,
    state: Mapping[str, worker.JsonValue],
    fence_token: int,
) -> worker.ActionTask:
    payload = state.get("controlTask")
    if not isinstance(payload, Mapping) or set(payload) != {
        "binding",
        "parameters",
        "release",
    }:
        raise worker.HandlerContractError from None
    gate = worker.ACTION_TO_GATE["inject-control-plane-fault"]
    correlation = hashlib.sha256(f"{owner.owner_id}:{gate}:cleanup".encode("ascii")).hexdigest()[:32]
    idempotency = hashlib.sha256(f"{owner.owner_id}:{gate}:cleanup-control".encode("ascii")).hexdigest()
    task_payload = {
        "binding": dict(payload["binding"]),
        "owner": {"expiresAt": owner.expires_at_text},
        "parameters": dict(payload["parameters"]),
        "release": dict(payload["release"]),
    }
    if not all(isinstance(task_payload[name], dict) for name in ("binding", "parameters", "release")):
        raise worker.HandlerContractError from None
    return worker.ActionTask(
        payload=task_payload,
        gate=gate,
        operation="clear-control-plane-fault",
        owner_id=owner.owner_id,
        correlation_id=correlation,
        idempotency_key=idempotency,
        expires_at=owner.expires_at,
        fence_token=fence_token,
        request_sha256=hashlib.sha256(f"{owner.owner_id}:{fence_token}:cleanup".encode("ascii")).hexdigest(),
    )


def create_domain(**_kwargs: Any) -> ControlPlaneDomain:
    return ControlPlaneDomain()
