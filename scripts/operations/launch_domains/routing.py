"""Concrete provider-routing launch rehearsal domain."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import launch_activity_domains as framework
import launch_activity_worker as worker

from launch_domains import common


OPERATIONS = framework.DOMAIN_OPERATIONS["routing"]
STRATEGIES = worker.ROUTING_STRATEGIES
GATE = worker.ACTION_TO_GATE["exercise-routing-strategies"]


def _parameters(task: worker.ActionTask) -> tuple[str, list[str]]:
    value = task.payload.get("parameters")
    expected = {
        "tenantId",
        "projectId",
        "model",
        "strategies",
        "candidateProviders",
    }
    if type(value) is not dict or set(value) != expected:
        raise worker.HandlerContractError from None
    model = value["model"]
    strategies = value["strategies"]
    providers = value["candidateProviders"]
    if (
        not isinstance(model, str)
        or worker.MODEL.fullmatch(model) is None
        or strategies != list(STRATEGIES)
        or type(providers) is not list
        or len(providers) < 2
        or providers != sorted(providers)
        or len(providers) != len(set(providers))
        or any(not isinstance(provider, str) or worker.PROVIDER.fullmatch(provider) is None for provider in providers)
    ):
        raise worker.HandlerContractError from None
    return model, list(providers)


def _chat_payload(model: str, strategy: str) -> dict[str, Any]:
    return {
        "action": "chat",
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (f"Return the single word ready. Launch routing rehearsal strategy: {strategy}."),
            }
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 8,
    }


def _observed_evidence(
    observations: Sequence[Any],
    *,
    candidate_providers: Sequence[str],
    require_complete: bool,
) -> tuple[list[str], list[str], int]:
    decisions = [
        observation for observation in observations if getattr(observation, "kind", None) == "routing-decision"
    ]
    attempts = [observation for observation in observations if getattr(observation, "kind", None) == "provider-attempt"]
    strategy_requests: dict[str, set[str]] = {strategy: set() for strategy in STRATEGIES}
    providers_by_request: dict[str, set[str]] = {}
    for decision in decisions:
        payload = getattr(decision, "payload", None)
        if not isinstance(payload, Mapping):
            raise worker.DomainTaskFailure("RoutingEvidenceInvalid")
        strategy = payload.get("strategy")
        provider = payload.get("provider")
        request_id = payload.get("request_id")
        if strategy not in strategy_requests or provider not in candidate_providers or not isinstance(request_id, str):
            raise worker.DomainTaskFailure("RoutingEvidenceInvalid")
        strategy_requests[strategy].add(request_id)
        providers_by_request.setdefault(request_id, set()).add(provider)

    successful: dict[str, set[str]] = {}
    for attempt in attempts:
        payload = getattr(attempt, "payload", None)
        if not isinstance(payload, Mapping):
            raise worker.DomainTaskFailure("RoutingEvidenceInvalid")
        if payload.get("outcome") != "success":
            continue
        request_id = payload.get("request_id")
        provider = payload.get("provider")
        if not isinstance(request_id, str) or provider not in candidate_providers:
            raise worker.DomainTaskFailure("RoutingEvidenceInvalid")
        successful.setdefault(request_id, set()).add(provider)

    covered: list[str] = []
    request_ids: set[str] = set()
    for strategy in STRATEGIES:
        ids = strategy_requests[strategy]
        if len(ids) > 1:
            raise worker.DomainTaskFailure("RoutingEvidenceInvalid")
        if not ids:
            continue
        request_id = next(iter(ids))
        if not providers_by_request[request_id].intersection(successful.get(request_id, set())):
            raise worker.DomainTaskFailure(
                "RehearsalEvidenceUnavailable",
                retryable=True,
            )
        covered.append(strategy)
        request_ids.add(request_id)

    if len(request_ids) != len(covered):
        raise worker.DomainTaskFailure("RoutingEvidenceInvalid")
    observed_providers = sorted(
        {provider for request_id in request_ids for provider in providers_by_request[request_id]}
    )
    if require_complete and (
        covered != list(STRATEGIES) or len(request_ids) != len(STRATEGIES) or len(observed_providers) < 2
    ):
        raise worker.DomainTaskFailure(
            "RehearsalEvidenceUnavailable",
            retryable=True,
        )
    return covered, observed_providers, len(request_ids)


def _empty_ownership() -> dict[str, worker.JsonValue]:
    return {
        "faultIds": [],
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


class RoutingDomain:
    """Exercise and prove every reviewed routing strategy."""

    def handle_action(
        self,
        *,
        operation: str,
        task: worker.ActionTask,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainActionResult:
        model, candidate_providers = _parameters(task)
        next_state = common.completed_state(
            state,
            operations=OPERATIONS,
            operation=operation,
        )
        if dict(ownership) != _empty_ownership():
            raise worker.HandlerContractError from None

        fence = state.get("fenceToken", context.fence_token)
        if (
            isinstance(fence, bool)
            or not isinstance(fence, int)
            or fence < 1
            or (state and (state.get("model") != model or state.get("candidateProviders") != candidate_providers))
        ):
            raise worker.HandlerContractError from None
        session = common.LaunchSession(
            task=task,
            context=context,
            control_gate=GATE,
            fence_token=fence,
        )

        if operation == "exercise-routing-strategies":
            session.claim()
            existing = session.observations()
            covered, _, _ = _observed_evidence(
                existing,
                candidate_providers=candidate_providers,
                require_complete=False,
            )
            for strategy in STRATEGIES:
                if strategy in covered:
                    continue
                observation = session.invoke(
                    _chat_payload(model, strategy),
                    operation=operation,
                    routing_strategy=strategy,
                )
                if (
                    observation.transport_error
                    or observation.status_code != 200
                    or not isinstance(observation.body, Mapping)
                ):
                    raise worker.DomainTaskFailure(
                        "RuntimeInvocationFailed",
                        retryable=True,
                    )
            observations = session.observations(
                "routing-decision",
                "provider-attempt",
            )
            exercised, _, request_count = _observed_evidence(
                observations,
                candidate_providers=candidate_providers,
                require_complete=True,
            )
            evidence: Mapping[str, worker.JsonValue] = {
                "strategiesExercised": exercised,
                "candidateProviders": candidate_providers,
                "requestCount": request_count,
            }
        elif operation == "verify-routing-decisions":
            observations = session.observations(
                "routing-decision",
                "provider-attempt",
            )
            _, observed_providers, successful_count = _observed_evidence(
                observations,
                candidate_providers=candidate_providers,
                require_complete=True,
            )
            evidence = {
                "observedProviders": observed_providers,
                "successfulRequestCount": successful_count,
            }
        else:
            raise worker.HandlerContractError from None

        next_state.update(
            {
                "fenceToken": fence,
                "model": model,
                "candidateProviders": candidate_providers,
            }
        )
        return framework.DomainActionResult(
            evidence=evidence,
            state=next_state,
            ownership=common.copied_ownership(ownership),
        )

    def cleanup(
        self,
        *,
        owner: framework.OwnerBinding,
        context: worker.HandlerContext,
        state: Mapping[str, worker.JsonValue],
        ownership: Mapping[str, worker.JsonValue],
    ) -> framework.DomainCleanupResult:
        del owner, context
        if dict(ownership) != _empty_ownership():
            raise worker.HandlerContractError from None
        return framework.DomainCleanupResult(
            state=dict(state),
            ownership=_empty_ownership(),
            verified_complete=True,
        )


def create_domain(**_kwargs: Any) -> RoutingDomain:
    return RoutingDomain()
