"""Network-free routing and provider-fallback launch-domain tests."""

from __future__ import annotations

import hashlib
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "operations"))

import launch_activity_domains as framework
import launch_activity_worker as worker
from launch_domains import provider_fallback, routing


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=1)
OWNER = "a" * 64
COMMIT = "b" * 40
PRIMARY = "anthropic"
FALLBACK = "openai"
MODEL = "reviewed-model"


def _ownership(*, fault: bool = False) -> dict[str, Any]:
    return {
        "faultIds": ([f"{OWNER}:provider-unavailable"] if fault else []),
        "fixtureIds": [],
        "dlqCorrelationIds": [],
        "snapshots": {"model": None, "tenantConfig": None},
    }


def _task(operation: str) -> worker.ActionTask:
    is_routing = operation in routing.OPERATIONS
    parameters = (
        {
            "tenantId": "tenant-1",
            "projectId": "project-1",
            "model": MODEL,
            "strategies": list(worker.ROUTING_STRATEGIES),
            "candidateProviders": [PRIMARY, FALLBACK],
        }
        if is_routing
        else {
            "tenantId": "tenant-1",
            "projectId": "project-1",
            "model": MODEL,
            "primaryProvider": PRIMARY,
            "fallbackProvider": FALLBACK,
            "failureStatusCode": 503,
            "faultTtlSeconds": 300,
        }
    )
    gate = worker.ACTION_TO_GATE[operation]
    return worker.ActionTask(
        payload={
            "owner": {
                "id": OWNER,
                "expiresAt": EXPIRES.isoformat(timespec="seconds"),
            },
            "release": {"commit": COMMIT},
            "binding": {
                "tenantId": "tenant-1",
                "projectId": "project-1",
                "runtimeArn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/AxonLLMRuntime-abcdefghij"),
                "runtimeEndpointArn": (
                    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                    "runtime/AxonLLMRuntime-abcdefghij/"
                    "runtime-endpoint/production"
                ),
            },
            "parameters": parameters,
        },
        gate=gate,
        operation=operation,
        owner_id=OWNER,
        correlation_id=hashlib.sha256(f"{OWNER}:{gate}:{operation}".encode()).hexdigest()[:32],
        idempotency_key=hashlib.sha256(operation.encode()).hexdigest(),
        expires_at=EXPIRES,
        fence_token=17,
        request_sha256="c" * 64,
    )


def _context(*, fence: int | None = 17) -> worker.HandlerContext:
    return worker.HandlerContext(
        aws=SimpleNamespace(),
        region="us-east-1",
        state_store=SimpleNamespace(),
        owner_state=None,
        cancellation=worker.CancellationToken(threading.Event()),
        fence_token=fence,
    )


@dataclass(frozen=True)
class Observation:
    sequence: int
    kind: str
    payload: dict[str, Any]


def _routing_observations(
    *,
    wrong_provider: bool = False,
    omit_strategy: str | None = None,
) -> list[Observation]:
    values: list[Observation] = []
    sequence = 0
    for index, strategy in enumerate(worker.ROUTING_STRATEGIES):
        if strategy == omit_strategy:
            continue
        provider = "groq" if wrong_provider and index == 0 else (PRIMARY if index % 2 == 0 else FALLBACK)
        request_id = f"route-{index}"
        sequence += 1
        values.append(
            Observation(
                sequence,
                "provider-attempt",
                {
                    "attempt": 1,
                    "outcome": "success",
                    "provider": provider,
                    "request_id": request_id,
                    "status_code": 200,
                },
            )
        )
        sequence += 1
        values.append(
            Observation(
                sequence,
                "routing-decision",
                {
                    "candidate_count": 2,
                    "provider": provider,
                    "request_id": request_id,
                    "strategy": strategy,
                },
            )
        )
    return values


def _fallback_observations(*, wrong_fallback: bool = False) -> list[Observation]:
    return [
        Observation(
            1,
            "provider-attempt",
            {
                "attempt": 1,
                "outcome": "retryable-failure",
                "provider": PRIMARY,
                "request_id": "fallback-request",
                "status_code": 503,
            },
        ),
        Observation(
            2,
            "provider-attempt",
            {
                "attempt": 1,
                "outcome": "success",
                "provider": "groq" if wrong_fallback else FALLBACK,
                "request_id": "fallback-request",
                "status_code": 200,
            },
        ),
    ]


class FakeLedger:
    def __init__(self) -> None:
        self.active: dict[str, Any] | None = None

    def read_active_fault(self, _binding, _name):
        if self.active is None:
            return None
        return SimpleNamespace(parameters=self.active)


class FakeSession:
    instances: list["FakeSession"] = []
    observations_value: list[Observation] = []
    invoke_hook: Any = None

    def __init__(
        self,
        *,
        task,
        context,
        control_gate,
        fence_token,
    ) -> None:
        del context
        self.task = task
        self.control_gate = control_gate
        self.fence_token = fence_token
        self.binding = SimpleNamespace(
            tenant_id=task.payload["binding"]["tenantId"],
            project_id=task.payload["binding"]["projectId"],
            release_commit=task.payload["release"]["commit"],
        )
        self.ledger = FakeLedger()
        self.invocations: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.controls: list[dict[str, Any]] = []
        self.__class__.instances.append(self)

    def claim(self) -> int:
        return 1

    def observations(self, *_required):
        return tuple(self.__class__.observations_value)

    def invoke(self, payload, **kwargs):
        self.invocations.append((payload, kwargs))
        hook = self.__class__.invoke_hook
        if hook is not None:
            hook(self, payload, kwargs)
        return SimpleNamespace(
            transport_error=False,
            status_code=200,
            body={"id": "response"},
        )

    def write_control(self, **kwargs):
        self.controls.append(kwargs)
        self.ledger.active = dict(kwargs["parameters"]) if kwargs["active"] else None
        return 2


@pytest.fixture(autouse=True)
def _fake_sessions(monkeypatch):
    FakeSession.instances = []
    FakeSession.observations_value = []
    FakeSession.invoke_hook = None
    monkeypatch.setattr(routing.common, "LaunchSession", FakeSession)
    monkeypatch.setattr(
        provider_fallback.common,
        "LaunchSession",
        FakeSession,
    )


def test_routing_exercises_all_six_strategies_in_exact_order() -> None:
    def append_observations(_session, _payload, kwargs):
        index = len(FakeSession.instances[-1].invocations) - 1
        strategy = kwargs["routing_strategy"]
        provider = PRIMARY if index % 2 == 0 else FALLBACK
        sequence = len(FakeSession.observations_value)
        FakeSession.observations_value.extend(
            [
                Observation(
                    sequence + 1,
                    "provider-attempt",
                    {
                        "attempt": 1,
                        "outcome": "success",
                        "provider": provider,
                        "request_id": f"route-{index}",
                        "status_code": 200,
                    },
                ),
                Observation(
                    sequence + 2,
                    "routing-decision",
                    {
                        "candidate_count": 2,
                        "provider": provider,
                        "request_id": f"route-{index}",
                        "strategy": strategy,
                    },
                ),
            ]
        )

    FakeSession.invoke_hook = append_observations
    result = routing.RoutingDomain().handle_action(
        operation="exercise-routing-strategies",
        task=_task("exercise-routing-strategies"),
        context=_context(),
        state={},
        ownership=_ownership(),
    )
    session = FakeSession.instances[-1]
    assert [kwargs["routing_strategy"] for _, kwargs in session.invocations] == list(worker.ROUTING_STRATEGIES)
    assert all(
        payload["action"] == "chat" and payload["model"] == MODEL and payload["stream"] is False
        for payload, _ in session.invocations
    )
    assert result.evidence == {
        "strategiesExercised": list(worker.ROUTING_STRATEGIES),
        "candidateProviders": [PRIMARY, FALLBACK],
        "requestCount": 6,
    }
    assert result.state["fenceToken"] == 17


def test_routing_verification_derives_providers_and_counts_from_ledger() -> None:
    FakeSession.observations_value = _routing_observations()
    result = routing.RoutingDomain().handle_action(
        operation="verify-routing-decisions",
        task=_task("verify-routing-decisions"),
        context=_context(fence=99),
        state={
            "completed": ["exercise-routing-strategies"],
            "fenceToken": 17,
            "model": MODEL,
            "candidateProviders": [PRIMARY, FALLBACK],
        },
        ownership=_ownership(),
    )
    assert FakeSession.instances[-1].fence_token == 17
    assert result.evidence == {
        "observedProviders": [PRIMARY, FALLBACK],
        "successfulRequestCount": 6,
    }


@pytest.mark.parametrize(
    "observations",
    [
        _routing_observations(omit_strategy="smart"),
        _routing_observations(wrong_provider=True),
    ],
)
def test_routing_rejects_missing_or_wrong_provider_evidence(
    observations,
) -> None:
    FakeSession.observations_value = observations
    with pytest.raises(worker.DomainTaskFailure):
        routing.RoutingDomain().handle_action(
            operation="verify-routing-decisions",
            task=_task("verify-routing-decisions"),
            context=_context(),
            state={
                "completed": ["exercise-routing-strategies"],
                "fenceToken": 17,
                "model": MODEL,
                "candidateProviders": [PRIMARY, FALLBACK],
            },
            ownership=_ownership(),
        )


def test_domains_reject_out_of_order_actions() -> None:
    FakeSession.observations_value = _routing_observations()
    with pytest.raises(
        worker.DomainTaskFailure,
        match="DomainActionOutOfOrder",
    ):
        routing.RoutingDomain().handle_action(
            operation="verify-routing-decisions",
            task=_task("verify-routing-decisions"),
            context=_context(),
            state={},
            ownership=_ownership(),
        )

    with pytest.raises(
        worker.DomainTaskFailure,
        match="DomainActionOutOfOrder",
    ):
        provider_fallback.ProviderFallbackDomain().handle_action(
            operation="verify-provider-fallback",
            task=_task("verify-provider-fallback"),
            context=_context(),
            state={},
            ownership=_ownership(),
        )


def test_fallback_installs_fault_and_proves_primary_then_fallback() -> None:
    def append_fallback(_session, _payload, _kwargs):
        FakeSession.observations_value = _fallback_observations()

    FakeSession.invoke_hook = append_fallback
    result = provider_fallback.ProviderFallbackDomain().handle_action(
        operation="inject-primary-provider-fault",
        task=_task("inject-primary-provider-fault"),
        context=_context(),
        state={},
        ownership=_ownership(),
    )
    session = FakeSession.instances[-1]
    assert session.controls == [
        {
            "control_type": "fault",
            "name": "provider-unavailable",
            "parameters": {
                "provider": PRIMARY,
                "status_code": 503,
            },
            "active": True,
        }
    ]
    assert session.invocations[0][0]["provider"] == PRIMARY
    assert result.evidence["primaryAttemptCount"] == 1
    assert result.state["fallbackRequestId"] == "fallback-request"
    assert result.ownership["faultIds"] == [f"{OWNER}:provider-unavailable"]

    verified = provider_fallback.ProviderFallbackDomain().handle_action(
        operation="verify-provider-fallback",
        task=_task("verify-provider-fallback"),
        context=_context(fence=91),
        state=result.state,
        ownership=result.ownership,
    )
    assert FakeSession.instances[-1].fence_token == 17
    assert verified.evidence == {
        "observedProvider": FALLBACK,
        "fallbackResponseStatusCode": 200,
        "fallbackAttemptCount": 1,
    }


def test_fallback_verification_rejects_wrong_provider() -> None:
    FakeSession.observations_value = _fallback_observations(wrong_fallback=True)
    with pytest.raises(worker.DomainTaskFailure):
        provider_fallback.ProviderFallbackDomain().handle_action(
            operation="verify-provider-fallback",
            task=_task("verify-provider-fallback"),
            context=_context(),
            state={
                "completed": ["inject-primary-provider-fault"],
                "fenceToken": 17,
                "model": MODEL,
                "primaryProvider": PRIMARY,
                "fallbackProvider": FALLBACK,
                "fallbackRequestId": "fallback-request",
                "fallbackLastSequence": 2,
                "sessionBinding": {
                    "tenantId": "tenant-1",
                    "projectId": "project-1",
                    "releaseCommit": COMMIT,
                },
            },
            ownership=_ownership(fault=True),
        )


def test_fallback_verification_rejects_missing_evidence() -> None:
    FakeSession.observations_value = []
    with pytest.raises(
        worker.DomainTaskFailure,
        match="RehearsalEvidenceUnavailable",
    ):
        provider_fallback.ProviderFallbackDomain().handle_action(
            operation="verify-provider-fallback",
            task=_task("verify-provider-fallback"),
            context=_context(),
            state={
                "completed": ["inject-primary-provider-fault"],
                "fenceToken": 17,
                "model": MODEL,
                "primaryProvider": PRIMARY,
                "fallbackProvider": FALLBACK,
                "fallbackRequestId": "fallback-request",
                "fallbackLastSequence": 2,
                "sessionBinding": {
                    "tenantId": "tenant-1",
                    "projectId": "project-1",
                    "releaseCommit": COMMIT,
                },
            },
            ownership=_ownership(fault=True),
        )


def test_fallback_clear_recovery_and_cleanup_are_fenced_and_idempotent() -> None:
    domain = provider_fallback.ProviderFallbackDomain()
    base = {
        "completed": [
            "inject-primary-provider-fault",
            "verify-provider-fallback",
        ],
        "fenceToken": 17,
        "model": MODEL,
        "primaryProvider": PRIMARY,
        "fallbackProvider": FALLBACK,
        "fallbackRequestId": "fallback-request",
        "fallbackLastSequence": 2,
        "sessionBinding": {
            "tenantId": "tenant-1",
            "projectId": "project-1",
            "releaseCommit": COMMIT,
        },
    }
    FakeSession.observations_value = _fallback_observations()
    cleared = domain.handle_action(
        operation="clear-primary-provider-fault",
        task=_task("clear-primary-provider-fault"),
        context=_context(fence=99),
        state=base,
        ownership=_ownership(fault=True),
    )
    assert FakeSession.instances[-1].fence_token == 17
    assert cleared.ownership == _ownership()

    def append_recovery(_session, _payload, _kwargs):
        FakeSession.observations_value.append(
            Observation(
                3,
                "provider-attempt",
                {
                    "attempt": 1,
                    "outcome": "success",
                    "provider": PRIMARY,
                    "request_id": "recovery-request",
                    "status_code": 200,
                },
            )
        )

    FakeSession.invoke_hook = append_recovery
    recovered = domain.handle_action(
        operation="verify-primary-provider-recovery",
        task=_task("verify-primary-provider-recovery"),
        context=_context(fence=101),
        state=cleared.state,
        ownership=cleared.ownership,
    )
    assert recovered.evidence == {"postRecoveryStatusCode": 200}
    assert recovered.state["recoveryRequestId"] == "recovery-request"

    owner = framework.OwnerBinding(
        owner_id=OWNER,
        expires_at=EXPIRES,
        expires_at_text=EXPIRES.isoformat(timespec="seconds"),
    )
    first = domain.cleanup(
        owner=owner,
        context=_context(fence=None),
        state=recovered.state,
        ownership=recovered.ownership,
    )
    second = domain.cleanup(
        owner=owner,
        context=_context(fence=None),
        state=first.state,
        ownership=first.ownership,
    )
    assert first.verified_complete is True
    assert second.verified_complete is True
    assert first.ownership == second.ownership == _ownership()
    assert FakeSession.instances[-1].fence_token == 17

    unexpected = _ownership()
    unexpected["fixtureIds"] = [f"{OWNER}:unexpected"]
    with pytest.raises(worker.HandlerContractError):
        domain.cleanup(
            owner=owner,
            context=_context(fence=None),
            state=recovered.state,
            ownership=unexpected,
        )
