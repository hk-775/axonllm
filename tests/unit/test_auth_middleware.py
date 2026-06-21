"""Unit tests for AuthMiddleware."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.auth import AuthMiddleware, IdentityService, PolicyService
from src.gateway.models import RequestContext


# --- Fake services for testing ---


class FakeIdentityService:
    """Configurable fake identity service for testing."""

    def __init__(self, claims: dict | None = None):
        self._claims = claims

    async def validate_token(self, token: str) -> dict | None:
        if token == "invalid":
            return None
        return self._claims


class FakePolicyService:
    """Configurable fake policy service for testing."""

    def __init__(self, decision: str = "ALLOW"):
        self.decision = decision
        self.last_context: RequestContext | None = None
        self.last_action: str | None = None
        self.last_resource: str | None = None

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        self.last_context = context
        self.last_action = action
        self.last_resource = resource
        return self.decision


# --- Helpers ---

VALID_CLAIMS = {
    "sub": "user-123",
    "project_id": "proj-abc",
    "roles": ["admin", "user"],
    "scopes": ["read", "write"],
}


captured_context: RequestContext | None = None


async def echo_endpoint(request: Request) -> JSONResponse:
    """Simple endpoint that returns the attached RequestContext."""
    global captured_context
    ctx = getattr(request.state, "context", None)
    captured_context = ctx
    if ctx:
        return JSONResponse(
            {
                "user_id": ctx.user_id,
                "project_id": ctx.project_id,
                "roles": ctx.roles,
                "scopes": ctx.scopes,
            }
        )
    return JSONResponse({"error": "no context"})


def _make_app(
    identity_claims: dict | None = VALID_CLAIMS,
    policy_decision: str = "ALLOW",
    mode: str = "ENFORCE",
) -> tuple[TestClient, FakeIdentityService, FakePolicyService]:
    identity = FakeIdentityService(claims=identity_claims)
    policy = FakePolicyService(decision=policy_decision)

    app = Starlette(routes=[Route("/test", echo_endpoint)])
    app.add_middleware(AuthMiddleware, identity_service=identity, policy_service=policy, mode=mode)

    client = TestClient(app, raise_server_exceptions=False)
    return client, identity, policy


# --- Tests ---


class TestMissingOrInvalidToken:
    def test_missing_authorization_header_returns_401(self):
        client, _, _ = _make_app()
        resp = client.get("/test")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["type"] == "authentication_error"
        assert "Missing" in body["error"]["message"]

    def test_authorization_header_without_bearer_prefix_returns_401(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Basic abc123"})
        assert resp.status_code == 401

    def test_empty_authorization_header_returns_401(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": ""})
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Bearer invalid"})
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["type"] == "authentication_error"
        assert "Invalid" in body["error"]["message"]


class TestValidTokenExtractsContext:
    def test_valid_token_returns_200(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert resp.status_code == 200

    def test_valid_token_extracts_user_id(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        body = resp.json()
        assert body["user_id"] == "user-123"

    def test_valid_token_extracts_project_id(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        body = resp.json()
        assert body["project_id"] == "proj-abc"

    def test_valid_token_extracts_roles(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        body = resp.json()
        assert body["roles"] == ["admin", "user"]

    def test_valid_token_extracts_scopes(self):
        client, _, _ = _make_app()
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        body = resp.json()
        assert body["scopes"] == ["read", "write"]

    def test_request_context_attached_to_request_state(self):
        global captured_context
        captured_context = None
        client, _, _ = _make_app()
        client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert captured_context is not None
        assert isinstance(captured_context, RequestContext)
        assert captured_context.user_id == "user-123"
        assert captured_context.project_id == "proj-abc"


class TestCedarPolicyEnforcement:
    def test_policy_allow_returns_200(self):
        client, _, _ = _make_app(policy_decision="ALLOW")
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert resp.status_code == 200

    def test_policy_deny_enforce_mode_returns_403(self):
        client, _, _ = _make_app(policy_decision="DENY", mode="ENFORCE")
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["type"] == "authorization_error"
        assert "denied" in body["error"]["message"].lower()

    def test_policy_deny_log_only_mode_returns_200(self):
        client, _, _ = _make_app(policy_decision="DENY", mode="LOG_ONLY")
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert resp.status_code == 200

    def test_policy_deny_log_only_still_attaches_context(self):
        global captured_context
        captured_context = None
        client, _, _ = _make_app(policy_decision="DENY", mode="LOG_ONLY")
        client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert captured_context is not None
        assert captured_context.user_id == "user-123"

    def test_policy_receives_correct_action_and_resource(self):
        client, _, policy = _make_app()
        client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert policy.last_action == "get"
        assert policy.last_resource == "/test"


class TestMissingClaims:
    def test_missing_sub_defaults_to_empty_string(self):
        claims = {"project_id": "proj-1", "roles": [], "scopes": []}
        client, _, _ = _make_app(identity_claims=claims)
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == ""

    def test_missing_roles_defaults_to_empty_list(self):
        claims = {"sub": "user-1", "project_id": "proj-1", "scopes": ["read"]}
        client, _, _ = _make_app(identity_claims=claims)
        resp = client.get("/test", headers={"Authorization": "Bearer valid-token"})
        assert resp.status_code == 200
        assert resp.json()["roles"] == []
