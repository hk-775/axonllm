"""Tests for the multi-strategy auth middleware."""

import asyncio
import base64
import json

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import APIKey, AuthMethod, RequestContext


# --- Fakes ---


class FakeOIDCService:
    def __init__(self, valid_tokens: dict[str, RequestContext] | None = None):
        self._valid_tokens = valid_tokens or {}

    async def validate_alb_jwt(self, token: str) -> RequestContext | None:
        return self._valid_tokens.get(f"alb:{token}")

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None:
        return self._valid_tokens.get(f"oidc:{token}")


class FakeAPIKeyService:
    def __init__(self, valid_keys: dict[str, APIKey] | None = None):
        self._valid_keys = valid_keys or {}

    async def validate_key(self, raw_key: str) -> APIKey | None:
        return self._valid_keys.get(raw_key)


class FakePolicyService:
    def __init__(self, decision: str = "ALLOW"):
        self.decision = decision

    async def evaluate(self, context, action, resource):
        return self.decision


# --- Helpers ---


def _build_app(oidc_service=None, api_key_service=None, policy_service=None, mode="ENFORCE"):
    async def protected(request: Request):
        ctx = request.state.context
        return JSONResponse({
            "user_id": ctx.user_id,
            "project_id": ctx.project_id,
            "auth_method": ctx.auth_method.value,
        })

    app = Starlette(routes=[
        Route("/api/chat", protected, methods=["POST"]),
        Route("/health", protected, methods=["GET"]),
    ])
    app.add_middleware(
        AuthMiddleware,
        oidc_service=oidc_service,
        api_key_service=api_key_service,
        policy_service=policy_service,
        mode=mode,
    )
    return app


# --- Tests ---


class TestPublicPaths:
    def test_health_bypasses_auth(self):
        app = _build_app(mode="ENFORCE")
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["auth_method"] == "anonymous"


class TestOIDCAuth:
    def test_alb_header_authenticates(self):
        ctx = RequestContext(
            user_id="user-1", project_id="proj-1",
            roles=["admin"], scopes=["openid"],
            auth_method=AuthMethod.OIDC_JWT,
        )
        oidc = FakeOIDCService(valid_tokens={"alb:valid-alb-token": ctx})
        app = _build_app(oidc_service=oidc, mode="ENFORCE")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={"X-Amzn-Oidc-Data": "valid-alb-token"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "user-1"
        assert resp.json()["auth_method"] == "oidc_jwt"

    def test_bearer_oidc_authenticates(self):
        ctx = RequestContext(
            user_id="user-2", project_id="proj-2",
            roles=[], scopes=[],
            auth_method=AuthMethod.OIDC_JWT,
        )
        oidc = FakeOIDCService(valid_tokens={"oidc:my-jwt-token": ctx})
        app = _build_app(oidc_service=oidc, mode="ENFORCE")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={"Authorization": "Bearer my-jwt-token"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "user-2"


class TestAPIKeyAuth:
    def test_x_api_key_header_authenticates(self):
        key = APIKey(
            key_id="axk_123", key_hash="h", project_id="proj-3",
            name="test", scopes=["chat:invoke"], created_by="admin",
        )
        api_key_svc = FakeAPIKeyService(valid_keys={"axon_" + "a" * 64: key})
        app = _build_app(api_key_service=api_key_svc, mode="ENFORCE")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={"X-Api-Key": "axon_" + "a" * 64})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "apikey:axk_123"
        assert resp.json()["auth_method"] == "api_key"

    def test_bearer_api_key_authenticates(self):
        key = APIKey(
            key_id="axk_456", key_hash="h", project_id="proj-4",
            name="test", scopes=["chat:invoke"], created_by="admin",
        )
        raw = "axon_" + "b" * 64
        api_key_svc = FakeAPIKeyService(valid_keys={raw: key})
        app = _build_app(api_key_service=api_key_svc, mode="ENFORCE")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200
        assert resp.json()["project_id"] == "proj-4"


class TestPriorityOrder:
    def test_oidc_wins_over_api_key(self):
        oidc_ctx = RequestContext(
            user_id="oidc-user", project_id="oidc-proj",
            roles=[], scopes=[], auth_method=AuthMethod.OIDC_JWT,
        )
        oidc = FakeOIDCService(valid_tokens={"alb:alb-token": oidc_ctx})

        key = APIKey(
            key_id="axk_x", key_hash="h", project_id="key-proj",
            name="test", scopes=["chat:invoke"], created_by="admin",
        )
        api_key_svc = FakeAPIKeyService(valid_keys={"axon_" + "c" * 64: key})

        app = _build_app(oidc_service=oidc, api_key_service=api_key_svc, mode="ENFORCE")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={
            "X-Amzn-Oidc-Data": "alb-token",
            "X-Api-Key": "axon_" + "c" * 64,
        })
        assert resp.json()["user_id"] == "oidc-user"


class TestNoCredentials:
    def test_returns_401_in_enforce_mode(self):
        app = _build_app(mode="ENFORCE")
        client = TestClient(app)
        resp = client.post("/api/chat")
        assert resp.status_code == 401

    def test_allows_anonymous_in_log_only_mode(self):
        app = _build_app(mode="LOG_ONLY")
        client = TestClient(app)
        resp = client.post("/api/chat")
        assert resp.status_code == 200
        assert resp.json()["auth_method"] == "anonymous"


class TestPolicyDenial:
    def test_policy_deny_returns_403(self):
        ctx = RequestContext(
            user_id="user-1", project_id="proj-1",
            roles=[], scopes=[], auth_method=AuthMethod.OIDC_JWT,
        )
        oidc = FakeOIDCService(valid_tokens={"alb:token": ctx})
        policy = FakePolicyService(decision="DENY")

        app = _build_app(oidc_service=oidc, policy_service=policy, mode="ENFORCE")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={"X-Amzn-Oidc-Data": "token"})
        assert resp.status_code == 403

    def test_policy_deny_logs_only_in_log_mode(self):
        ctx = RequestContext(
            user_id="user-1", project_id="proj-1",
            roles=[], scopes=[], auth_method=AuthMethod.OIDC_JWT,
        )
        oidc = FakeOIDCService(valid_tokens={"alb:token": ctx})
        policy = FakePolicyService(decision="DENY")

        app = _build_app(oidc_service=oidc, policy_service=policy, mode="LOG_ONLY")
        client = TestClient(app)

        resp = client.post("/api/chat", headers={"X-Amzn-Oidc-Data": "token"})
        assert resp.status_code == 200
