"""Adversarial coverage for the fail-closed AgentCore adapter."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from src.gateway.agentcore.adapter import AgentCoreAdapter
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.identity import FACADE_IDENTITY_HEADER
from src.gateway.agentcore.runtime import RuntimeProvider, RuntimeServices
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    RequestContext,
    TenantRole,
)


ISSUER = "https://idp.example.test"
TOKEN = "signed-runtime-token"


class _Verifier:
    def __init__(self, context: RequestContext | None) -> None:
        self.context = context
        self.tokens: list[str] = []

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None:
        self.tokens.append(token)
        return self.context


class _Resolver:
    def __init__(self, principal: Principal | None) -> None:
        self.principal = principal
        self.contexts: list[RequestContext] = []

    async def resolve(self, context: RequestContext) -> Principal | None:
        self.contexts.append(context)
        return self.principal


class _Gateway:
    def __init__(self, chat_result: Any | None = None) -> None:
        self.chat_result = chat_result or {"id": "completion-1"}
        self.chat_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.list_calls: list[tuple[str | None, str | None]] = []

    async def handle_chat_completion(
        self,
        request_data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        self.chat_calls.append((request_data, context))
        return self.chat_result

    async def handle_list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.list_calls.append((project_id, user_id))
        return {"models": [{"name": "claude-sonnet"}]}


class _StaticProvider:
    def __init__(self, runtime: RuntimeServices) -> None:
        self.runtime = runtime
        self.calls = 0

    async def get(self) -> RuntimeServices:
        self.calls += 1
        return self.runtime


def _principal(
    *,
    tenant_id: str = "tenant-a",
    projects: frozenset[str] = frozenset({"project-a"}),
    role: TenantRole = TenantRole.TENANT_MEMBER,
) -> Principal:
    return Principal(
        principal_id="principal-123",
        tenant_id=tenant_id,
        subject="external-subject",
        issuer=ISSUER,
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=projects,
        scopes=frozenset({"inference.invoke", "model.list"}),
        authorization_version=9,
    )


def _verified_context(
    *,
    tenant_id: str = "tenant-a",
    project_id: str = "project-a",
) -> RequestContext:
    return RequestContext(
        user_id="payload-claim-user",
        project_id=project_id,
        roles=["platform_admin"],
        scopes=["*"],
        auth_method=AuthMethod.OIDC_JWT,
        tenant_id=tenant_id,
        issuer=ISSUER,
        subject="external-subject",
    )


def _sdk_context(
    *,
    header_name: str = "Authorization",
) -> SimpleNamespace:
    return SimpleNamespace(
        request_headers={header_name: f"Bearer {TOKEN}"},
        session_id="runtime-session",
    )


def _runtime(
    *,
    principal: Principal | None = None,
    verified_context: RequestContext | None = None,
    gateway: _Gateway | None = None,
) -> tuple[RuntimeServices, _Gateway, _Verifier, _Resolver]:
    resolved_gateway = gateway or _Gateway()
    verifier = _Verifier(verified_context or _verified_context())
    resolver = _Resolver(principal or _principal())
    services = RuntimeServices(
        gateway=resolved_gateway,
        token_verifier=verifier,
        principal_resolver=resolver,
    )
    return services, resolved_gateway, verifier, resolver


def _chat_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "chat",
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_payload_identity_spoofing_is_rejected_before_dispatch() -> None:
    services, gateway, _, _ = _runtime()
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)
    payload = _chat_payload(
        user_id="attacker",
        project_id="other-project",
        tenant_id="other-tenant",
        roles=["platform_admin"],
        scopes=["*"],
    )

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(payload, _sdk_context())

    assert raised.value.status_code == 400
    assert raised.value.code == "untrusted_identity_fields"
    assert provider.calls == 0
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_missing_trusted_runtime_context_fails_closed() -> None:
    services, gateway, _, _ = _runtime()
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), None)

    assert raised.value.status_code == 401
    assert raised.value.code == "runtime_identity_required"
    assert gateway.chat_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "verified"),
    [
        (
            _principal(tenant_id="tenant-a"),
            _verified_context(tenant_id="tenant-b"),
        ),
        (
            _principal(projects=frozenset({"project-a"})),
            _verified_context(project_id="project-b"),
        ),
    ],
)
async def test_wrong_tenant_or_project_grant_is_concealed(
    principal: Principal,
    verified: RequestContext,
) -> None:
    services, gateway, _, _ = _runtime(
        principal=principal,
        verified_context=verified,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 404
    assert raised.value.code == "authorization_denied"
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_tenant_admin_cannot_assume_unverified_project_ownership() -> None:
    services, gateway, _, _ = _runtime(
        principal=_principal(
            projects=frozenset(),
            role=TenantRole.TENANT_ADMIN,
        ),
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 404
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_runtime_initializes_once_off_the_active_event_loop() -> None:
    services, _, _, _ = _runtime()
    calls = 0

    def factory() -> RuntimeServices:
        nonlocal calls
        calls += 1
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        time.sleep(0.03)
        return services

    provider = RuntimeProvider(factory)
    results = await asyncio.gather(*(provider.get() for _ in range(20)))

    assert calls == 1
    assert all(result is services for result in results)


@pytest.mark.asyncio
async def test_failed_runtime_initialization_can_be_retried() -> None:
    services, _, _, _ = _runtime()
    calls = 0

    def factory() -> RuntimeServices:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient bootstrap failure")
        return services

    provider = RuntimeProvider(factory)

    with pytest.raises(RuntimeError, match="transient bootstrap failure"):
        await provider.get()
    assert await provider.get() is services
    assert calls == 2


@pytest.mark.asyncio
async def test_all_supported_chat_fields_and_canonical_context_are_forwarded() -> None:
    services, gateway, verifier, resolver = _runtime()
    adapter = AgentCoreAdapter(_StaticProvider(services))
    payload = _chat_payload(
        system="You are concise.",
        temperature=0.4,
        max_tokens=321,
        top_p=0.9,
        stop=["END"],
        stream=False,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value.",
                    "parameters": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                    },
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "lookup"}},
    )

    result = await adapter.invoke(payload, _sdk_context())

    assert result == {"id": "completion-1"}
    request_data, gateway_context = gateway.chat_calls[0]
    assert request_data == {key: value for key, value in payload.items() if key != "action"}
    assert gateway_context == {
        "user_id": "principal-123",
        "project_id": "project-a",
        "roles": ["tenant_member"],
        "scopes": ["inference.invoke", "model.list"],
        "tenant_id": "tenant-a",
        "auth_method": "oidc_jwt",
        "principal_id": "principal-123",
        "authorization_version": 9,
    }
    assert verifier.tokens == [TOKEN]
    assert resolver.contexts[0].roles == []
    assert resolver.contexts[0].scopes == []


@pytest.mark.asyncio
async def test_first_streamed_chunk_is_not_buffered_or_rewritten() -> None:
    produced: list[str] = []
    first_chunk = {
        "data": {
            "choices": [{"delta": {"content": "first"}}],
        }
    }

    async def chunks():
        produced.append("first")
        yield first_chunk
        produced.append("second")
        yield {"data": {"choices": [{"delta": {"content": "second"}}]}}

    gateway = _Gateway(chat_result=chunks())
    services, _, _, _ = _runtime(gateway=gateway)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        _chat_payload(stream=True),
        _sdk_context(),
    )

    assert produced == []
    assert hasattr(result, "__aiter__")
    assert await anext(result) is first_chunk
    assert produced == ["first"]
    await result.aclose()


@pytest.mark.asyncio
async def test_model_list_uses_canonical_user_and_signed_project() -> None:
    services, gateway, _, _ = _runtime()
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        {"action": "list_models"},
        _sdk_context(),
    )

    assert result == {"models": [{"name": "claude-sonnet"}]}
    assert gateway.list_calls == [("project-a", "principal-123")]


@pytest.mark.asyncio
async def test_health_is_liveness_only_and_skips_identity_and_bootstrap() -> None:
    services, _, _, _ = _runtime()
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)

    result = await adapter.invoke({"action": "health"}, None)

    assert result == {
        "status": "alive",
        "ready": False,
        "dependencies": "not_checked",
    }
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_strict_types_reject_string_stream_flag() -> None:
    services, gateway, _, _ = _runtime()
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            _chat_payload(stream="true"),
            _sdk_context(),
        )

    assert raised.value.status_code == 400
    assert provider.calls == 0
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_allowlisted_facade_identity_header_is_supported() -> None:
    services, gateway, verifier, _ = _runtime()
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        _chat_payload(),
        _sdk_context(header_name=FACADE_IDENTITY_HEADER),
    )

    assert result == {"id": "completion-1"}
    assert verifier.tokens == [TOKEN]
    assert len(gateway.chat_calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_runtime_identity_headers_are_rejected() -> None:
    services, gateway, _, _ = _runtime()
    adapter = AgentCoreAdapter(_StaticProvider(services))
    context = SimpleNamespace(
        request_headers={
            "Authorization": f"Bearer {TOKEN}",
            FACADE_IDENTITY_HEADER: f"Bearer {TOKEN}",
        }
    )

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), context)

    assert raised.value.status_code == 401
    assert raised.value.code == "invalid_runtime_identity"
    assert gateway.chat_calls == []
