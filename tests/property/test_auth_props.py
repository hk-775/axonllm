# Feature: litellm-service, Property 19: JWT claim extraction
# Feature: litellm-service, Property 20: Invalid JWT returns 401
# Feature: litellm-service, Property 21: Cedar policy DENY returns 403
# Feature: litellm-service, Property 22: Policy engine mode behavior
"""Property-based tests for AuthMiddleware.

Properties covered:
  19 – JWT claim extraction produces complete RequestContext
  20 – Invalid or missing JWT returns 401
  21 – Cedar policy DENY returns 403
  22 – Policy engine mode controls enforcement behavior
"""

from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import AuthMethod, RequestContext


# ---------------------------------------------------------------------------
# Fake services
# ---------------------------------------------------------------------------


class FakeOIDCService:
    """Returns a pre-configured RequestContext for any token except 'invalid'."""

    def __init__(self, claims: dict | None = None):
        self._claims = claims

    async def validate_alb_jwt(
        self,
        token: str,
        expected_subject: str,
    ) -> RequestContext | None:
        return None

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None:
        if token == "invalid":
            return None
        if self._claims is None:
            return None
        return RequestContext(
            user_id=self._claims.get("sub", ""),
            project_id=self._claims.get("project_id", ""),
            roles=self._claims.get("roles", []),
            scopes=self._claims.get("scopes", []),
            auth_method=AuthMethod.OIDC_JWT,
        )


class FakePolicyService:
    """Returns a fixed decision for every evaluation."""

    def __init__(self, decision: str = "ALLOW"):
        self.decision = decision

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        return self.decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _echo_context(request: Request) -> JSONResponse:
    """Endpoint that returns the RequestContext attached by middleware."""
    ctx: RequestContext | None = getattr(request.state, "context", None)
    if ctx:
        return JSONResponse({
            "user_id": ctx.user_id,
            "project_id": ctx.project_id,
            "roles": ctx.roles,
            "scopes": ctx.scopes,
        })
    return JSONResponse({"error": "no context"})


def _build_client(
    claims: dict | None,
    decision: str = "ALLOW",
    mode: str = "ENFORCE",
) -> TestClient:
    oidc = FakeOIDCService(claims=claims)
    policy = FakePolicyService(decision=decision)
    app = Starlette(routes=[Route("/test", _echo_context)])
    app.add_middleware(
        AuthMiddleware,
        oidc_service=oidc,
        policy_service=policy,
        mode=mode,
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=30,
).filter(lambda s: len(s.strip()) > 0)

_role_strategy = st.lists(_safe_text, min_size=0, max_size=5)
_scope_strategy = st.lists(_safe_text, min_size=0, max_size=5)


# ===========================================================================
# Property 19: JWT claim extraction produces complete RequestContext
# Feature: litellm-service, Property 19: JWT claim extraction
# ===========================================================================


@given(
    sub=_safe_text,
    project_id=_safe_text,
    roles=_role_strategy,
    scopes=_scope_strategy,
)
@settings(max_examples=100)
def test_jwt_claim_extraction_produces_complete_context(sub, project_id, roles, scopes):
    """Property 19: JWT claim extraction produces complete RequestContext.

    For any valid JWT with sub, project_id, roles, scopes, the middleware
    extracts all four into RequestContext and returns them in the response body.

    **Validates: Requirements 1.5, 8.3**
    """
    claims = {
        "sub": sub,
        "project_id": project_id,
        "roles": roles,
        "scopes": scopes,
    }
    client = _build_client(claims=claims)
    resp = client.get("/test", headers={"Authorization": "Bearer good-token"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == sub
    assert body["project_id"] == project_id
    assert body["roles"] == roles
    assert body["scopes"] == scopes


# ===========================================================================
# Property 20: Invalid or missing JWT returns 401
# Feature: litellm-service, Property 20: Invalid JWT returns 401
# ===========================================================================


@given(data=st.data())
@settings(max_examples=100)
def test_invalid_or_missing_jwt_returns_401(data):
    """Property 20: Invalid or missing JWT returns 401.

    For any request without a JWT or with an invalid JWT, the middleware
    returns 401 Unauthorized.

    **Validates: Requirements 8.2**
    """
    # Pick one of the invalid-token scenarios
    scenario = data.draw(
        st.sampled_from(["no_header", "bearer_invalid", "non_bearer_prefix"]),
    )

    claims = {"sub": "u", "project_id": "p", "roles": [], "scopes": []}
    client = _build_client(claims=claims)

    if scenario == "no_header":
        resp = client.get("/test")
    elif scenario == "bearer_invalid":
        resp = client.get("/test", headers={"Authorization": "Bearer invalid"})
    else:
        # Use ASCII-only prefix (httpx requires ASCII header values)
        random_prefix = data.draw(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10)
        )
        resp = client.get("/test", headers={"Authorization": f"{random_prefix} some-token"})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["type"] == "authentication_error"


# ===========================================================================
# Property 21: Cedar policy DENY returns 403
# Feature: litellm-service, Property 21: Cedar policy DENY returns 403
# ===========================================================================


@given(
    sub=_safe_text,
    project_id=_safe_text,
    roles=_role_strategy,
    scopes=_scope_strategy,
)
@settings(max_examples=100)
def test_cedar_policy_deny_returns_403(sub, project_id, roles, scopes):
    """Property 21: Cedar policy DENY returns 403.

    For any request where Cedar policy returns DENY (in ENFORCE mode),
    the middleware returns 403 with a policy violation message.

    **Validates: Requirements 8.4, 8.5**
    """
    claims = {
        "sub": sub,
        "project_id": project_id,
        "roles": roles,
        "scopes": scopes,
    }
    client = _build_client(claims=claims, decision="DENY", mode="ENFORCE")
    resp = client.get("/test", headers={"Authorization": "Bearer good-token"})

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["type"] == "authorization_error"
    assert "denied" in body["error"]["message"].lower() or "policy" in body["error"]["message"].lower()


# ===========================================================================
# Property 22: Policy engine mode controls enforcement behavior
# Feature: litellm-service, Property 22: Policy engine mode behavior
# ===========================================================================


@given(
    sub=_safe_text,
    project_id=_safe_text,
    roles=_role_strategy,
    scopes=_scope_strategy,
)
@settings(max_examples=100)
def test_policy_engine_mode_controls_enforcement(sub, project_id, roles, scopes):
    """Property 22: Policy engine mode controls enforcement behavior.

    In LOG_ONLY mode, denied requests are logged but allowed through (200).
    In ENFORCE mode, denied requests are blocked with 403.

    **Validates: Requirements 8.8**
    """
    claims = {
        "sub": sub,
        "project_id": project_id,
        "roles": roles,
        "scopes": scopes,
    }

    # ENFORCE mode → 403
    enforce_client = _build_client(claims=claims, decision="DENY", mode="ENFORCE")
    enforce_resp = enforce_client.get("/test", headers={"Authorization": "Bearer good-token"})
    assert enforce_resp.status_code == 403

    # LOG_ONLY mode → 200 (allowed through)
    log_client = _build_client(claims=claims, decision="DENY", mode="LOG_ONLY")
    log_resp = log_client.get("/test", headers={"Authorization": "Bearer good-token"})
    assert log_resp.status_code == 200
    # Verify the context was still extracted even in LOG_ONLY mode
    body = log_resp.json()
    assert body["user_id"] == sub
    assert body["project_id"] == project_id
