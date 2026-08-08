"""Read-only admin scopes: `admin:<resource>:read` vs `:write`.

Admin scopes used to name a resource and nothing else, so `admin:quotas` granted
`GET /admin/quotas/{project_id}` *and* `POST /admin/quotas/{project_id}/reset`.
There was no way to say "let support look at quotas" — the only choices were
read+write or nothing.

Two properties matter more than the syntax:

**Existing keys must not change meaning.** A bare `admin:quotas` still grants
both, so no already-issued credential loses (or gains) access on deploy. The
suffix narrows; it is never required to keep what you had. `:write` implies read,
because an operator who can reset a quota can already see the value being reset
and splitting them would only produce keys that mutate blind.

**Read/write is classified by effect, not by HTTP method.** Four admin POSTs read
like inspections and mutate anyway — `quotas/simulate` consumes the project's
rate-limit budget, `regions/health/check` updates spoke status (changing where
traffic goes), `regions/route` exercises the live router, and
`webhooks/{name}/test` sends a real HTTP request to an external host. Classifying
by method would have handed a nominally read-only key the ability to exhaust a
rate limit or ping an outside endpoint. `POST /admin/pii/preview` is the one
non-GET that genuinely persists nothing, so it counts as a read.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.middleware.admin_rbac import (
    PLATFORM_RESOURCES,
    WRITE_EFFECT_PATHS,
    AdminRBACMiddleware,
    classify_access,
    parse_admin_scope,
    scope_implies,
)
from src.gateway.models import (
    AuthMethod,
    Principal,
    RequestContext,
    TenantRole,
)


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class _FakeAuth:
    """Injects a context from test headers, standing in for AuthMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        raw = headers.get("x-test-scope", "")
        scope["state"] = scope.get("state", {})
        scope["state"]["context"] = RequestContext(
            user_id="tester",
            project_id="p1",
            roles=[r for r in headers.get("x-test-role", "").split(",") if r],
            scopes=[s for s in raw.split(",") if s],
            auth_method=AuthMethod.API_KEY,
        )
        return await self.app(scope, receive, send)


ROUTES = [
    ("GET", "/admin/quotas/p1"),
    ("POST", "/admin/quotas/p1/reset"),
    ("POST", "/admin/quotas/simulate"),
    ("GET", "/admin/projects"),
    ("POST", "/admin/projects"),
    ("PUT", "/admin/projects/p1"),
    ("DELETE", "/admin/projects/p1/members/u"),
    ("GET", "/admin/regions"),
    ("POST", "/admin/regions/health/check"),
    ("POST", "/admin/regions/route"),
    ("POST", "/admin/pii/preview"),
    ("POST", "/admin/webhooks/pagerduty/test"),
]


@pytest.fixture
def client() -> TestClient:
    app = Starlette(
        routes=[Route(path, _ok, methods=[m]) for m, path in ROUTES]
    )
    app.add_middleware(AdminRBACMiddleware, mode="ENFORCE")
    app.add_middleware(_FakeAuth)
    return TestClient(app)


def _call(client, method, path, scopes="", roles=""):
    return client.request(
        method,
        path,
        headers={"x-test-scope": scopes, "x-test-role": roles},
        json={} if method in ("POST", "PUT") else None,
    )


class TestExistingScopesKeepWorking:
    """The migration property: nobody's key changes meaning on deploy."""

    def test_a_bare_resource_scope_still_grants_writes(self, client):
        assert _call(client, "POST", "/admin/quotas/p1/reset",
                     scopes="admin:quotas").status_code == 200

    def test_a_bare_resource_scope_still_grants_reads(self, client):
        assert _call(client, "GET", "/admin/quotas/p1",
                     scopes="admin:quotas").status_code == 200

    def test_admin_star_still_grants_everything(self, client):
        for method, path in ROUTES:
            resp = _call(client, method, path, scopes="admin:*")
            assert resp.status_code == 200, f"admin:* lost access to {method} {path}"

    def test_the_admin_role_still_grants_everything(self, client):
        for method, path in ROUTES:
            resp = _call(client, method, path, roles="admin")
            assert resp.status_code == 200, f"admin role lost access to {method} {path}"

    def test_a_bare_scope_does_not_leak_to_other_resources(self, client):
        assert _call(client, "GET", "/admin/projects",
                     scopes="admin:quotas").status_code == 403


class TestReadOnlyScopes:
    """The thing that was previously inexpressible."""

    def test_read_grants_the_get(self, client):
        assert _call(client, "GET", "/admin/quotas/p1",
                     scopes="admin:quotas:read").status_code == 200

    def test_read_refuses_the_reset(self, client):
        """The concrete ask: a dashboard viewer who cannot wipe a usage counter."""
        assert _call(client, "POST", "/admin/quotas/p1/reset",
                     scopes="admin:quotas:read").status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [(m, p) for m, p in ROUTES if classify_access(m, p) == "write"],
    )
    def test_a_wildcard_read_scope_refuses_every_write(self, client, method, path):
        resp = _call(client, method, path, scopes="admin:*:read")
        assert resp.status_code == 403, (
            f"admin:*:read reached {method} {path}, which mutates state"
        )

    @pytest.mark.parametrize(
        "method,path",
        [(m, p) for m, p in ROUTES if classify_access(m, p) == "read"],
    )
    def test_a_wildcard_read_scope_allows_every_read(self, client, method, path):
        resp = _call(client, method, path, scopes="admin:*:read")
        assert resp.status_code == 200, (
            f"admin:*:read was denied {method} {path}, which persists nothing"
        )

    def test_read_does_not_reach_another_resource(self, client):
        assert _call(client, "GET", "/admin/projects",
                     scopes="admin:quotas:read").status_code == 403

    @pytest.mark.parametrize("role", ["tenant_member", "tenant_auditor"])
    def test_canonical_non_admin_roles_can_read_all_tenant_config(
        self,
        client,
        role,
    ):
        for method, path in ROUTES:
            resource = path.strip("/").split("/")[1]
            if (
                classify_access(method, path) == "read"
                and resource not in PLATFORM_RESOURCES
            ):
                assert _call(
                    client,
                    method,
                    path,
                    roles=role,
                ).status_code == 200

    @pytest.mark.parametrize(
        "role",
        ["tenant_admin", "tenant_member", "tenant_auditor"],
    )
    def test_tenant_roles_cannot_read_platform_config(self, client, role):
        assert _call(
            client,
            "GET",
            "/admin/regions",
            roles=role,
        ).status_code == 403

    @pytest.mark.parametrize("role", ["tenant_member", "tenant_auditor"])
    def test_canonical_non_admin_roles_cannot_mutate(
        self,
        client,
        role,
    ):
        for method, path in ROUTES:
            if classify_access(method, path) == "write":
                assert _call(
                    client,
                    method,
                    path,
                    roles=role,
                ).status_code == 403

    def test_tenant_admin_can_write_tenant_config(self, client):
        assert _call(
            client,
            "POST",
            "/admin/projects",
            roles="tenant_admin",
        ).status_code == 200

    def test_tenant_admin_cannot_write_platform_config(self, client):
        assert _call(
            client,
            "POST",
            "/admin/regions/route",
            roles="tenant_admin",
        ).status_code == 403

    def test_service_role_has_no_admin_access(self, client):
        assert _call(
            client,
            "GET",
            "/admin/projects",
            roles="service",
        ).status_code == 403

    def test_canonical_service_cannot_elevate_with_legacy_admin_scope(self):
        middleware = AdminRBACMiddleware(app=None, mode="ENFORCE")
        context = RequestContext(
            user_id="service-principal",
            project_id="p1",
            roles=["service"],
            scopes=["admin:*"],
            auth_method=AuthMethod.API_KEY,
            tenant_id="tenant-a",
            principal_id="principal:service",
        )

        assert not middleware._is_authorized(
            context,
            "/admin/projects",
            "read",
        )

    def test_canonical_viewer_cannot_elevate_with_legacy_admin_scope(self):
        middleware = AdminRBACMiddleware(app=None, mode="ENFORCE")
        context = RequestContext(
            user_id="viewer",
            project_id="p1",
            roles=["tenant_member"],
            scopes=["admin:*"],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id="tenant-a",
            principal_id="principal:viewer",
        )

        assert not middleware._is_authorized(
            context,
            "/admin/projects",
            "write",
        )

    def test_legacy_service_keeps_admin_scope_compatibility(self):
        middleware = AdminRBACMiddleware(app=None, mode="ENFORCE")
        context = RequestContext(
            user_id="legacy-service",
            project_id="p1",
            roles=["service"],
            scopes=["admin:*"],
            auth_method=AuthMethod.API_KEY,
        )

        assert middleware._is_authorized(
            context,
            "/admin/projects",
            "write",
        )

    def test_platform_admin_needs_break_glass_for_tenant_config(self):
        middleware = AdminRBACMiddleware(app=None, mode="ENFORCE")
        principal = Principal(
            principal_id="principal:platform-admin",
            tenant_id="platform-home",
            subject="platform-admin",
            issuer="https://idp.example.test",
            roles=frozenset({TenantRole.PLATFORM_ADMIN}),
            auth_method=AuthMethod.OIDC_JWT,
        )
        context = RequestContext(
            user_id=principal.principal_id,
            project_id="",
            roles=["platform_admin"],
            scopes=[],
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            authorization_version=principal.authorization_version,
        )

        assert not middleware._is_authorized(
            context,
            "/admin/projects",
            "read",
        )
        assert middleware._is_authorized(
            context,
            "/admin/projects",
            "read",
            break_glass_reason="support incident 123",
            principal=principal,
            target_tenant_id="tenant-a",
        )

    def test_the_denial_names_the_scope_that_would_work(self, client):
        """A 403 should be actionable, not just a refusal."""
        resp = _call(client, "POST", "/admin/quotas/p1/reset",
                     scopes="admin:quotas:read")
        assert "admin:quotas:write" in resp.json()["error"]["message"]


class TestWriteImpliesRead:
    def test_write_grants_the_get_too(self, client):
        assert _call(client, "GET", "/admin/quotas/p1",
                     scopes="admin:quotas:write").status_code == 200

    def test_write_grants_writes(self, client):
        assert _call(client, "POST", "/admin/quotas/p1/reset",
                     scopes="admin:quotas:write").status_code == 200


class TestClassificationByEffectNotMethod:
    """Four POSTs are named like reads and mutate anyway."""

    @pytest.mark.parametrize(
        "path,why",
        [
            ("/admin/quotas/simulate", "consumes the project's rate-limit budget"),
            ("/admin/regions/health/check", "updates spoke status, changing routing"),
            ("/admin/regions/route", "exercises the live router"),
            ("/admin/webhooks/pagerduty/test", "sends real HTTP to an external host"),
        ],
    )
    def test_a_misleadingly_named_post_is_a_write(self, path, why):
        assert classify_access("POST", path) == "write", (
            f"{path} classified as a read, but it {why}"
        )

    def test_pii_preview_is_a_read(self):
        """The one non-GET that genuinely persists nothing."""
        assert classify_access("POST", "/admin/pii/preview") == "read"

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/quotas/simulate",
            "/admin/regions/health/check",
            "/admin/regions/route",
        ],
    )
    def test_each_write_effect_path_is_listed_explicitly(self, path):
        """Pins the *override*, not just its outcome.

        Deleting the `WRITE_EFFECT_PATHS` check entirely leaves these paths
        classified as writes anyway, because they are POSTs and the method
        fallback agrees. So an outcome assertion cannot tell the two apart — and
        the distinction matters the moment one of these routes is changed to
        `GET`, or a new read-effect exception is added above them. Asserting
        membership makes the intent, not the coincidence, the thing under test.
        """
        assert path in WRITE_EFFECT_PATHS

    def test_a_write_effect_path_stays_a_write_even_as_a_get(self):
        """The by-effect rule must beat the method, not merely agree with it."""
        assert classify_access("GET", "/admin/quotas/simulate") == "write"

    def test_the_webhook_test_override_beats_the_method_too(self):
        assert classify_access("GET", "/admin/webhooks/pagerduty/test") == "write"

    def test_the_override_does_not_catch_the_whole_webhooks_resource(self):
        """`endswith("/test")`, not "anything under /admin/webhooks/"."""
        assert classify_access("GET", "/admin/webhooks") == "read"
        assert classify_access("GET", "/admin/webhooks/stats") == "read"

    def test_a_read_scope_cannot_consume_the_rate_limit_budget(self, client):
        """Named separately because this is the security consequence.

        `quotas/simulate` calls `enforce_all`, whose `check_rate_limit` appends a
        timestamp to the project's window — so repeated 'simulation' from a
        read-only credential can exhaust a real rate limit.
        """
        assert _call(client, "POST", "/admin/quotas/simulate",
                     scopes="admin:quotas:read").status_code == 403

    def test_a_read_scope_cannot_reach_an_external_webhook(self, client):
        assert _call(client, "POST", "/admin/webhooks/pagerduty/test",
                     scopes="admin:webhooks:read").status_code == 403

    def test_a_read_scope_can_still_preview_redaction(self, client):
        assert _call(client, "POST", "/admin/pii/preview",
                     scopes="admin:pii:read").status_code == 200

    def test_get_is_a_read_and_other_methods_are_writes_by_default(self):
        assert classify_access("GET", "/admin/projects") == "read"
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert classify_access(method, "/admin/projects") == "write"


class TestMalformedScopesFailClosed:
    """An unrecognised suffix must not be read as an access level."""

    @pytest.mark.parametrize("scope", ["admin:quotas:raed", "admin:quotas:READ",
                                       "admin:quotas:admin", "admin:quotas:"])
    def test_a_typo_grants_nothing(self, client, scope):
        resp = _call(client, "GET", "/admin/quotas/p1", scopes=scope)
        assert resp.status_code == 403, (
            f"{scope} granted access; a mistyped suffix must not fall back to a "
            "resource-wide grant"
        )

    def test_a_non_admin_scope_is_ignored(self, client):
        assert _call(client, "GET", "/admin/quotas/p1",
                     scopes="chat").status_code == 403

    def test_parse_treats_an_unknown_suffix_as_part_of_the_resource(self):
        assert parse_admin_scope("admin:quotas:raed") == ("quotas:raed", "write")

    def test_parse_defaults_a_bare_scope_to_write(self):
        assert parse_admin_scope("admin:quotas") == ("quotas", "write")

    def test_parse_reads_the_known_suffixes(self):
        assert parse_admin_scope("admin:quotas:read") == ("quotas", "read")
        assert parse_admin_scope("admin:quotas:write") == ("quotas", "write")
        assert parse_admin_scope("admin:*:read") == ("*", "read")

    def test_is_authorized_defaults_to_the_stricter_check(self):
        """A caller that omits `access` should get the write check, not the read
        one — the default must not be the permissive direction."""
        mw = AdminRBACMiddleware(app=None, mode="ENFORCE")
        ctx = RequestContext(
            user_id="u", project_id="p1", roles=[],
            scopes=["admin:quotas:read"], auth_method=AuthMethod.API_KEY,
        )
        assert mw._is_authorized(ctx, "/admin/quotas/p1") is False


class TestDelegatingANarrowerScope:
    """`scope_implies` is what lets the key-issuance guard hand out a subset."""

    def test_read_write_implies_read_only(self):
        assert scope_implies("admin:projects", "admin:projects:read")
        assert scope_implies("admin:projects:write", "admin:projects:read")

    def test_read_only_does_not_imply_write(self):
        assert not scope_implies("admin:projects:read", "admin:projects")
        assert not scope_implies("admin:projects:read", "admin:projects:write")

    def test_the_wildcard_implies_any_resource(self):
        assert scope_implies("admin:*", "admin:quotas:write")
        assert scope_implies("admin:*:read", "admin:quotas:read")

    def test_a_read_wildcard_does_not_imply_a_write(self):
        assert not scope_implies("admin:*:read", "admin:quotas:write")
        assert not scope_implies("admin:*:read", "admin:quotas")

    def test_one_resource_does_not_imply_another(self):
        assert not scope_implies("admin:quotas", "admin:projects")
        assert not scope_implies("admin:quotas:write", "admin:projects:read")

    def test_a_narrow_resource_does_not_imply_the_wildcard(self):
        assert not scope_implies("admin:quotas", "admin:*")
        assert not scope_implies("admin:quotas:write", "admin:*:read")
