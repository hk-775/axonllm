"""Runtime-side contracts for fenced launch-rehearsal hooks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from src.gateway.agent import GatewayAgent
from src.gateway.agentcore import adapter
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.identity import InvocationIdentity
from src.gateway.agentcore.schemas import (
    REHEARSAL_SCHEMA,
    RehearsalInvocation,
    parse_invocation_payload,
)
from src.gateway.models import (
    AuthMethod,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Principal,
    ProviderModelMapping,
    RequestContext,
    TenantRole,
    TokenUsage,
)
from src.gateway.rehearsal_control import (
    ActiveRehearsalControl,
    RehearsalBinding,
)
from src.gateway.router import ProviderError


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _raw_rehearsal(**overrides: Any) -> dict[str, Any]:
    value = {
        "schema": REHEARSAL_SCHEMA,
        "correlation_id": "a" * 32,
        "owner_id": "b" * 64,
        "release_commit": "c" * 40,
        "fence_token": 7,
        "expires_at_epoch": int((NOW + timedelta(hours=1)).timestamp()),
        "operation": "verify-control-plane-fail-closed",
        "dependency": "dynamodb",
    }
    value.update(overrides)
    return value


def _binding() -> RehearsalBinding:
    return RehearsalBinding(
        tenant_id="tenant-a",
        project_id="project-a",
        correlation_id="a" * 32,
        owner_id="b" * 64,
        release_commit="c" * 40,
        fence_token=7,
        expires_at_epoch=int((NOW + timedelta(hours=1)).timestamp()),
    )


class _Ledger:
    def __init__(self) -> None:
        self.faults: dict[str, ActiveRehearsalControl] = {}
        self.observations: list[tuple[str, dict[str, Any]]] = []
        self.thread_ids: list[int] = []

    def read_active_fault(
        self,
        _binding: RehearsalBinding,
        name: str,
    ) -> ActiveRehearsalControl | None:
        self.thread_ids.append(threading.get_ident())
        return self.faults.get(name)

    def append_observation(
        self,
        _binding: RehearsalBinding,
        kind: str,
        payload: dict[str, Any],
    ) -> bool:
        self.thread_ids.append(threading.get_ident())
        self.observations.append((kind, payload))
        return True


def _control(name: str, parameters: dict[str, Any]) -> ActiveRehearsalControl:
    return ActiveRehearsalControl(
        control_type="fault",
        name=name,
        parameters=parameters,
        revision=2,
        expires_at_epoch=int((NOW + timedelta(hours=1)).timestamp()),
    )


def test_rehearsal_envelope_is_strict_and_not_forwarded_to_chat() -> None:
    parsed = parse_invocation_payload(
        {
            "model": "model-a",
            "messages": [{"role": "user", "content": "test"}],
            "rehearsal": _raw_rehearsal(
                operation="verify-provider-fallback",
                dependency=None,
            ),
        }
    )
    assert parsed.rehearsal is not None
    assert parsed.rehearsal.operation == "verify-provider-fallback"
    assert parsed.request_data is not None
    assert "rehearsal" not in parsed.request_data

    for invalid in (
        _raw_rehearsal(unknown=True),
        _raw_rehearsal(fence_token=True),
        _raw_rehearsal(owner_id="B" * 64),
        _raw_rehearsal(operation="run-arbitrary-code"),
        _raw_rehearsal(dependency="s3"),
    ):
        with pytest.raises(AgentCoreAdapterError) as raised:
            parse_invocation_payload(
                {
                    "action": "readiness",
                    "rehearsal": invalid,
                }
            )
        assert raised.value.status_code == 400
        assert raised.value.code == "invalid_payload"


def test_binding_uses_only_authenticated_tenant_and_project() -> None:
    rehearsal = RehearsalInvocation.from_payload(_raw_rehearsal())
    principal = Principal(
        principal_id="principal-a",
        roles=frozenset({TenantRole.TENANT_AUDITOR}),
        scopes=frozenset({"model.list"}),
        auth_method=AuthMethod.OIDC_JWT,
        issuer="https://issuer.example",
        subject="subject-a",
        tenant_id="tenant-a",
        project_ids=frozenset({"project-a"}),
    )
    identity = InvocationIdentity(
        principal=principal,
        request_context=RequestContext(
            user_id="subject-a",
            project_id="project-a",
            roles=["tenant_auditor"],
            scopes=["model:list"],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id="tenant-a",
            issuer="https://issuer.example",
            subject="subject-a",
        ),
        tenant_id="tenant-a",
        project_id="project-a",
    )
    binding = adapter._rehearsal_binding(rehearsal, identity)
    assert binding is not None
    assert binding.tenant_id == "tenant-a"
    assert binding.project_id == "project-a"


@pytest.mark.asyncio
async def test_exact_dependency_fault_fails_closed_and_records_observation() -> None:
    ledger = _Ledger()
    request_thread = threading.get_ident()
    ledger.faults["dependency-unavailable"] = _control(
        "dependency-unavailable",
        {"dependency": "dynamodb"},
    )
    rehearsal = RehearsalInvocation.from_payload(_raw_rehearsal())
    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter._apply_rehearsal_control(
            ledger=ledger,
            binding=_binding(),
            rehearsal=rehearsal,
        )
    assert raised.value.status_code == 503
    assert raised.value.code == "rehearsal_dependency_unavailable"
    assert ledger.observations == [
        (
            "dependency-call",
            {
                "dependency": "dynamodb",
                "outcome": "unavailable",
                "request_id": "a" * 32,
                "status_code": 503,
            },
        )
    ]
    assert ledger.thread_ids
    assert all(thread_id != request_thread for thread_id in ledger.thread_ids)


@pytest.mark.asyncio
async def test_startup_exit_requires_switch_and_durable_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _Ledger()
    ledger.faults["startup-delay"] = _control(
        "startup-delay",
        {"delay_seconds": 5},
    )
    rehearsal = RehearsalInvocation.from_payload(
        _raw_rehearsal(
            operation="induce-initialization-timeout",
            dependency=None,
        )
    )
    slept: list[int] = []

    async def sleep(seconds: int) -> None:
        slept.append(seconds)

    class ProcessExit(Exception):
        pass

    def exit_process(code: int) -> None:
        raise ProcessExit(code)

    monkeypatch.setattr(adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(adapter.socket, "gethostname", lambda: "runtime-a")
    monkeypatch.setattr(adapter.os, "_exit", exit_process)

    await adapter._apply_rehearsal_control(
        ledger=ledger,
        binding=_binding(),
        rehearsal=rehearsal,
    )
    assert slept == []
    assert ledger.observations == []

    monkeypatch.setenv(
        "AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT",
        "true",
    )
    with pytest.raises(ProcessExit, match="124"):
        await adapter._apply_rehearsal_control(
            ledger=ledger,
            binding=_binding(),
            rehearsal=rehearsal,
        )
    assert slept == [5]
    assert [payload["phase"] for _, payload in ledger.observations] == [
        "started",
        "timed-out",
    ]
    assert ledger.observations[-1][1]["exit_code"] == 124


@pytest.mark.asyncio
async def test_provider_fault_forces_request_local_fallback_observations() -> None:
    ledger = _Ledger()
    request_thread = threading.get_ident()
    ledger.faults["provider-unavailable"] = _control(
        "provider-unavailable",
        {"provider": "openai", "status_code": 503},
    )
    mappings = [
        ProviderModelMapping("openai", "model-a", fallback_order=0),
        ProviderModelMapping("anthropic", "model-b", fallback_order=1),
    ]
    gateway = object.__new__(GatewayAgent)
    gateway.router = SimpleNamespace(
        available_mappings=lambda _model: mappings,
    )
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "test"}],
        model="logical-model",
    )

    async def provider(mapping: ProviderModelMapping) -> ChatCompletionResponse:
        return ChatCompletionResponse(
            id="response-a",
            choices=[],
            usage=TokenUsage(1, 1, 2),
            model=request.model,
            provider=mapping.provider,
        )

    observed = gateway._rehearsal_provider_fn(
        provider,
        context={
            "rehearsal": SimpleNamespace(
                operation="verify-provider-fallback",
                routing_strategy="weighted",
            ),
            "rehearsal_binding": _binding(),
            "rehearsal_ledger": ledger,
        },
        request=request,
        request_id="request-a",
    )

    with pytest.raises(ProviderError) as raised:
        await observed(mappings[0])
    assert raised.value.status_code == 503
    assert raised.value.provider_unavailable is False
    response = await observed(mappings[1])
    assert response.provider == "anthropic"
    assert [kind for kind, _ in ledger.observations] == [
        "provider-attempt",
        "provider-attempt",
        "routing-decision",
    ]
    assert ledger.observations[0][1]["outcome"] == "retryable-failure"
    assert ledger.observations[1][1]["outcome"] == "success"
    assert ledger.observations[2][1]["candidate_count"] == 2
    assert ledger.thread_ids
    assert all(thread_id != request_thread for thread_id in ledger.thread_ids)
