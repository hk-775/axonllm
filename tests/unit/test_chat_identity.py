"""Regression tests for task #3 — chat identity must come from the authenticated
context, never from the request body/query params.

Guards against tenant impersonation: a caller must not be able to set user_id or
project_id in the body to attribute quota/budget/model-access to another tenant.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.cache_manager import CacheManager
from src.gateway.chat.client_agent import ClientAgent
from src.gateway.chat.routes import (
    ChatAPI,
    _authorized_project,
    _identity_from_context,
    create_chat_routes,
)
from src.gateway.models import (
    AuthMethod,
    ChatCompletionRequest,
    Project,
    RequestContext,
)


# ---------------------------------------------------------------------------
# _identity_from_context — the trust boundary
# ---------------------------------------------------------------------------


def _request_with_context(ctx):
    return SimpleNamespace(state=SimpleNamespace(context=ctx))


class TestIdentityFromContext:
    def test_api_key_context_used(self):
        ctx = RequestContext(
            user_id="apikey:k1", project_id="project-b", roles=["service"],
            scopes=[], auth_method=AuthMethod.API_KEY, api_key_id="k1",
            tenant_id="tenant-b",
        )
        assert _identity_from_context(_request_with_context(ctx)) == (
            "apikey:k1",
            "project-b",
            "tenant-b",
        )

    def test_oidc_context_used(self):
        ctx = RequestContext(
            user_id="alice", project_id="proj-alpha", roles=["dev"],
            scopes=[], auth_method=AuthMethod.OIDC_JWT,
        )
        assert _identity_from_context(_request_with_context(ctx)) == (
            "alice",
            "proj-alpha",
            None,
        )

    def test_anonymous_context_returns_none(self):
        # LOG_ONLY / dev — do not trust for attribution; fall back to defaults downstream.
        ctx = RequestContext(
            user_id="anonymous", project_id="", roles=[], scopes=[],
            auth_method=AuthMethod.ANONYMOUS, tenant_id="ignored-tenant",
        )
        assert _identity_from_context(_request_with_context(ctx)) == (
            None,
            None,
            None,
        )

    def test_missing_context_returns_none(self):
        assert _identity_from_context(
            SimpleNamespace(state=SimpleNamespace())
        ) == (None, None, None)

    def test_authoritative_project_comes_only_from_request_state(self):
        project = Project(
            project_id="shared",
            tenant_id="tenant-a",
            name="Tenant A",
        )
        ctx = RequestContext(
            user_id="alice",
            project_id="shared",
            roles=[],
            scopes=[],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id="tenant-a",
            authorized_project=project,
        )

        assert _authorized_project(_request_with_context(ctx)) is project


# ---------------------------------------------------------------------------
# ClientAgent — attribution follows the passed (context) identity, not defaults
# ---------------------------------------------------------------------------


class _CapturingGateway:
    def __init__(self):
        self.captured = []
        self.cache_keys = []
        self.model_calls = []
        self.cost_tracker = SimpleNamespace(_records=[])
        self._user_configs = {}

    async def handle_chat_completion(self, request_data, context):
        self.captured.append(dict(context))
        request = ChatCompletionRequest(
            model=request_data["model"],
            messages=request_data["messages"],
            stream=request_data.get("stream", False),
        )
        self.cache_keys.append(
            CacheManager().compute_cache_key(
                request,
                context["project_id"],
                context.get("tenant_id"),
            )
        )
        if request_data.get("stream"):
            async def _stream():
                yield {
                    "data": {
                        "id": "x",
                        "model": "m",
                        "choices": [
                            {"delta": {"content": "ok"}}
                        ],
                    }
                }
                yield {"data": "[DONE]"}

            return _stream()
        return {
            "id": "x", "model": "m", "provider": "p",
            "choices": [{"message": {"content": "ok"}}], "usage": {},
        }

    async def handle_list_models(self, **kwargs):
        self.model_calls.append(dict(kwargs))
        return {"models": []}


def _native_client(
    gateway: _CapturingGateway,
    *,
    tenant_id: str | None,
    auth_method: AuthMethod = AuthMethod.API_KEY,
) -> TestClient:
    api = ChatAPI(ClientAgent(gateway))

    class _ContextMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.context = RequestContext(
                user_id=(
                    "anonymous"
                    if auth_method is AuthMethod.ANONYMOUS
                    else f"user:{tenant_id}"
                ),
                project_id=(
                    ""
                    if auth_method is AuthMethod.ANONYMOUS
                    else "shared-project"
                ),
                roles=[],
                scopes=[],
                auth_method=auth_method,
                tenant_id=tenant_id,
            )
            return await call_next(request)

    app = Starlette(routes=create_chat_routes(api))
    app.add_middleware(_ContextMiddleware)
    return TestClient(app)


class TestClientAgentAttribution:
    def test_authenticated_identity_wins(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw, default_user_id="chat-user", default_project_id="chat-project")
        asyncio.run(ca.chat(
            "m", [{"role": "user", "content": "hi"}],
            user_id="apikey:k1", project_id="project-b",
        ))
        assert gw.captured[-1]["user_id"] == "apikey:k1"
        assert gw.captured[-1]["project_id"] == "project-b"

    def test_anonymous_falls_back_to_defaults(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw, default_user_id="chat-user", default_project_id="chat-project")
        asyncio.run(ca.chat(
            "m", [{"role": "user", "content": "hi"}],
            user_id=None, project_id=None,
        ))
        assert gw.captured[-1]["user_id"] == "chat-user"
        assert gw.captured[-1]["project_id"] == "chat-project"
        assert "tenant_id" not in gw.captured[-1]

    def test_authenticated_tenant_reaches_gateway_without_authorized_project(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)

        asyncio.run(
            ca.chat(
                "m",
                [{"role": "user", "content": "hi"}],
                user_id="alice",
                project_id="shared",
                tenant_id="tenant-a",
            )
        )

        assert gw.captured[-1]["tenant_id"] == "tenant-a"
        assert "authorized_project" not in gw.captured[-1]

    def test_authoritative_project_and_tenant_reach_the_gateway(self):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        project = Project(
            project_id="shared",
            tenant_id="tenant-a",
            name="Tenant A",
        )

        asyncio.run(
            ca.chat(
                "m",
                [{"role": "user", "content": "hi"}],
                user_id="alice",
                project_id="shared",
                authorized_project=project,
            )
        )

        assert gw.captured[-1]["tenant_id"] == "tenant-a"
        assert gw.captured[-1]["authorized_project"] is project

    @pytest.mark.parametrize(
        ("project_id", "tenant_id"),
        [
            ("other-project", "tenant-a"),
            ("shared", "tenant-b"),
        ],
    )
    def test_authoritative_project_rejects_scope_mismatch(
        self,
        project_id,
        tenant_id,
    ):
        gw = _CapturingGateway()
        ca = ClientAgent(gw)
        project = Project(
            project_id="shared",
            tenant_id="tenant-a",
            name="Tenant A",
        )

        with pytest.raises(ValueError, match="does not match"):
            asyncio.run(
                ca.chat(
                    "m",
                    [{"role": "user", "content": "hi"}],
                    user_id="alice",
                    project_id=project_id,
                    tenant_id=tenant_id,
                    authorized_project=project,
                )
            )

        assert gw.captured == []


class TestNativeHTTPAttribution:
    def test_tenant_reaches_models_chat_and_stream_without_project_object(self):
        gateway = _CapturingGateway()
        client = _native_client(gateway, tenant_id="tenant-a")

        assert client.get("/api/models").status_code == 200
        assert client.post(
            "/api/chat",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
            },
        ).status_code == 200
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as response:
            assert response.status_code == 200
            response.read()

        assert gateway.model_calls[-1]["tenant_id"] == "tenant-a"
        assert [ctx["tenant_id"] for ctx in gateway.captured] == [
            "tenant-a",
            "tenant-a",
        ]
        assert all(
            "authorized_project" not in ctx
            for ctx in gateway.captured
        )
        assert all(
            ctx["allow_legacy_project_lookup"] is True
            for ctx in gateway.captured
        )

    def test_same_project_in_two_authenticated_tenants_has_distinct_cache_keys(
        self,
    ):
        gateway = _CapturingGateway()
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "same prompt"}],
        }

        assert _native_client(
            gateway,
            tenant_id="tenant-a",
        ).post("/api/chat", json=body).status_code == 200
        assert _native_client(
            gateway,
            tenant_id="tenant-b",
        ).post("/api/chat", json=body).status_code == 200

        assert [ctx["project_id"] for ctx in gateway.captured] == [
            "shared-project",
            "shared-project",
        ]
        assert [ctx["tenant_id"] for ctx in gateway.captured] == [
            "tenant-a",
            "tenant-b",
        ]
        assert gateway.cache_keys[0] != gateway.cache_keys[1]

    def test_anonymous_native_request_keeps_local_fallback(self):
        gateway = _CapturingGateway()
        client = _native_client(
            gateway,
            tenant_id="ignored-tenant",
            auth_method=AuthMethod.ANONYMOUS,
        )

        response = client.post(
            "/api/chat",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert gateway.captured[-1]["user_id"] == "chat-user"
        assert gateway.captured[-1]["project_id"] == "chat-project"
        assert "tenant_id" not in gateway.captured[-1]
