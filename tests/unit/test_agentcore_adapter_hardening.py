"""Adversarial coverage for the fail-closed AgentCore adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
    RuntimeReadiness,
    RuntimeServices,
)
from src.gateway.agentcore.schemas import REHEARSAL_SCHEMA
from src.gateway.auth.project_repository import (
    ProjectConfigConflict,
    ProjectStoreUnavailable,
)
from src.gateway.config_sync import RegionTopologyUnavailable
from src.gateway.models import (
    AuthMethod,
    GuardrailRule,
    MembershipStatus,
    Principal,
    Project,
    RequestContext,
    TenantRole,
)
from src.gateway.security.audit_trail import AuditEventType


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
        update_error: Exception | None = None,
    ) -> None:
        self.project = project or Project(
            project_id="project-a",
            tenant_id="tenant-a",
            name="Project A",
        )
        self.unavailable = unavailable
        self.missing = missing
        self.update_error = update_error
        self.calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[Project, int]] = []

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
        if self.project is None or self.project.tenant_id != tenant_id or self.project.project_id != project_id:
            return None
        return self.project

    async def update(
        self,
        project: Project,
        *,
        expected_revision: int,
    ) -> Project:
        self.update_calls.append((project, expected_revision))
        if self.update_error is not None:
            raise self.update_error
        if expected_revision != self.project.revision:
            raise ProjectConfigConflict("stale")
        self.project = replace(
            project,
            revision=expected_revision + 1,
        )
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


class _ConfigSync:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def refresh_if_stale(self) -> bool:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return True


class _AuditTrail:
    def __init__(
        self,
        *,
        durable_enabled: bool = True,
        fail_on_call: int | None = None,
    ) -> None:
        self.durable_enabled = durable_enabled
        self.fail_on_call = fail_on_call
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> object:
        call_number = len(self.records) + 1
        if self.fail_on_call == call_number:
            raise RuntimeError("audit credential=secret-value")
        self.records.append(kwargs)
        return object()


class _Gateway:
    def __init__(self, chat_result: Any | None = None) -> None:
        self.chat_result = chat_result or {"id": "completion-1"}
        self.chat_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.list_calls: list[tuple[str | None, str | None, str | None, Project | None]] = []

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
        self.list_calls.append((project_id, user_id, tenant_id, authorized_project))
        return {"models": [{"name": "claude-sonnet"}]}


class _StaticProvider:
    def __init__(self, runtime: RuntimeServices) -> None:
        self.runtime = runtime
        self.calls = 0

    async def get(self) -> RuntimeServices:
        self.calls += 1
        return self.runtime

    async def readiness(
        self,
        *,
        force: bool = False,
    ) -> RuntimeReadiness:
        assert force is True
        return RuntimeReadiness(
            ready=True,
            state="ready",
            dependencies={"runtime": "ready", "dynamodb": "ready"},
        )


def _principal(
    *,
    tenant_id: str = "tenant-a",
    projects: frozenset[str] = frozenset({"project-a"}),
    role: TenantRole = TenantRole.TENANT_MEMBER,
    scopes: frozenset[str] | None = None,
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
        scopes=(frozenset({"inference.invoke", "model.list"}) if scopes is None else scopes),
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
    config_sync: _ConfigSync | None = None,
    audit_trail: Any = ...,
) -> tuple[RuntimeServices, _Gateway, _Verifier, _Resolver]:
    resolved_gateway = gateway or _Gateway()
    verifier = _Verifier(verified_context or _verified_context())
    resolver = _Resolver(principal or _principal())
    projects = project_resolver or _ProjectResolver()
    resolved_audit = _AuditTrail() if audit_trail is ... else audit_trail
    services = RuntimeServices(
        gateway=resolved_gateway,
        token_verifier=verifier,
        principal_resolver=resolver,
        project_resolver=projects,
        project_config_store=projects,
        audit_trail=resolved_audit,
        policy_service=policy_service,
        config_sync=config_sync,
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


def _rehearsal_payload() -> dict[str, Any]:
    return {
        "schema": REHEARSAL_SCHEMA,
        "correlation_id": "a" * 32,
        "owner_id": "b" * 64,
        "release_commit": "c" * 40,
        "fence_token": 7,
        "expires_at_epoch": int(time.time()) + 3600,
        "operation": "verify-provider-fallback",
    }


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
    ("role", "scopes"),
    [
        (TenantRole.SERVICE, frozenset({"*"})),
        (
            TenantRole.TENANT_ADMIN,
            frozenset({"launch.rehearsal"}),
        ),
    ],
)
async def test_rehearsal_authority_is_exact_and_checked_before_project_work(
    role: TenantRole,
    scopes: frozenset[str],
) -> None:
    projects = _ProjectResolver()
    config_sync = _ConfigSync()
    services, gateway, _, _ = _runtime(
        principal=_principal(role=role, scopes=scopes),
        project_resolver=projects,
        config_sync=config_sync,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            _chat_payload(rehearsal=_rehearsal_payload()),
            _sdk_context(),
        )

    assert raised.value.status_code == 403
    assert raised.value.code == "authorization_denied"
    assert projects.calls == []
    assert config_sync.calls == 0
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_service_rehearsal_scope_allows_canonical_dispatch() -> None:
    services, gateway, _, _ = _runtime(
        principal=_principal(
            role=TenantRole.SERVICE,
            scopes=frozenset(
                {
                    "launch.rehearsal",
                    "inference.invoke",
                }
            ),
        ),
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        _chat_payload(rehearsal=_rehearsal_payload()),
        _sdk_context(),
    )

    assert result == {"id": "completion-1"}
    assert len(gateway.chat_calls) == 1


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
async def test_agentcore_refreshes_fleet_config_before_dispatch() -> None:
    config_sync = _ConfigSync()
    services, gateway, _, _ = _runtime(config_sync=config_sync)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(_chat_payload(), _sdk_context())

    assert result == {"id": "completion-1"}
    assert config_sync.calls == 1
    assert len(gateway.chat_calls) == 1


@pytest.mark.asyncio
async def test_agentcore_config_refresh_failure_uses_loaded_config() -> None:
    config_sync = _ConfigSync(RuntimeError("config refresh unavailable"))
    services, gateway, _, _ = _runtime(config_sync=config_sync)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(_chat_payload(), _sdk_context())

    assert result == {"id": "completion-1"}
    assert config_sync.calls == 1
    assert len(gateway.chat_calls) == 1


@pytest.mark.asyncio
async def test_agentcore_topology_refresh_failure_fails_closed() -> None:
    config_sync = _ConfigSync(RegionTopologyUnavailable("topology read unavailable"))
    services, gateway, _, _ = _runtime(config_sync=config_sync)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_chat_payload(), _sdk_context())

    assert raised.value.status_code == 503
    assert raised.value.code == "region_topology_unavailable"
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
    payload = _chat_payload() if invocation_action == "chat" else {"action": "list_models"}

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(payload, _sdk_context())

    expected_target = ("post", "/v1/chat/completions") if invocation_action == "chat" else ("get", "/v1/models")
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
    results = await asyncio.gather(*(provider.initialize() for _ in range(20)))

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
        provider="azure_openai",
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
    assert request_data == {key: value for key, value in payload.items() if key not in {"action", "provider"}}
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
        "provider": "azure_openai",
    }
    assert authorized_project.tenant_id == "tenant-a"
    assert authorized_project.project_id == "project-a"
    assert verifier.tokens == [TOKEN]
    assert resolver.contexts[0].roles == []
    assert resolver.contexts[0].scopes == []


@pytest.mark.asyncio
async def test_unsupported_provider_preference_fails_before_dispatch() -> None:
    services, gateway, _, _ = _runtime()
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            _chat_payload(provider="not-a-provider"),
            _sdk_context(),
        )

    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_payload"
    assert provider.calls == 0
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_cohere_required_tool_selection_fails_before_dispatch() -> None:
    services, gateway, _, _ = _runtime()
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            _chat_payload(
                provider="cohere",
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    }
                ],
                tool_choice="required",
            ),
            _sdk_context(),
        )

    assert raised.value.status_code == 400
    assert raised.value.code == "unsupported_provider_feature"
    assert "automatic tool selection" in raised.value.message
    assert provider.calls == 0
    assert gateway.chat_calls == []


@pytest.mark.asyncio
async def test_first_streamed_chunk_is_not_buffered_or_rewritten() -> None:
    produced: list[str] = []
    closed: list[bool] = []
    first_chunk = {
        "data": {
            "choices": [{"delta": {"content": "first"}}],
        }
    }

    async def chunks():
        try:
            produced.append("first")
            yield first_chunk
            produced.append("second")
            yield {"data": {"choices": [{"delta": {"content": "second"}}]}}
        finally:
            closed.append(True)

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
    assert closed == [True]


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
async def test_readiness_requires_canonical_identity_and_checks_dependencies() -> None:
    policy = _PolicyService()
    services, gateway, _, _ = _runtime(policy_service=policy)
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)

    result = await adapter.invoke(
        {"action": "readiness"},
        _sdk_context(),
    )

    assert result == {
        "status": "ready",
        "ready": True,
        "state": "ready",
        "dependencies": {
            "runtime": "ready",
            "dynamodb": "ready",
        },
    }
    assert provider.calls == 1
    assert gateway.chat_calls == []
    assert gateway.list_calls == []
    assert policy.evaluations[0][1:] == ("get", "/ready")


@pytest.mark.asyncio
async def test_tenant_member_reads_complete_canonical_project_config() -> None:
    project = Project(
        project_id="project-a",
        tenant_id="tenant-a",
        name="Production",
        budget_limit=500.0,
        alert_threshold=400.0,
        allowed_models=["claude-sonnet"],
        guardrail_rules=[
            GuardrailRule(
                name="block-secrets",
                rule_type="regex_match",
                pattern="secret",
                action="block",
                applies_to="both",
            )
        ],
        cache_enabled=True,
        cache_ttl_seconds=900,
        semantic_cache_enabled=True,
        semantic_cache_threshold=0.95,
        log_level="WARNING",
        log_destination="cloudwatch",
        prompt_caching_enabled=True,
        ltm_enabled=True,
        retention_period_hours=72,
        rate_limit_rpm=120,
        revision=7,
    )
    projects = _ProjectResolver(project)
    policy = _PolicyService()
    services, gateway, _, _ = _runtime(
        project_resolver=projects,
        policy_service=policy,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        {"action": "get_tenant_config"},
        _sdk_context(),
    )

    assert result == {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "revision": 7,
        "config": {
            "name": "Production",
            "budget_limit": 500.0,
            "alert_threshold": 400.0,
            "allowed_models": ["claude-sonnet"],
            "guardrail_rules": [
                {
                    "name": "block-secrets",
                    "rule_type": "regex_match",
                    "pattern": "secret",
                    "action": "block",
                    "applies_to": "both",
                }
            ],
            "cache_enabled": True,
            "cache_ttl_seconds": 900,
            "semantic_cache_enabled": True,
            "semantic_cache_threshold": 0.95,
            "log_level": "WARNING",
            "log_destination": "cloudwatch",
            "prompt_caching_enabled": True,
            "ltm_enabled": True,
            "retention_period_hours": 72,
            "rate_limit_rpm": 120,
        },
    }
    assert gateway.chat_calls == []
    assert policy.evaluations[0][1:] == ("get", "/v1/tenant/config")


@pytest.mark.asyncio
async def test_tenant_admin_updates_config_with_cas_without_mutating_snapshot() -> None:
    project = Project(
        project_id="project-a",
        tenant_id="tenant-a",
        name="Before",
        allowed_models=["model-a"],
        revision=4,
    )
    projects = _ProjectResolver(project)
    policy = _PolicyService()
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
        policy_service=policy,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    result = await adapter.invoke(
        {
            "action": "update_tenant_config",
            "expected_revision": 4,
            "config": {
                "name": "After",
                "allowed_models": ["model-b"],
                "prompt_caching_enabled": True,
            },
        },
        _sdk_context(),
    )

    assert result["revision"] == 5
    assert result["config"]["name"] == "After"
    assert result["config"]["allowed_models"] == ["model-b"]
    assert result["config"]["prompt_caching_enabled"] is True
    assert project.name == "Before"
    assert project.allowed_models == ["model-a"]
    assert len(projects.update_calls) == 1
    staged, expected_revision = projects.update_calls[0]
    assert expected_revision == 4
    assert staged.revision == 4
    assert policy.evaluations[0][1:] == ("put", "/v1/tenant/config")
    audit = services.audit_trail
    assert [record["event_type"] for record in audit.records] == [
        AuditEventType.TENANT_CONFIG_MUTATION_REQUEST,
        AuditEventType.TENANT_CONFIG_MUTATION_RESULT,
    ]
    assert audit.records[0]["request_id"] == audit.records[1]["request_id"]
    assert audit.records[1]["data"]["status"] == "committed"
    assert audit.records[1]["data"]["revision"] == 5
    assert "After" not in repr(audit.records)
    assert "model-b" not in repr(audit.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "audit",
    [
        None,
        _AuditTrail(durable_enabled=False),
    ],
)
async def test_tenant_config_write_requires_durable_audit_before_cas(
    audit,
) -> None:
    projects = _ProjectResolver()
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
        audit_trail=audit,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 0,
                "config": {"name": "Changed"},
            },
            _sdk_context(),
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "tenant_config_audit_unavailable"
    assert projects.update_calls == []


@pytest.mark.asyncio
async def test_tenant_config_request_audit_failure_prevents_cas() -> None:
    projects = _ProjectResolver()
    audit = _AuditTrail(fail_on_call=1)
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
        audit_trail=audit,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 0,
                "config": {"name": "Changed"},
            },
            _sdk_context(),
        )

    assert raised.value.code == "tenant_config_audit_unavailable"
    assert projects.update_calls == []


@pytest.mark.asyncio
async def test_tenant_config_result_audit_failure_withholds_success() -> None:
    projects = _ProjectResolver()
    audit = _AuditTrail(fail_on_call=2)
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
        audit_trail=audit,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 0,
                "config": {"name": "Committed"},
            },
            _sdk_context(),
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "tenant_config_audit_unavailable"
    assert len(projects.update_calls) == 1
    assert projects.project.name == "Committed"


@pytest.mark.asyncio
async def test_oversized_tenant_config_is_rejected_before_store_write() -> None:
    projects = _ProjectResolver(
        Project(
            project_id="project-a",
            tenant_id="tenant-a",
            name="Current",
            members=["member-" + ("x" * 1024)],
        )
    )
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))
    rule = {
        "name": "large-pattern",
        "rule_type": "regex_match",
        "pattern": "x" * 4096,
        "action": "block",
        "applies_to": "both",
    }

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 0,
                "config": {"guardrail_rules": [{**rule, "name": f"large-pattern-{index}"} for index in range(100)]},
            },
            _sdk_context(),
        )

    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_payload"
    assert projects.update_calls == []


@pytest.mark.asyncio
async def test_tenant_member_config_update_is_denied_before_store_write() -> None:
    projects = _ProjectResolver()
    services, _, _, _ = _runtime(project_resolver=projects)
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 0,
                "config": {"name": "Escalated"},
            },
            _sdk_context(),
        )

    assert raised.value.status_code == 403
    assert raised.value.code == "authorization_denied"
    assert projects.update_calls == []


@pytest.mark.asyncio
async def test_tenant_config_stale_revision_is_a_conflict_without_write() -> None:
    projects = _ProjectResolver(
        Project(
            project_id="project-a",
            tenant_id="tenant-a",
            name="Current",
            revision=3,
        )
    )
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 2,
                "config": {"name": "Stale"},
            },
            _sdk_context(),
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "tenant_config_write_conflict"
    assert projects.update_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            ProjectConfigConflict("concurrent"),
            409,
            "tenant_config_write_conflict",
        ),
        (
            ProjectStoreUnavailable("unavailable"),
            503,
            "tenant_config_unavailable",
        ),
    ],
)
async def test_tenant_config_store_failures_are_sanitized(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    projects = _ProjectResolver(update_error=error)
    services, _, _, _ = _runtime(
        principal=_principal(role=TenantRole.TENANT_ADMIN),
        project_resolver=projects,
    )
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 0,
                "config": {"name": "Changed"},
            },
            _sdk_context(),
        )

    assert raised.value.status_code == status_code
    assert raised.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": "update_tenant_config"},
        {
            "action": "update_tenant_config",
            "expected_revision": True,
            "config": {"name": "Changed"},
        },
        {
            "action": "update_tenant_config",
            "expected_revision": 0,
            "config": {},
        },
        {
            "action": "update_tenant_config",
            "expected_revision": 0,
            "config": {"tenant_id": "other"},
        },
        {
            "action": "update_tenant_config",
            "expected_revision": 0,
            "config": {"allowed_models": ["same", "same"]},
        },
        {
            "action": "update_tenant_config",
            "expected_revision": 0,
            "config": {"semantic_cache_threshold": float("nan")},
        },
        {
            "action": "update_tenant_config",
            "expected_revision": 0,
            "config": {"budget_limit": 10**1000},
        },
        {
            "action": "update_tenant_config",
            "expected_revision": 0,
            "config": {
                "guardrail_rules": [
                    {
                        "name": "invalid-regex",
                        "rule_type": "regex_match",
                        "pattern": "([",
                        "action": "block",
                        "applies_to": "both",
                    }
                ]
            },
        },
    ],
)
async def test_tenant_config_schema_rejects_malformed_updates_before_runtime(
    payload: dict[str, Any],
) -> None:
    services, _, _, _ = _runtime()
    provider = _StaticProvider(services)
    adapter = AgentCoreAdapter(provider)

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(payload, _sdk_context())

    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_payload"
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
    adapter = AgentCoreAdapter(
        _StaticProvider(services),
        allow_facade_identity=True,
    )

    result = await adapter.invoke(
        _chat_payload(),
        _sdk_context(header_name=FACADE_IDENTITY_HEADER),
    )

    assert result == {"id": "completion-1"}
    assert verifier.tokens == [TOKEN]
    assert len(gateway.chat_calls) == 1


@pytest.mark.asyncio
async def test_facade_identity_header_is_rejected_by_direct_jwt_runtime() -> None:
    services, gateway, verifier, _ = _runtime()
    adapter = AgentCoreAdapter(_StaticProvider(services))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            _chat_payload(),
            _sdk_context(header_name=FACADE_IDENTITY_HEADER),
        )

    assert raised.value.status_code == 401
    assert raised.value.code == "invalid_runtime_identity"
    assert verifier.tokens == []
    assert gateway.chat_calls == []


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
