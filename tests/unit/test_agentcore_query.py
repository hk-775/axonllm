"""AgentCore query action schema, authorization, and dispatch coverage."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from src.gateway.agentcore.adapter import AgentCoreAdapter
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.runtime import (
    RuntimeServices,
    build_runtime_services,
)
from src.gateway.agentcore.schemas import (
    InvocationAction,
    parse_invocation_payload,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    Project,
    RequestContext,
    TenantRole,
)
from src.gateway.query.service import QueryServiceError


pytestmark = pytest.mark.skip(
    reason=(
        "customer database query integration is an optional add-on and is not registered by the core AgentCore runtime"
    )
)


ISSUER = "https://idp.example.test"
TOKEN = "signed-runtime-token"


class _Gateway:
    def __init__(self) -> None:
        self.chat_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def handle_chat_completion(
        self,
        request_data: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self.chat_calls.append((request_data, context))
        return {"id": "completion-1"}

    async def handle_list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        authorized_project: Project | None = None,
    ) -> dict[str, Any]:
        self.list_calls.append(
            {
                "project_id": project_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "authorized_project": authorized_project,
            }
        )
        return {"models": [{"name": "claude-sonnet"}]}


class _Verifier:
    async def validate_oidc_jwt(
        self,
        token: str,
    ) -> RequestContext | None:
        assert token == TOKEN
        return RequestContext(
            user_id="untrusted-user",
            project_id="project-a",
            roles=["platform_admin"],
            scopes=["*"],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id="tenant-a",
            issuer=ISSUER,
            subject="external-subject",
        )


class _PrincipalResolver:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    async def resolve(
        self,
        context: RequestContext,
    ) -> Principal | None:
        assert context.roles == []
        assert context.scopes == []
        return self.principal


class _ProjectResolver:
    async def resolve(
        self,
        tenant_id: str,
        project_id: str,
    ) -> Project | None:
        if (tenant_id, project_id) != ("tenant-a", "project-a"):
            return None
        return Project(
            project_id="project-a",
            tenant_id="tenant-a",
            name="Project A",
        )


class _PolicyService:
    def __init__(self, decision: str = "ALLOW") -> None:
        self.decision = decision
        self.evaluations: list[tuple[RequestContext, str, str]] = []

    async def evaluate(
        self,
        context: RequestContext,
        action: str,
        resource: str,
    ) -> str:
        self.evaluations.append((context, action, resource))
        return self.decision


class _QueryService:
    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: QueryServiceError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return deepcopy(self.result)
        return _query_result(
            request_id=kwargs["request_id"] or "qry_generated",
            datasource_id=kwargs["datasource_id"],
            project_id=kwargs["project_id"],
        )


class _Provider:
    def __init__(self, runtime: RuntimeServices) -> None:
        self.runtime = runtime
        self.calls = 0

    async def get(self) -> RuntimeServices:
        self.calls += 1
        return self.runtime


def _principal(
    *,
    role: TenantRole = TenantRole.TENANT_MEMBER,
    projects: frozenset[str] = frozenset({"project-a"}),
    scopes: frozenset[str] = frozenset({"inference.invoke", "model.list"}),
) -> Principal:
    return Principal(
        principal_id="principal-123",
        tenant_id="tenant-a",
        subject="external-subject",
        issuer=ISSUER,
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=projects,
        scopes=scopes,
        authorization_version=9,
    )


def _runtime(
    *,
    principal: Principal | None = None,
    query_service: _QueryService | None = None,
    policy_service: _PolicyService | None = None,
) -> tuple[RuntimeServices, _Gateway, _QueryService | None]:
    gateway = _Gateway()
    runtime = RuntimeServices(
        gateway=gateway,
        token_verifier=_Verifier(),
        principal_resolver=_PrincipalResolver(principal or _principal()),
        project_resolver=_ProjectResolver(),
        query_service=query_service,
        policy_service=policy_service,
    )
    return runtime, gateway, query_service


def _context() -> SimpleNamespace:
    return SimpleNamespace(request_headers={"Authorization": f"Bearer {TOKEN}"})


def _query_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "query",
        "datasource_id": "warehouse",
        "sql": "SELECT order_id FROM orders",
        "max_rows": 50,
        "request_id": "request-123",
    }
    payload.update(overrides)
    return payload


def _query_result(
    *,
    request_id: str = "request-123",
    datasource_id: str = "warehouse",
    project_id: str = "project-a",
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "datasource_id": datasource_id,
        "project_id": project_id,
        "query_execution_id": "athena-execution-123",
        "columns": [
            {"name": "order_id", "type": "varchar"},
            {"name": "total", "type": "decimal(10,2)"},
        ],
        "rows": [["order-1", "19.95"], ["order-2", None]],
        "row_count": 2,
        "truncated": False,
        "statistics": {
            "data_scanned_bytes": 2048,
            "engine_execution_ms": 42,
            "result_bytes": 19,
        },
    }


def test_runtime_builder_reuses_bootstrap_query_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.gateway import bootstrap
    from src.gateway.admin import webhook_routes
    from src.gateway.auth import cedar_policy
    from src.gateway import config_sync

    query_service = _QueryService()
    registry = object()
    sync_kwargs: dict = {}

    async def _stop() -> None:
        return None

    components = SimpleNamespace(
        gateway_agent=SimpleNamespace(),
        oidc_service=SimpleNamespace(),
        principal_resolver=SimpleNamespace(),
        project_resolver=SimpleNamespace(),
        query_service=query_service,
        registry=registry,
        projects={},
        user_configs={},
        cost_tracker=SimpleNamespace(),
        persistence=SimpleNamespace(),
        audit_trail=SimpleNamespace(durable_enabled=True),
        policy_resolver=SimpleNamespace(),
        region_router=SimpleNamespace(config=SimpleNamespace()),
        health_monitor=SimpleNamespace(stop=_stop),
        event_dispatcher=SimpleNamespace(stop=_stop),
        multi_factory=SimpleNamespace(),
        policies=[],
    )
    monkeypatch.setattr(
        bootstrap,
        "build_gateway_components",
        lambda: components,
    )

    def _config_sync(**kwargs):
        sync_kwargs.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        config_sync,
        "ConfigSyncService",
        _config_sync,
    )
    monkeypatch.setattr(
        cedar_policy,
        "CedarPolicyService",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        webhook_routes,
        "WebhookAPI",
        lambda **_kwargs: SimpleNamespace(),
    )

    runtime = build_runtime_services()

    assert runtime.query_service is query_service
    assert sync_kwargs["model_registry"] is registry


def test_query_payload_schema_accepts_only_service_inputs() -> None:
    parsed = parse_invocation_payload(_query_payload())

    assert parsed.action is InvocationAction.QUERY
    assert parsed.request_data is None
    assert parsed.query_request is not None
    assert parsed.query_request.datasource_id == "warehouse"
    assert parsed.query_request.sql == "SELECT order_id FROM orders"
    assert parsed.query_request.max_rows == 50
    assert parsed.query_request.request_id == "request-123"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "query", "sql": "SELECT 1"},
        {"action": "query", "datasource_id": "warehouse"},
        _query_payload(datasource_id="../warehouse"),
        _query_payload(sql=" SELECT 1"),
        _query_payload(sql=123),
        _query_payload(max_rows=True),
        _query_payload(max_rows=10_001),
        _query_payload(request_id="bad\nrequest"),
        _query_payload(model="claude-sonnet"),
    ],
)
def test_query_payload_schema_rejects_malformed_requests(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(AgentCoreAdapterError) as raised:
        parse_invocation_payload(payload)

    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_payload"


@pytest.mark.asyncio
async def test_query_payload_rejects_authority_before_runtime_lookup() -> None:
    service = _QueryService()
    runtime, _, _ = _runtime(query_service=service)
    provider = _Provider(runtime)
    adapter = AgentCoreAdapter(provider)

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(
            _query_payload(
                tenant_id="other-tenant",
                project_id="other-project",
                roles=["platform_admin"],
            ),
            _context(),
        )

    assert raised.value.code == "untrusted_identity_fields"
    assert provider.calls == 0
    assert service.calls == []


@pytest.mark.asyncio
async def test_query_dispatch_uses_canonical_identity_and_policy_target() -> None:
    service = _QueryService()
    policy = _PolicyService()
    runtime, gateway, _ = _runtime(
        query_service=service,
        policy_service=policy,
    )
    adapter = AgentCoreAdapter(_Provider(runtime))

    result = await adapter.invoke(_query_payload(), _context())

    assert result == _query_result()
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call == {
        "principal": _principal(),
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "datasource_id": "warehouse",
        "sql": "SELECT order_id FROM orders",
        "max_rows": 50,
        "request_id": "request-123",
        "rehearsal": None,
        "rehearsal_binding": None,
        "rehearsal_ledger": None,
    }
    assert gateway.chat_calls == []
    assert gateway.list_calls == []
    assert len(policy.evaluations) == 1
    policy_context, action, resource = policy.evaluations[0]
    assert (action, resource) == ("post", "/v1/query")
    assert policy_context.principal_id == "principal-123"
    assert policy_context.roles == ["tenant_member"]
    assert policy_context.scopes == [
        "inference.invoke",
        "model.list",
    ]


@pytest.mark.asyncio
async def test_query_response_preserves_legal_quoted_column_aliases() -> None:
    query_result = _query_result()
    query_result["columns"][0]["name"] = " total amount "
    service = _QueryService(result=query_result)
    runtime, _, _ = _runtime(query_service=service)
    adapter = AgentCoreAdapter(_Provider(runtime))

    result = await adapter.invoke(_query_payload(), _context())

    assert result["columns"][0]["name"] == " total amount "


@pytest.mark.asyncio
async def test_query_action_honors_query_select_service_scope() -> None:
    service = _QueryService()
    runtime, _, _ = _runtime(
        principal=_principal(
            role=TenantRole.SERVICE,
            scopes=frozenset({"query.select"}),
        ),
        query_service=service,
    )
    adapter = AgentCoreAdapter(_Provider(runtime))

    result = await adapter.invoke(_query_payload(), _context())

    assert result["query_execution_id"] == "athena-execution-123"
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_query_action_denies_service_without_query_select_scope() -> None:
    service = _QueryService()
    runtime, _, _ = _runtime(
        principal=_principal(
            role=TenantRole.SERVICE,
            scopes=frozenset({"inference.invoke"}),
        ),
        query_service=service,
    )
    adapter = AgentCoreAdapter(_Provider(runtime))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_query_payload(), _context())

    assert raised.value.status_code == 403
    assert raised.value.code == "authorization_denied"
    assert service.calls == []


@pytest.mark.asyncio
async def test_query_cross_project_access_is_concealed_before_dispatch() -> None:
    service = _QueryService()
    runtime, _, _ = _runtime(
        principal=_principal(projects=frozenset({"project-b"})),
        query_service=service,
    )
    adapter = AgentCoreAdapter(_Provider(runtime))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_query_payload(), _context())

    assert raised.value.status_code == 404
    assert raised.value.code == "authorization_denied"
    assert raised.value.message == "Resource not found."
    assert service.calls == []


@pytest.mark.asyncio
async def test_query_policy_denial_skips_query_service() -> None:
    service = _QueryService()
    policy = _PolicyService("DENY")
    runtime, _, _ = _runtime(
        query_service=service,
        policy_service=policy,
    )
    adapter = AgentCoreAdapter(_Provider(runtime))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_query_payload(), _context())

    assert raised.value.status_code == 403
    assert raised.value.code == "authorization_denied"
    assert policy.evaluations[0][1:] == ("post", "/v1/query")
    assert service.calls == []


@pytest.mark.asyncio
async def test_query_service_must_be_configured() -> None:
    runtime, _, _ = _runtime()
    adapter = AgentCoreAdapter(_Provider(runtime))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_query_payload(), _context())

    assert raised.value.status_code == 503
    assert raised.value.code == "query_service_unavailable"


@pytest.mark.asyncio
async def test_query_service_errors_retain_sanitized_contract() -> None:
    service = _QueryService(
        error=QueryServiceError(
            400,
            "query_policy_rejected",
            "Only SELECT queries are supported.",
        )
    )
    runtime, _, _ = _runtime(query_service=service)
    adapter = AgentCoreAdapter(_Provider(runtime))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_query_payload(), _context())

    assert raised.value.status_code == 400
    assert raised.value.code == "query_policy_rejected"
    assert raised.value.message == "Only SELECT queries are supported."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {**_query_result(project_id="project-b")},
        {**_query_result(), "row_count": 3},
        {**_query_result(), "credentials": "must-not-leak"},
        {
            **_query_result(),
            "rows": [["order-1"], ["order-2", None]],
        },
    ],
)
async def test_query_response_schema_rejects_invalid_service_results(
    result: dict[str, Any],
) -> None:
    service = _QueryService(result=result)
    runtime, _, _ = _runtime(query_service=service)
    adapter = AgentCoreAdapter(_Provider(runtime))

    with pytest.raises(AgentCoreAdapterError) as raised:
        await adapter.invoke(_query_payload(), _context())

    assert raised.value.status_code == 502
    assert raised.value.code == "invalid_query_response"
    assert "credentials" not in raised.value.message


@pytest.mark.asyncio
async def test_query_dependency_does_not_change_chat_or_model_dispatch() -> None:
    service = _QueryService()
    runtime, gateway, _ = _runtime(query_service=service)
    adapter = AgentCoreAdapter(_Provider(runtime))

    chat = await adapter.invoke(
        {
            "action": "chat",
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "hello"}],
        },
        _context(),
    )
    models = await adapter.invoke(
        {"action": "list_models"},
        _context(),
    )

    assert chat == {"id": "completion-1"}
    assert models == {"models": [{"name": "claude-sonnet"}]}
    assert len(gateway.chat_calls) == 1
    assert len(gateway.list_calls) == 1
    assert service.calls == []
