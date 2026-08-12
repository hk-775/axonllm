"""Concrete provider-fallback launch rehearsal domain."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker

from launch_domains import common


OPERATIONS = framework.DOMAIN_OPERATIONS["provider_fallback"]
GATE = worker.ACTION_TO_GATE["inject-primary-provider-fault"]
FAULT_NAME = "provider-unavailable"


def _parameters(task: worker.ActionTask) -> tuple[str, str, str, int]:
    value = task.payload.get("parameters")
    expected = {
        "tenantId",
        "projectId",
        "model",
        "primaryProvider",
        "fallbackProvider",
        "failureStatusCode",
        "faultTtlSeconds",
    }
    if type(value) is not dict or set(value) != expected:
        raise worker.HandlerContractError from None
    model = value["model"]
    primary = value["primaryProvider"]
    fallback = value["fallbackProvider"]
    status = value["failureStatusCode"]
    ttl = value["faultTtlSeconds"]
    if (
        not isinstance(model, str)
        or worker.MODEL.fullmatch(model) is None
        or not isinstance(primary, str)
        or worker.PROVIDER.fullmatch(primary) is None
        or not isinstance(fallback, str)
        or worker.PROVIDER.fullmatch(fallback) is None
        or primary == fallback
        or status != 503
        or isinstance(ttl, bool)
        or not isinstance(ttl, int)
        or not 30 <= ttl <= 3600
    ):
        raise worker.HandlerContractError from None
    return model, primary, fallback, status


def _chat_payload(model: str, provider: str) -> dict[str, Any]:
    return {
        "action": "chat",
        "model": model,
        "provider": provider,
        "messages": [
            {
                "role": "user",
                "content": (f"Return the single word ready. Launch provider rehearsal preference: {provider}."),
            }
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 8,
    }


def _attempts(
    observations: Sequence[Any],
) -> dict[str, list[tuple[int, Mapping[str, Any]]]]:
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for observation in observations:
        if getattr(observation, "kind", None) != "provider-attempt":
            continue
        payload = getattr(observation, "payload", None)
        sequence = getattr(observation, "sequence", None)
        request_id = payload.get("request_id") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not isinstance(request_id, str)
        ):
            raise worker.DomainTaskFailure("ProviderEvidenceInvalid")
        grouped.setdefault(request_id, []).append((sequence, payload))
    for values in grouped.values():
        values.sort(key=lambda value: value[0])
    return grouped


def _fallback_chain(
    observations: Sequence[Any],
    *,
    primary: str,
    fallback: str,
    status: int,
    request_id: str | None = None,
) -> tuple[str, int, int, int]:
    matches: list[tuple[str, int, int, int]] = []
    for candidate_id, values in _attempts(observations).items():
        if request_id is not None and candidate_id != request_id:
            continue
        providers = [payload.get("provider") for _, payload in values]
        if any(provider not in {primary, fallback} for provider in providers):
            continue
        primary_failures = [
            (sequence, payload)
            for sequence, payload in values
            if payload.get("provider") == primary
            and payload.get("outcome") == "retryable-failure"
            and payload.get("status_code") == status
        ]
        fallback_successes = [
            (sequence, payload)
            for sequence, payload in values
            if payload.get("provider") == fallback
            and payload.get("outcome") == "success"
            and payload.get("status_code") == 200
        ]
        if (
            primary_failures
            and fallback_successes
            and len(primary_failures) + len(fallback_successes) == len(values)
            and max(sequence for sequence, _ in primary_failures) < min(sequence for sequence, _ in fallback_successes)
        ):
            matches.append(
                (
                    candidate_id,
                    len(primary_failures),
                    len(fallback_successes),
                    max(sequence for sequence, _ in values),
                )
            )
    if len(matches) != 1:
        raise worker.DomainTaskFailure(
            "RehearsalEvidenceUnavailable",
            retryable=True,
        )
    return matches[0]


def _recovery(
    observations: Sequence[Any],
    *,
    primary: str,
    after_sequence: int,
) -> tuple[str, int] | None:
    matches: list[tuple[str, int]] = []
    for request_id, values in _attempts(observations).items():
        values = [value for value in values if value[0] > after_sequence]
        if not values:
            continue
        if all(
            payload.get("provider") == primary
            and payload.get("outcome") == "success"
            and payload.get("status_code") == 200
            for _, payload in values
        ):
            matches.append((request_id, len(values)))
    if len(matches) > 1:
        raise worker.DomainTaskFailure("ProviderEvidenceInvalid")
    return matches[0] if matches else None


def _empty_ownership() -> dict[str, worker.JsonValue]:
    return {
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _expected_ownership(
    fault_id: str | None = None,
) -> dict[str, worker.JsonValue]:
    value = _empty_ownership()
    if fault_id is not None:
        value["faultIds"] = [fault_id]
    return value


def _validate_bound_state(
    state: Mapping[str, worker.JsonValue],
    *,
    model: str,
    primary: str,
    fallback: str,
) -> int:
    fence = state.get("fenceToken")
    if (
        isinstance(fence, bool)
        or not isinstance(fence, int)
        or fence < 1
        or state.get("model") != model
        or state.get("primaryProvider") != primary
        or state.get("fallbackProvider") != fallback
    ):
        raise worker.HandlerContractError from None
    return fence


def _control_is_absent(session: common.LaunchSession) -> None:
    session.observations()
    if (
        session.ledger.read_active_fault(
            session.binding,
            FAULT_NAME,
        )
        is not None
    ):
        raise worker.DomainTaskFailure(
            "ProviderFaultStillActive",
            retryable=True,
        )


def _cleanup_task(
    *,
    owner: framework.OwnerBinding,
    state: Mapping[str, worker.JsonValue],
) -> worker.ActionTask:
    session_binding = state.get("sessionBinding")
    if type(session_binding) is not dict or set(session_binding) != {
        "tenantId",
        "projectId",
        "releaseCommit",
    }:
        raise worker.HandlerContractError from None
    material = f"{owner.owner_id}:{GATE}:cleanup"
    return worker.ActionTask(
        payload={
            "owner": {
                "id": owner.owner_id,
                "expiresAt": owner.expires_at_text,
            },
            "release": {"commit": session_binding["releaseCommit"]},
            "binding": {
                "tenantId": session_binding["tenantId"],
                "projectId": session_binding["projectId"],
            },
            "parameters": {
                "tenantId": session_binding["tenantId"],
                "projectId": session_binding["projectId"],
            },
        },
        gate=GATE,
        operation="clear-primary-provider-fault",
        owner_id=owner.owner_id,
        correlation_id=hashlib.sha256(material.encode("ascii")).hexdigest()[:32],
        idempotency_key=hashlib.sha256(f"{material}:idempotency".encode("ascii")).hexdigest(),
        expires_at=owner.expires_at,
        fence_token=int(state["fenceToken"]),
        request_sha256=hashlib.sha256(f"{material}:request".encode("ascii")).hexdigest(),
    )


class ProviderFallbackDomain:
    """Install, prove, clear, and recover from one fenced provider fault."""

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        model, primary, fallback, status = _parameters(task)
        next_state = common.completed_state(
            state,
            operations=OPERATIONS,
            operation=operation,
        )
        fault_id = common.owned_id(task, FAULT_NAME)
        fence = (
            context.fence_token
            if operation == OPERATIONS[0] and not state
            else _validate_bound_state(
                state,
                model=model,
                primary=primary,
                fallback=fallback,
            )
        )
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise worker.HandlerContractError from None
        session = common.LaunchSession(
            task=task,
            context=context,
            control_gate=GATE,
            fence_token=fence,
        )
        next_ownership = common.copied_ownership(ownership)
        extra: dict[str, worker.JsonValue] = {
            "fenceToken": fence,
            "model": model,
            "primaryProvider": primary,
            "fallbackProvider": fallback,
            "sessionBinding": {
                "tenantId": session.binding.tenant_id,
                "projectId": session.binding.project_id,
                "releaseCommit": session.binding.release_commit,
            },
        }

        if operation == "inject-primary-provider-fault":
            if dict(ownership) != _empty_ownership():
                raise worker.HandlerContractError from None
            session.write_control(
                control_type="fault",
                name=FAULT_NAME,
                parameters={"provider": primary, "status_code": status},
                active=True,
            )
            session.observations()
            active = session.ledger.read_active_fault(
                session.binding,
                FAULT_NAME,
            )
            if (
                active is None
                or active.parameters.get("provider") != primary
                or active.parameters.get("status_code") != status
            ):
                raise worker.DomainTaskFailure(
                    "ProviderFaultUnavailable",
                    retryable=True,
                )
            observations = session.observations()
            try:
                chain = _fallback_chain(
                    observations,
                    primary=primary,
                    fallback=fallback,
                    status=status,
                )
            except worker.DomainTaskFailure:
                runtime = session.invoke(
                    _chat_payload(model, primary),
                    operation=operation,
                )
                if runtime.transport_error or runtime.status_code != 200 or not isinstance(runtime.body, Mapping):
                    raise worker.DomainTaskFailure(
                        "RuntimeInvocationFailed",
                        retryable=True,
                    )
                chain = _fallback_chain(
                    session.observations("provider-attempt"),
                    primary=primary,
                    fallback=fallback,
                    status=status,
                )
            request_id, primary_count, _, last_sequence = chain
            next_ownership["faultIds"] = [fault_id]
            extra.update(
                {
                    "fallbackRequestId": request_id,
                    "fallbackLastSequence": last_sequence,
                }
            )
            evidence: Mapping[str, worker.JsonValue] = {
                "primaryProvider": primary,
                "fallbackProvider": fallback,
                "injectedFailureStatusCode": status,
                "primaryAttemptCount": primary_count,
            }
        elif operation == "verify-provider-fallback":
            if dict(ownership) != _expected_ownership(fault_id):
                raise worker.HandlerContractError from None
            request_id = state.get("fallbackRequestId")
            if not isinstance(request_id, str):
                raise worker.HandlerContractError from None
            _, _, fallback_count, _ = _fallback_chain(
                session.observations("provider-attempt"),
                primary=primary,
                fallback=fallback,
                status=status,
                request_id=request_id,
            )
            evidence = {
                "observedProvider": fallback,
                "fallbackResponseStatusCode": 200,
                "fallbackAttemptCount": fallback_count,
            }
        elif operation == "clear-primary-provider-fault":
            if dict(ownership) != _expected_ownership(fault_id):
                raise worker.HandlerContractError from None
            session.write_control(
                control_type="fault",
                name=FAULT_NAME,
                parameters={"provider": primary, "status_code": status},
                active=False,
            )
            _control_is_absent(session)
            next_ownership["faultIds"] = []
            evidence = {}
        elif operation == "verify-primary-provider-recovery":
            if dict(ownership) != _empty_ownership():
                raise worker.HandlerContractError from None
            after_sequence = state.get("fallbackLastSequence")
            if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 1:
                raise worker.HandlerContractError from None
            observations = session.observations("provider-attempt")
            recovered = _recovery(
                observations,
                primary=primary,
                after_sequence=after_sequence,
            )
            if recovered is None:
                runtime = session.invoke(
                    _chat_payload(model, primary),
                    operation=operation,
                )
                if runtime.transport_error or runtime.status_code != 200 or not isinstance(runtime.body, Mapping):
                    raise worker.DomainTaskFailure(
                        "RuntimeInvocationFailed",
                        retryable=True,
                    )
                recovered = _recovery(
                    session.observations("provider-attempt"),
                    primary=primary,
                    after_sequence=after_sequence,
                )
            if recovered is None:
                raise worker.DomainTaskFailure(
                    "RehearsalEvidenceUnavailable",
                    retryable=True,
                )
            evidence = {"postRecoveryStatusCode": 200}
            extra["recoveryRequestId"] = recovered[0]
        else:
            raise worker.HandlerContractError from None

        next_state.update(extra)
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
        if not state:
            if dict(ownership) != _empty_ownership():
                raise worker.HandlerContractError from None
            return framework.DomainCleanupResult(
                state={},
                ownership=_empty_ownership(),
                verified_complete=True,
            )
        fence = state.get("fenceToken")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise worker.HandlerContractError from None
        task = _cleanup_task(owner=owner, state=state)
        session = common.LaunchSession(
            task=task,
            context=context,
            control_gate=GATE,
            fence_token=fence,
        )
        primary = state.get("primaryProvider")
        if not isinstance(primary, str):
            raise worker.HandlerContractError from None
        fault_ids = list(ownership.get("faultIds", []))
        expected = common.owned_id(task, FAULT_NAME)
        if dict(ownership) != {
            **_empty_ownership(),
            "faultIds": fault_ids,
        }:
            raise worker.HandlerContractError from None
        if fault_ids not in ([], [expected]):
            raise worker.HandlerContractError from None
        session.write_control(
            control_type="fault",
            name=FAULT_NAME,
            parameters={"provider": primary, "status_code": 503},
            active=False,
        )
        _control_is_absent(session)
        cleaned_state = dict(state)
        cleaned_state["faultActive"] = False
        return framework.DomainCleanupResult(
            state=cleaned_state,
            ownership=_empty_ownership(),
            verified_complete=True,
            cleared_fault_ids=fault_ids,
        )


def create_domain(**_kwargs: Any) -> ProviderFallbackDomain:
    return ProviderFallbackDomain()
