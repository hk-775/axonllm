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
from src.gateway.agentcore.runtime import (
    RuntimeInitializationError,
    RuntimeProvider,
    RuntimeServices,
)
from src.gateway.auth.project_repository import ProjectStoreUnavailable
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    Project,
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


class _ProjectResolver:
    def __init__(
        self,
        project: Project | None = None,
        *,
        missing: bool = False,
        unavailable: bool = False,
    ) -> None:
        self.project = project or Project(
            project_id="project-a",
            tenant_id="tenant-a",
            name="Project A",
        )
        self.unavailable = unavailable
        self.missing = missing
        self.calls: list[tuple[str, str]] = []

    async def resolve(
        self,
        tenant_id: str,
        project_id: str,
    ) -> Project | None:
        self.calls.append((tenant_id, project_id))
        if self.unavailable:
            raise ProjectStoreUnavailable("unavailable")
        if self.missing:
            return None
        if (
            self.project is None
            or self.project.tenant_id != tenant_id
            or self.project.project_id != project_id
        ):
            return None
        return self.project


class _PolicyService:
    def __init__(
        self,
        decision: str = "ALLOW",
        *,
        refresh_error: Exception | None = None,
        evaluation_error: Exception | None = None,
    ) -> None:
        self.decision = decision
        self.refresh_error = refresh_error
        self.evaluation_error = evaluation_error
        self.events: list[str] = []
        self.evaluations: list[tuple[RequestContext, str, str]] = []

    async def refresh_if_stale(self) -> bool:
        self.events.append("refresh")
        if self.refresh_error is not None:
            raise self.refresh_error
        return False

    async def evaluate(
        self,
        context: RequestContext,
        action: str,
        resource: str,
    ) -> str:
        self.events.append("evaluate")
        self.evaluations.append((context, action, resource))
        if self.evaluation_error is not None:
            raise self.evaluation_error
        return self.decision


class _Gateway:
    def __init__(self, chat_result: Any | None = None) -> None:
        self.chat_result = chat_result or {"id": "completion-1"}
        self.chat_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.list_calls: list[
            tuple[str | None, str | None, str | None, Project | None]
        ] = []

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
        tenant_id: str | None = None,
        authorized_project: Project | None = None,
    ) -> dict[str, Any]:
        self.list_calls.append(
            (project_id, user_id, tenant_id, authorized_project)
        )
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
    project_resolver: _ProjectResolver | None = None,
    policy_service: _PolicyService | None = None,
) -> tuple[RuntimeServices, _Gateway, _Verifier, _Resolver]:
    resolved_gateway = gateway or _Gateway()
    verifier = _Verifier(verified_context or _verified_context())
    resolver = _Resolver(principal or _principal())
    projects = project_resolver or _ProjectResolver()
    services = RuntimeServices(
        gateway=resolved_gateway,
        token_verifier=verifier,
        principal_resolver=resolver,
        project_resolver=projects,
        policy_service=policy_service,
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
    assert raised.value.code == "resource_not_found"
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
async def test_missing_authoritative_project_is_concealed() -> None:
    services, gateway, _, _ = _runtime(
        project_resolver=_ProjectResolver(missing=True),
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 404
    assert raised.value.code == "resource_not_found"
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_project_store_outage_fails_closed() -> None:
    services, gateway, _, _ = _runtime(
        project_resolver=_ProjectResolver(unavailable=True),
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 503
    assert raised.value.code == "project_resolver_unavailable"
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_cedar_allow_uses_canonical_context_and_chat_target() -> None:
    policy = _PolicyService()
    services, gateway, _, _ = _runtime(policy_service=policy)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(_chat_payload(), _sdk_context())

    assert result == {"id": "completion-1"}
    assert policy.events == ["refresh", "evaluate"]
    assert len(policy.evaluations) == 1
    policy_context, action, resource = policy.evaluations[0]
    assert (action, resource) == ("post", "/v1/chat/completions")
    assert policy_context.user_id == "principal-123"
    assert policy_context.principal_id == "principal-123"
    assert policy_context.tenant_id == "tenant-a"
    assert policy_context.project_id == "project-a"
    assert policy_context.roles == ["tenant_member"]
    assert policy_context.scopes == ["inference.invoke", "model.list"]
    assert len(gateway.chat_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invocation_action", ["chat", "list_models"])
async def test_cedar_deny_skips_gateway_dispatch(
    invocation_action: str,
) -> None:
    policy = _PolicyService("DENY")
    services, gateway, _, _ = _runtime(policy_service=policy)
    adapter = AgentCoreAdapter(_StaticProvider(services))
    payload = (
        _chat_payload()
        if invocation_action == "chat"
        else {"action": "list_models"}
    )

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(payload, _sdk_context())

    expected_target = (
        ("post", "/v1/chat/completions")
        if invocation_action == "chat"
        else ("get", "/v1/models")
    )
    assert raised.value.status_code == 403
    assert raised.value.code == "authorization_denied"
    assert raised.value.message == "Access denied by policy."
    assert policy.events == ["refresh", "evaluate"]
    assert policy.evaluations[0][1:] == expected_target
    assert gateway.chat_calls == []
    assert gateway.list_calls == []


@pytest.mark.asyncio
async def test_cedar_evaluation_failure_is_sanitized_and_fails_closed() -> None:
    detail = "backend policy table axon-secret failed"
    policy = _PolicyService(evaluation_error=RuntimeError(detail))
    services, gateway, _, _ = _runtime(policy_service=policy)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 503
    assert raised.value.code == "policy_evaluation_failed"
    assert raised.value.message == "Authorization is temporarily unavailable."
    assert detail not in raised.value.message
    assert policy.events == ["refresh", "evaluate"]
    assert gateway.chat_calls == []
    assert gateway.list_calls == []


@pytest.mark.asyncio
async def test_cedar_refresh_failure_uses_compiled_policy() -> None:
    policy = _PolicyService(
        refresh_error=RuntimeError("policy refresh unavailable"),
    )
    services, gateway, _, _ = _runtime(policy_service=policy)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(_chat_payload(), _sdk_context())

    assert result == {"id": "completion-1"}
    assert policy.events == ["refresh", "evaluate"]
    assert len(gateway.chat_calls) == 1


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
    results = await asyncio.gather(
        *(provider.initialize() for _ in range(20))
    )

    assert calls == 1
    assert all(result is services for result in results)
    assert await provider.get() is services
    await provider.close()


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

    with pytest.raises(RuntimeInitializationError) as raised:
        await provider.initialize()
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "transient bootstrap failure"
    assert await provider.initialize() is services
    assert await provider.get() is services
    assert calls == 2
    await provider.close()


@pytest.mark.asyncio
async def test_request_never_triggers_uninitialized_runtime_factory() -> None:
    services, gateway, _, _ = _runtime()
    calls = 0

    def factory() -> RuntimeServices:
        nonlocal calls
        calls += 1
        return services

    adapter = AgentCoreAdapter(RuntimeProvider(factory))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 503
    assert raised.value.code == "gateway_initialization_failed"
    assert calls == 0
    assert gateway.chat_calls == []


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
    authorized_project = gateway_context.pop("authorized_project")
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
    assert authorized_project.tenant_id == "tenant-a"
    assert authorized_project.project_id == "project-a"
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
    policy = _PolicyService()
    services, gateway, _, _ = _runtime(policy_service=policy)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        {"action": "list_models"},
        _sdk_context(),
    )

    assert result == {"models": [{"name": "claude-sonnet"}]}
    assert len(gateway.list_calls) == 1
    project_id, user_id, tenant_id, authorized_project = gateway.list_calls[0]
    assert (project_id, user_id, tenant_id) == (
        "project-a",
        "principal-123",
        "tenant-a",
    )
    assert authorized_project is not None
    assert authorized_project.tenant_id == "tenant-a"
    assert policy.events == ["refresh", "evaluate"]
    assert policy.evaluations[0][1:] == ("get", "/v1/models")


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
