"""What a real issued API key can and cannot reach under ENFORCE.

`test_admin_rbac.py` asserts the RBAC matrix against a synthetic
`RequestContext`, which proves the middleware's logic but not the value the
middleware is actually handed. The gap between the two is where the surprise
lives: `axon issue-key` defaults `--scopes` to `chat`, and `AuthMiddleware` gives
every API key `roles=["service"]` — so a key that authenticates perfectly well is
rejected by every `/admin/*` path, and an operator who has just switched to
`ENFORCE` is locked out of the admin API by their own credential.

These tests run a key through the real `APIKeyService` and the real
`AuthMiddleware` → `AdminRBACMiddleware` pair, so the CLI's default and the
middleware's role assignment are both in the path. The README documents the
resulting matrix and the "issue an `admin:*` key *before* enabling ENFORCE"
ordering that follows from it; this is what catches a change at either end making
that instruction wrong.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.persistence import DynamoPersistence

CLI_DEFAULT_SCOPES = ["chat"]
"""Mirrors `cli.py`'s `--scopes` default, pinned by a test below."""


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _client(api_key_service: APIKeyService) -> TestClient:
    app = Starlette(
        routes=[
            Route("/admin/projects", _ok, methods=["GET"]),
            Route("/admin/quotas/proj:test", _ok, methods=["GET"]),
            Route("/api/chat", _ok, methods=["POST"]),
            Route("/health", _ok, methods=["GET"]),
        ]
    )
    # add_middleware prepends, so this order runs AuthMiddleware first and lets
    # AdminRBACMiddleware see the context it produced.
    app.add_middleware(AdminRBACMiddleware, mode="ENFORCE")
    app.add_middleware(AuthMiddleware, api_key_service=api_key_service, mode="ENFORCE")
    return TestClient(app)


@pytest.fixture
def service(monkeypatch) -> APIKeyService:
    """A key service with persistence off — the local clean-install case.

    The real `DynamoPersistence` rather than a fake, because its writes are
    no-ops when disabled and `APIKeyService` falls back to its in-process store.
    That is exactly the configuration a first-time operator runs under, and it
    keeps this test from asserting against a storage reimplementation.
    """
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    persistence = DynamoPersistence()
    assert not persistence.enabled, "these tests need the in-memory key path"
    return APIKeyService(persistence=persistence)


async def _issue(service: APIKeyService, scopes: list[str]) -> str:
    _, raw = await service.issue_key(
        project_id="proj:test", name="test-key", scopes=scopes, created_by="test"
    )
    return raw


class TestTheCLIDefaultCannotAdminister:
    """The failure mode the README's step ordering exists to prevent."""

    async def test_a_default_scoped_key_is_denied_every_admin_path(self, service):
        raw = await _issue(service, CLI_DEFAULT_SCOPES)
        client = _client(service)

        for path in ("/admin/projects", "/admin/quotas/proj:test"):
            resp = client.get(path, headers={"X-Api-Key": raw})
            assert resp.status_code == 403, (
                f"{path} allowed a key with scopes={CLI_DEFAULT_SCOPES}; the README "
                "tells operators to issue an admin:* key before enabling ENFORCE "
                "precisely because this is a 403"
            )

    async def test_the_same_key_can_still_chat(self, service):
        """The denial is about scope, not the key being invalid.

        Worth separating when debugging: 403 on /admin with 200 on /api/chat
        means "wrong scopes", while 401 on both means "wrong key".
        """
        raw = await _issue(service, CLI_DEFAULT_SCOPES)
        assert _client(service).post("/api/chat", headers={"X-Api-Key": raw}).status_code == 200

    async def test_an_admin_wildcard_key_reaches_admin(self, service):
        raw = await _issue(service, ["admin:*"])
        client = _client(service)
        assert client.get("/admin/projects", headers={"X-Api-Key": raw}).status_code == 200
        assert client.get("/admin/quotas/proj:test", headers={"X-Api-Key": raw}).status_code == 200

    async def test_a_resource_scoped_key_reaches_only_that_resource(self, service):
        """`admin:quotas` matches one path segment; it is not a prefix of /admin."""
        raw = await _issue(service, ["admin:quotas"])
        client = _client(service)
        assert client.get("/admin/quotas/proj:test", headers={"X-Api-Key": raw}).status_code == 200
        assert client.get("/admin/projects", headers={"X-Api-Key": raw}).status_code == 403

    async def test_both_headers_authenticate_the_same_key(self, service):
        """The README offers X-Api-Key and Authorization: Bearer interchangeably."""
        raw = await _issue(service, ["admin:*"])
        client = _client(service)
        assert client.get("/admin/projects", headers={"X-Api-Key": raw}).status_code == 200
        assert (
            client.get("/admin/projects", headers={"Authorization": f"Bearer {raw}"}).status_code
            == 200
        )


class TestTheDocumentedDefaultIsTheRealDefault:
    def test_the_cli_still_defaults_to_chat(self):
        """Pins the value the tests above and the README are written against.

        Read out of `cli.py` rather than duplicated as a string, so widening the
        CLI default to include an admin scope fails here — instead of quietly
        making the README's "issue an admin key first" ordering unnecessary and
        its allow/deny matrix wrong. Parsed as source because the parser is built
        inside `main()`, and importing that to read one default would run the CLI.
        """
        import ast
        import pathlib

        cli = pathlib.Path(__file__).resolve().parents[2] / "src" / "gateway" / "cli.py"
        tree = ast.parse(cli.read_text(encoding="utf-8"))

        defaults = [
            keyword.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(
                isinstance(arg, ast.Constant) and arg.value == "--scopes" for arg in node.args
            )
            for keyword in node.keywords
            if keyword.arg == "default" and isinstance(keyword.value, ast.Constant)
        ]
        assert defaults == ["chat"], f"expected one --scopes default of 'chat', got {defaults}"


class TestUnauthenticatedRequests:
    def test_no_credential_is_401_not_403(self, service):
        """Authentication fails before authorization runs, so the status code says
        which layer rejected you — the distinction the README's auth diagram draws."""
        client = _client(service)
        assert client.get("/admin/projects").status_code == 401
        assert client.post("/api/chat").status_code == 401

    def test_health_stays_public(self, service):
        assert _client(service).get("/health").status_code == 200
