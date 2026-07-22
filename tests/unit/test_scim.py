"""Tests for SCIM 2.0 provisioning (#14): store lifecycle + REST endpoints."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.auth.scim_routes import ScimAPI, create_scim_routes
from src.gateway.auth.scim_service import (
    ScimConflictError,
    ScimNotFoundError,
    ScimStore,
)
from src.gateway.models import ScimGroup, ScimUser

TOKEN = "scim-secret-token"


@pytest.fixture(autouse=True)
def _scim_token(monkeypatch):
    monkeypatch.setenv("AXON_SCIM_TOKEN", TOKEN)


@pytest.fixture
def client():
    store = ScimStore()
    app = Starlette(routes=create_scim_routes(ScimAPI(store)))
    return TestClient(app), store


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# --- store unit tests ---


class TestScimStore:
    async def test_create_and_lookup(self):
        store = ScimStore()
        u = await store.create_user(ScimUser(id="", user_name="alice@x.com"))
        assert u.id.startswith("scu_")
        assert store.get_user_by_username("ALICE@x.com").id == u.id

    async def test_duplicate_username_conflicts(self):
        store = ScimStore()
        await store.create_user(ScimUser(id="", user_name="a@x.com"))
        with pytest.raises(ScimConflictError):
            await store.create_user(ScimUser(id="", user_name="a@x.com"))

    async def test_deactivate(self):
        store = ScimStore()
        u = await store.create_user(ScimUser(id="", user_name="a@x.com"))
        await store.set_user_active(u.id, False)
        assert store.get_user(u.id).active is False

    async def test_roles_from_groups(self):
        store = ScimStore()
        g = await store.create_group(ScimGroup(id="", display_name="eng", roles=["developer"]))
        u = await store.create_user(ScimUser(id="", user_name="a@x.com",
                                             roles=["viewer"], groups=[g.id]))
        assert store.roles_for_user(u) == ["developer", "viewer"]

    async def test_delete_missing_raises(self):
        store = ScimStore()
        with pytest.raises(ScimNotFoundError):
            await store.delete_user("nope")


# --- persistence rehydration ---


class FakePersistence:
    enabled = True

    def __init__(self):
        self.users, self.groups = {}, {}

    async def save_scim_user(self, u):
        self.users[u.id] = u

    async def save_scim_group(self, g):
        self.groups[g.id] = g

    async def delete_scim_user(self, uid):
        self.users.pop(uid, None)

    async def delete_scim_group(self, gid):
        self.groups.pop(gid, None)

    async def load_scim_users(self):
        return list(self.users.values())

    async def load_scim_groups(self):
        return list(self.groups.values())


class TestScimPersistence:
    async def test_survives_restart(self):
        p = FakePersistence()
        s1 = ScimStore(persistence=p)
        u = await s1.create_user(ScimUser(id="", user_name="a@x.com"))
        # Fresh store on the same durable backend rehydrates.
        s2 = ScimStore(persistence=p)
        await s2.initialize()
        assert s2.get_user(u.id) is not None
        assert s2.get_user_by_username("a@x.com").id == u.id


# --- REST endpoints ---


class TestScimEndpoints:
    def test_requires_token(self, client):
        c, _ = client
        r = c.get("/scim/v2/Users")           # no auth header
        assert r.status_code == 401

    def test_disabled_without_env_token(self, client, monkeypatch):
        monkeypatch.delenv("AXON_SCIM_TOKEN", raising=False)
        c, _ = client
        r = c.get("/scim/v2/Users", headers=_auth())
        assert r.status_code == 503

    def test_create_get_list_user(self, client):
        c, _ = client
        r = c.post("/scim/v2/Users", headers=_auth(), json={
            "userName": "alice@x.com", "displayName": "Alice",
            "emails": [{"value": "alice@x.com", "primary": True}]})
        assert r.status_code == 201
        uid = r.json()["id"]

        assert c.get(f"/scim/v2/Users/{uid}", headers=_auth()).json()["userName"] == "alice@x.com"

        lst = c.get("/scim/v2/Users", headers=_auth()).json()
        assert lst["totalResults"] == 1
        assert lst["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]

    def test_filter_by_username(self, client):
        c, _ = client
        c.post("/scim/v2/Users", headers=_auth(), json={"userName": "a@x.com"})
        c.post("/scim/v2/Users", headers=_auth(), json={"userName": "b@x.com"})
        r = c.get('/scim/v2/Users?filter=userName eq "a@x.com"', headers=_auth()).json()
        assert r["totalResults"] == 1
        assert r["Resources"][0]["userName"] == "a@x.com"

    def test_duplicate_returns_409(self, client):
        c, _ = client
        c.post("/scim/v2/Users", headers=_auth(), json={"userName": "a@x.com"})
        r = c.post("/scim/v2/Users", headers=_auth(), json={"userName": "a@x.com"})
        assert r.status_code == 409
        assert r.json()["scimType"] == "uniqueness"

    def test_patch_deprovision(self, client):
        c, store = client
        uid = c.post("/scim/v2/Users", headers=_auth(),
                     json={"userName": "a@x.com"}).json()["id"]
        r = c.patch(f"/scim/v2/Users/{uid}", headers=_auth(), json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}]})
        assert r.status_code == 200
        assert r.json()["active"] is False
        assert store.get_user(uid).active is False

    def test_patch_deprovision_value_object_form(self, client):
        # Okta sends {"op":"replace","value":{"active":false}} (no path).
        c, store = client
        uid = c.post("/scim/v2/Users", headers=_auth(),
                     json={"userName": "a@x.com"}).json()["id"]
        r = c.patch(f"/scim/v2/Users/{uid}", headers=_auth(), json={
            "Operations": [{"op": "replace", "value": {"active": False}}]})
        assert r.status_code == 200 and store.get_user(uid).active is False

    def test_delete_user(self, client):
        c, store = client
        uid = c.post("/scim/v2/Users", headers=_auth(),
                     json={"userName": "a@x.com"}).json()["id"]
        assert c.delete(f"/scim/v2/Users/{uid}", headers=_auth()).status_code == 204
        assert store.get_user(uid) is None

    def test_group_crud_and_roles(self, client):
        c, _ = client
        gid = c.post("/scim/v2/Groups", headers=_auth(), json={
            "displayName": "engineers", "roles": [{"value": "developer"}]}).json()["id"]
        # user in the group inherits the role in its SCIM representation
        uid = c.post("/scim/v2/Users", headers=_auth(), json={
            "userName": "a@x.com", "groups": [{"value": gid}]}).json()["id"]
        roles = [r["value"] for r in c.get(
            f"/scim/v2/Users/{uid}", headers=_auth()).json()["roles"]]
        assert "developer" in roles

    def test_get_missing_user_404(self, client):
        c, _ = client
        assert c.get("/scim/v2/Users/nope", headers=_auth()).status_code == 404
