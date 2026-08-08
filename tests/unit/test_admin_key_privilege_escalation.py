"""A narrow admin scope must not be a path to a wider one.

`AdminRBACMiddleware` authorizes on the first path segment (`_extract_resource`
takes `parts[1]`), which is the right granularity for most of the admin API and
the wrong granularity for exactly one part of it: the routes that mint and
rotate credentials. `admin:projects` reaches `POST /admin/projects/{id}/keys`;
`admin:keys` reaches `POST /admin/keys/{key_id}/rotate`. Before the checks in
`key_routes.py`, both were full privilege escalations, confirmed against the real
app rather than inferred:

    POST /admin/projects/esc/keys  {"scopes": ["admin:*"]}  -> 201, raw key returned
    POST /admin/keys/{victim}/rotate                        -> 201, scopes ['admin:*']

The second is the nastier one, because it needs no cooperation from the victim
and works *within a single project*: `APIKeyService.rotate_key` copies the old
key's scopes onto the replacement and the handler returns that replacement's raw
value. So "rotate a key" and "be handed that key" are the same operation.

These tests drive the real `KeyManagementAPI` routes behind the real
`AuthMiddleware` -> `AdminRBACMiddleware` pair. That matters: the existing
`test_admin_rbac.py` wires a fake auth middleware around three mock routes, which
is why it passed throughout while the escalation was live. A regression here has
to be caught end-to-end or it is not caught at all.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src.gateway.admin.key_routes import KeyManagementAPI, create_key_routes
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.middleware.admin_rbac import AdminRBACMiddleware
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import RequestContext
from src.gateway.persistence import DynamoPersistence


@pytest.fixture
def service(monkeypatch) -> APIKeyService:
    """A key service on the in-process path (persistence disabled)."""
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    persistence = DynamoPersistence()
    assert not persistence.enabled, "these tests need the in-memory key path"
    return APIKeyService(persistence=persistence)


def _client(service: APIKeyService, mode: str = "ENFORCE") -> TestClient:
    """The real key routes behind the real middleware pair."""
    app = Starlette(routes=create_key_routes(KeyManagementAPI(service, mode=mode)))
    # add_middleware prepends, so AuthMiddleware runs first and AdminRBAC sees
    # the context it produced — the same order bootstrap.py builds.
    app.add_middleware(AdminRBACMiddleware, mode=mode)
    app.add_middleware(AuthMiddleware, api_key_service=service, mode=mode)
    return TestClient(app)


async def _issue(service, scopes, project_id="p1", name="k"):
    return await service.issue_key(
        project_id=project_id, name=name, scopes=scopes, created_by="test"
    )


def _tenant_route_client(
    service: APIKeyService,
    tenant_id: str | None,
    *,
    project_id: str = "shared-project",
) -> TestClient:
    async def add_context(request, call_next):
        request.state.context = RequestContext(
            user_id="tenant-admin",
            project_id=project_id,
            roles=["tenant_admin"],
            scopes=["admin:*"],
            tenant_id=tenant_id,
            principal_id="principal:tenant-admin",
        )
        return await call_next(request)

    app = Starlette(
        routes=create_key_routes(KeyManagementAPI(service, mode="ENFORCE"))
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=add_context)
    return TestClient(app)


class TestMintingAWiderKey:
    """`admin:projects` reaches the issue route; it must not grant admin scopes."""

    async def test_a_narrow_admin_scope_cannot_issue_an_admin_star_key(self, service):
        _, raw = await _issue(service, ["admin:projects"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "escalated", "scopes": ["admin:*"]},
        )

        assert resp.status_code == 403, (
            "an admin:projects holder minted an admin:* key — RBAC authorizes "
            f"this route on the path segment alone, so it got {resp.status_code}"
        )

    async def test_it_cannot_grant_admin_scopes_it_does_not_hold(self, service):
        _, raw = await _issue(service, ["admin:quotas"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "sideways", "scopes": ["admin:audit"]},
        )

        assert resp.status_code == 403

    async def test_it_may_reissue_the_scope_it_already_holds(self, service):
        """The rule is no *escalation*, not no delegation.

        A caller holding a scope can hand out that same scope; it confers nothing
        the caller could not already do.
        """
        _, raw = await _issue(service, ["admin:projects"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "peer", "scopes": ["admin:projects"]},
        )

        assert resp.status_code == 201
        assert resp.json()["scopes"] == ["admin:projects"]

    async def test_non_admin_scopes_are_freely_grantable(self, service):
        """Ordinary gateway access is not the thing being protected."""
        _, raw = await _issue(service, ["admin:projects"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "app", "scopes": ["chat"]},
        )

        assert resp.status_code == 201

    async def test_a_superadmin_is_unaffected(self, service):
        """The check must not break the operator it is protecting."""
        _, raw = await _issue(service, ["admin:*"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "delegate", "scopes": ["admin:*"]},
        )

        assert resp.status_code == 201
        assert resp.json()["scopes"] == ["admin:*"]


class TestRotatingSomeoneElsesKey:
    """Rotation returns a raw key carrying the *target's* scopes."""

    async def test_admin_keys_cannot_rotate_a_superadmin_key(self, service):
        victim, _ = await _issue(service, ["admin:*"], name="victim")
        _, attacker = await _issue(service, ["admin:keys"], name="attacker")
        client = _client(service)

        resp = client.post(
            f"/admin/keys/{victim.key_id}/rotate",
            headers={"X-Api-Key": attacker},
            json={"rotated_by": "attacker"},
        )

        assert resp.status_code == 403, (
            "rotation handed back a key with the victim's scopes; the response "
            "includes the raw value, so this is equivalent to stealing it"
        )

    async def test_the_victims_key_still_works_after_a_refused_rotation(self, service):
        """A refused rotation must not revoke the target as a side effect.

        `rotate_key` revokes before re-issuing, so a check placed after the call
        would deny the response while still having destroyed the credential —
        turning a blocked escalation into a denial of service.
        """
        victim, victim_raw = await _issue(service, ["admin:*"], name="victim")
        _, attacker = await _issue(service, ["admin:keys"], name="attacker")
        client = _client(service)

        client.post(
            f"/admin/keys/{victim.key_id}/rotate", headers={"X-Api-Key": attacker}
        )

        still_valid = await service.validate_key(victim_raw)
        assert still_valid is not None and not still_valid.revoked

    async def test_a_holder_may_rotate_its_own_equally_scoped_key(self, service):
        """Self-service rotation is the documented way to replace a lost key."""
        own, _ = await _issue(service, ["admin:keys"], name="own")
        _, raw = await _issue(service, ["admin:keys"], name="caller")
        client = _client(service)

        resp = client.post(f"/admin/keys/{own.key_id}/rotate", headers={"X-Api-Key": raw})

        assert resp.status_code == 201
        assert resp.json()["scopes"] == ["admin:keys"]

    async def test_a_superadmin_may_still_rotate_any_key(self, service):
        victim, _ = await _issue(service, ["admin:quotas"], name="victim")
        _, raw = await _issue(service, ["admin:*"], name="root")
        client = _client(service)

        resp = client.post(
            f"/admin/keys/{victim.key_id}/rotate", headers={"X-Api-Key": raw}
        )

        assert resp.status_code == 201


class TestDelegatingANarrowerScope:
    """A caller may hand out a subset of its own authority, and nothing more.

    Exact string comparison would have refused even this, which is the one
    delegation that cannot escalate anything: `admin:projects` giving out
    `admin:projects:read`.
    """

    async def test_a_read_write_holder_may_issue_the_read_only_version(self, service):
        _, raw = await _issue(service, ["admin:projects"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "viewer", "scopes": ["admin:projects:read"]},
        )

        assert resp.status_code == 201
        assert resp.json()["scopes"] == ["admin:projects:read"]

    async def test_it_still_cannot_reach_the_wildcard(self, service):
        _, raw = await _issue(service, ["admin:projects"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "root", "scopes": ["admin:*"]},
        )

        assert resp.status_code == 403

    async def test_it_cannot_grant_a_sideways_resource(self, service):
        _, raw = await _issue(service, ["admin:projects"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "quotas", "scopes": ["admin:quotas:read"]},
        )

        assert resp.status_code == 403

    async def test_a_read_only_scope_cannot_issue_keys_at_all(self, service):
        """Not the delegation rules — RBAC refuses this one layer earlier.

        Issuing a key is a *write* to `projects`, so `admin:projects:read` never
        reaches the handler. Worth pinning because it is the natural question
        ("can a read-only key mint another read-only key?") and the answer comes
        from a different layer than the rest of this file.
        """
        _, raw = await _issue(service, ["admin:projects:read"])
        client = _client(service)

        resp = client.post(
            "/admin/projects/p1/keys",
            headers={"X-Api-Key": raw},
            json={"name": "peer", "scopes": ["admin:projects:read"]},
        )

        assert resp.status_code == 403

    async def test_but_a_read_only_scope_can_list_keys(self, service):
        """The contrast: reading the key list is exactly what :read is for."""
        _, raw = await _issue(service, ["admin:projects:read"])
        client = _client(service)

        assert client.get(
            "/admin/projects/p1/keys", headers={"X-Api-Key": raw}
        ).status_code == 200


class TestCrossProjectAccess:
    """A project-scoped credential should stay inside its project."""

    async def test_it_cannot_list_another_projects_keys(self, service):
        await _issue(service, ["admin:*"], project_id="p2", name="p2-key")
        _, raw = await _issue(service, ["admin:projects"], project_id="p1")
        client = _client(service)

        resp = client.get("/admin/projects/p2/keys", headers={"X-Api-Key": raw})

        assert resp.status_code == 403, (
            "listing leaked another project's key ids and metadata"
        )

    async def test_it_cannot_issue_keys_for_another_project(self, service):
        _, raw = await _issue(service, ["admin:projects"], project_id="p1")
        client = _client(service)

        resp = client.post(
            "/admin/projects/p2/keys",
            headers={"X-Api-Key": raw},
            json={"name": "foreign", "scopes": ["chat"]},
        )

        assert resp.status_code == 403

    async def test_it_cannot_revoke_another_projects_key(self, service):
        victim, victim_raw = await _issue(service, ["chat"], project_id="p2")
        _, raw = await _issue(service, ["admin:keys"], project_id="p1")
        client = _client(service)

        resp = client.delete(
            f"/admin/keys/{victim.key_id}", headers={"X-Api-Key": raw}
        )

        assert resp.status_code == 403
        survivor = await service.validate_key(victim_raw)
        assert survivor is not None and not survivor.revoked

    async def test_it_can_still_operate_within_its_own_project(self, service):
        _, raw = await _issue(service, ["admin:projects"], project_id="p1")
        client = _client(service)

        resp = client.get("/admin/projects/p1/keys", headers={"X-Api-Key": raw})

        assert resp.status_code == 200


class TestTenantKeyIsolation:
    async def test_same_project_lists_only_authenticated_tenant(self, service):
        await service.issue_key(
            "shared-project",
            "tenant-a-key",
            ["chat"],
            "test",
            tenant_id="tenant-a",
        )
        await service.issue_key(
            "shared-project",
            "tenant-b-key",
            ["chat"],
            "test",
            tenant_id="tenant-b",
        )

        tenant_a = _tenant_route_client(service, "tenant-a")
        tenant_b = _tenant_route_client(service, "tenant-b")

        assert [key["name"] for key in tenant_a.get(
            "/admin/projects/shared-project/keys"
        ).json()] == ["tenant-a-key"]
        assert [key["name"] for key in tenant_b.get(
            "/admin/projects/shared-project/keys"
        ).json()] == ["tenant-b-key"]

    async def test_tenant_admin_manages_keys_across_own_tenant_projects(
        self,
        service,
    ):
        existing, _ = await service.issue_key(
            "project-b",
            "existing",
            ["inference.invoke"],
            "test",
            tenant_id="tenant-a",
        )
        tenant_admin = _tenant_route_client(
            service,
            "tenant-a",
            project_id="project-a",
        )

        listed = tenant_admin.get("/admin/projects/project-b/keys")
        issued = tenant_admin.post(
            "/admin/projects/project-b/keys",
            json={
                "name": "new-key",
                "scopes": ["inference.invoke"],
            },
        )
        revoked = tenant_admin.delete(f"/admin/keys/{existing.key_id}")

        assert listed.status_code == 200
        assert [key["name"] for key in listed.json()] == ["existing"]
        assert issued.status_code == 201
        assert issued.json()["project_id"] == "project-b"
        assert revoked.status_code == 200

    def test_canonical_key_cannot_receive_legacy_admin_scope(self, service):
        tenant_admin = _tenant_route_client(service, "tenant-a")

        response = tenant_admin.post(
            "/admin/projects/shared-project/keys",
            json={"name": "legacy-admin", "scopes": ["admin:*"]},
        )

        assert response.status_code == 403
        assert "cannot carry legacy admin scopes" in response.json()["error"]["message"]

    async def test_tenant_cannot_revoke_another_tenants_key(self, service):
        victim, victim_raw = await service.issue_key(
            "shared-project",
            "victim",
            ["chat"],
            "test",
            tenant_id="tenant-b",
        )
        tenant_a = _tenant_route_client(service, "tenant-a")

        response = tenant_a.delete(f"/admin/keys/{victim.key_id}")

        assert response.status_code == 404
        assert await service.validate_key(victim_raw) is not None

    async def test_tenant_cannot_rotate_another_tenants_key(self, service):
        victim, victim_raw = await service.issue_key(
            "shared-project",
            "victim",
            ["chat"],
            "test",
            tenant_id="tenant-b",
        )
        tenant_a = _tenant_route_client(service, "tenant-a")

        response = tenant_a.post(f"/admin/keys/{victim.key_id}/rotate")

        assert response.status_code == 404
        assert await service.validate_key(victim_raw) is not None

    def test_canonical_role_without_tenant_fails_closed(self, service):
        client = _tenant_route_client(service, None)

        response = client.get("/admin/projects/shared-project/keys")

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_tenant_scope"

    async def test_direct_stub_without_state_keeps_legacy_scope(self, service):
        await _issue(service, ["chat"], project_id="p1", name="legacy")

        class RequestStub:
            path_params = {"id": "p1"}

        response = await KeyManagementAPI(service).list_keys(RequestStub())

        assert response.status_code == 200
        assert b'"name":"legacy"' in response.body


class TestLogOnlyDoesNotLockOutLocalDevelopment:
    """LOG_ONLY has no authenticated context at all; failing closed is unusable.

    The mode exists so an operator can reach the admin API *before* they have a
    credential for it. If these checks enforced there, issuing the first key
    would require already holding one.
    """

    async def test_an_unauthenticated_caller_may_issue_in_log_only(self, service):
        client = _client(service, mode="LOG_ONLY")

        resp = client.post(
            "/admin/projects/p1/keys", json={"name": "bootstrap", "scopes": ["admin:*"]}
        )

        assert resp.status_code == 201

    async def test_enforce_denies_the_same_request(self, service):
        """The contrast is the point: the mode is what changes, not the request."""
        client = _client(service, mode="ENFORCE")

        resp = client.post(
            "/admin/projects/p1/keys", json={"name": "bootstrap", "scopes": ["admin:*"]}
        )

        assert resp.status_code == 401  # AuthMiddleware rejects before RBAC

    def test_the_default_mode_fails_closed(self, service):
        """A caller that forgets to pass `mode` must get enforcement.

        `bootstrap.py` always passes `app_config.auth_mode`, so this default is
        only reachable by direct construction — which is exactly why it needs
        pinning: a permissive default is invisible until something constructs the
        API without a mode and silently stops enforcing.
        """
        assert KeyManagementAPI(service).mode == "ENFORCE"
        assert AdminRBACMiddleware(app=None).mode == "ENFORCE", (
            "the two admin authorization layers should agree on their default"
        )
