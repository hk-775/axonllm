"""Unit tests for AuthMiddleware (legacy contract).

These tests verify the new multi-strategy middleware still supports the
original bearer-token-only pattern via the OIDC service path.
"""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import AuthMethod, RequestContext


# --- Fake services for testing ---


class FakeOIDCService:
    """Validates any Bearer token that isn't 'invalid'."""

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
) -> tuple[TestClient, FakeOIDCService, FakePolicyService]:
    oidc = FakeOIDCService(claims=identity_claims)
    policy = FakePolicyService(decision=policy_decision)

    app = Starlette(routes=[Route("/test", echo_endpoint)])
    app.add_middleware(
        AuthMiddleware,
        oidc_service=oidc,
        policy_service=policy,
        mode=mode,
    )

    client = TestClient(app, raise_server_exceptions=False)
    return client, oidc, policy


# --- Tests ---


class TestMissingOrInvalidToken:
    def test_missing_authorization_header_returns_401(self):
        client, _, _ = _make_app()
        resp = client.get("/test")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["type"] == "authentication_error"

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


class TestSiteAssetsAreAnonymous:
    """The marketing site's pages must load without a session.

    "/" is already public — gating the page it links to, or the SVGs that page
    fetches, would serve the pitch to an anonymous reader and then 401 the
    architecture diagram behind it. Under ENFORCE that failure is invisible
    server-side: the HTML arrives 200 and the diagrams silently don't.
    """

    def _app(self):
        app = Starlette(
            routes=[
                Route("/{path}", echo_endpoint),
                Route("/{directory}/{path}", echo_endpoint),
                Route("/admin/projects", echo_endpoint),
            ]
        )
        app.add_middleware(AuthMiddleware, oidc_service=FakeOIDCService(), mode="ENFORCE")
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        "path",
        [
            "/architecture.html",
            "/architecture-pipeline.svg",
            "/architecture.drawio",
            # The narrated walkthrough. An <audio> src that 401s fails the same
            # invisible way the SVGs would: the page renders, the play button
            # appears, and nothing comes out of it.
            "/narration/architecture-narration.json",
            "/narration/pipeline.mp3",
        ],
    )
    def test_the_pages_and_their_assets_need_no_token(self, path):
        r = self._app().get(path)
        assert r.status_code == 200, f"{path} requires auth"
        assert r.json()["user_id"] == "anonymous"

    def test_the_exemption_does_not_reach_the_api(self):
        """Shape-matched, so check it stays narrow.

        Only single-segment paths ending in a suffix the site handler serves. An
        API path is neither, and must still demand a token.
        """
        client = self._app()
        assert client.get("/admin/projects").status_code == 401

    @pytest.mark.parametrize(
        "path",
        [
            "/config.yaml",              # not a suffix the site serves
            "/infra/stack.py",           # nested, and not a served suffix
            "/deep/page.html",           # right suffix, wrong directory
            "/infra/cdk.json",           # served suffix, but not a public dir
            "/narration/deep/x.mp3",     # inside the public dir, too deep
        ],
    )
    def test_paths_outside_the_shape_still_require_auth(self, path):
        assert self._app().get(path).status_code == 401

    def test_the_shape_tracks_what_the_handler_serves(self):
        """One source of truth for "which paths are public".

        The middleware calls the handler's own ``_is_servable_site_path`` rather
        than repeating the rule, so adding a suffix or a directory to the
        handler cannot leave it publicly routable but 401-ing. This asserts the
        coupling is real, not incidental.
        """
        from src.gateway.admin.routes import SITE_ASSET_DIRS, SITE_ASSET_TYPES
        from src.gateway.middleware.auth import _is_site_asset

        for suffix in SITE_ASSET_TYPES:
            assert _is_site_asset("/page" + suffix), suffix
            for directory in SITE_ASSET_DIRS:
                assert _is_site_asset(f"/{directory}/page{suffix}"), (
                    f"{directory}/*{suffix}"
                )
        assert not _is_site_asset("/page.py")
        assert not _is_site_asset("/page.yaml")
        # Not a path at all — the middleware is handed request.url.path, but a
        # relative string must not fall through to a public verdict.
        assert not _is_site_asset("page.html")
