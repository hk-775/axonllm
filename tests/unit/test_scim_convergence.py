"""Cross-replica convergence tests for the tenant SCIM directory."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.gateway.auth.scim_routes import ScimAPI, create_scim_routes
from src.gateway.auth.scim_service import ScimStore
from src.gateway.models import ScimGroup, ScimUser


class _ConvergencePersistence:
    enabled = True

    def __init__(self) -> None:
        self.users: dict[str, dict[str, ScimUser]] = {}
        self.groups: dict[str, dict[str, ScimGroup]] = {}
        self.versions: dict[str, int | None] = {}
        self.version_reads: dict[str, int] = {}
        self.snapshot_reads: dict[str, int] = {}
        self.bump_on_save = True
        self.block_snapshot = False
        self.snapshot_started: asyncio.Event | None = None
        self.snapshot_release: asyncio.Event | None = None

    def seed_user(self, user: ScimUser, *, version: int = 1) -> None:
        self.users.setdefault(user.tenant_id, {})[user.id] = copy.deepcopy(user)
        self.versions[user.tenant_id] = version

    def replace_durable_user(
        self,
        user: ScimUser,
        *,
        version: int,
    ) -> None:
        self.users.setdefault(user.tenant_id, {})[user.id] = copy.deepcopy(user)
        self.versions[user.tenant_id] = version

    async def get_tenant_scim_version(
        self,
        tenant_id: str,
    ) -> int | None:
        self.version_reads[tenant_id] = self.version_reads.get(tenant_id, 0) + 1
        return self.versions.get(tenant_id, 0)

    async def load_tenant_scim_snapshot_or_none(
        self,
        tenant_id: str,
    ) -> tuple[list[ScimUser], list[ScimGroup]] | None:
        self.snapshot_reads[tenant_id] = self.snapshot_reads.get(tenant_id, 0) + 1
        snapshot = (
            copy.deepcopy(list(self.users.get(tenant_id, {}).values())),
            copy.deepcopy(list(self.groups.get(tenant_id, {}).values())),
        )
        if self.block_snapshot:
            assert self.snapshot_started is not None
            assert self.snapshot_release is not None
            self.snapshot_started.set()
            await self.snapshot_release.wait()
        return snapshot

    async def save_scim_user_with_principal(
        self,
        user: ScimUser,
        _principal,
        **_kwargs,
    ) -> None:
        self.users.setdefault(user.tenant_id, {})[user.id] = copy.deepcopy(user)
        if self.bump_on_save:
            self.versions[user.tenant_id] = int(self.versions.get(user.tenant_id, 0) or 0) + 1

    async def save_scim_group_with_principals(
        self,
        group: ScimGroup,
        *,
        user_updates,
        **_kwargs,
    ) -> None:
        self.groups.setdefault(group.tenant_id, {})[group.id] = copy.deepcopy(group)
        for user, _principal, _expected in user_updates:
            self.users.setdefault(user.tenant_id, {})[user.id] = copy.deepcopy(user)
        if self.bump_on_save:
            self.versions[group.tenant_id] = int(self.versions.get(group.tenant_id, 0) or 0) + 1


def _user(
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
    display_name: str = "Initial",
) -> ScimUser:
    return ScimUser(
        id=user_id,
        user_name=f"{user_id}@example.test",
        tenant_id=tenant_id,
        issuer="https://idp.example.test",
        subject=f"subject-{user_id}",
        external_id=f"subject-{user_id}",
        display_name=display_name,
    )


async def test_initialize_validates_contract_without_reading_directory() -> None:
    persistence = _ConvergencePersistence()
    store = ScimStore(
        persistence,
        canonical_identity_required=True,
    )

    await store.initialize()

    assert persistence.version_reads == {}
    assert persistence.snapshot_reads == {}


async def test_canonical_initialization_rejects_missing_contract() -> None:
    persistence = type("_MissingContract", (), {"enabled": True})()
    store = ScimStore(
        persistence,
        canonical_identity_required=True,
    )

    with pytest.raises(
        RuntimeError,
        match="convergence persistence is unavailable",
    ):
        await store.initialize()


async def test_remote_mutation_converges_only_requested_tenant() -> None:
    persistence = _ConvergencePersistence()
    tenant_a = _user()
    tenant_b = _user(tenant_id="tenant-b", user_id="user-b")
    persistence.seed_user(tenant_a)
    persistence.seed_user(tenant_b)
    first = ScimStore(
        persistence,
        canonical_identity_required=True,
    )
    second = ScimStore(
        persistence,
        canonical_identity_required=True,
    )
    await first.ensure_tenant_current("tenant-a")
    await second.ensure_tenant_current("tenant-a")

    await first.set_user_active(tenant_a.id, False, "tenant-a")
    await second.ensure_tenant_current("tenant-a", force=True)

    assert second.get_user(tenant_a.id, "tenant-a").active is False
    assert second.get_user(tenant_b.id, "tenant-b") is None
    assert persistence.snapshot_reads.get("tenant-b", 0) == 0


async def test_tenant_refresh_is_single_flight() -> None:
    persistence = _ConvergencePersistence()
    persistence.seed_user(_user())
    persistence.block_snapshot = True
    persistence.snapshot_started = asyncio.Event()
    persistence.snapshot_release = asyncio.Event()
    store = ScimStore(
        persistence,
        canonical_identity_required=True,
    )

    refreshes = [asyncio.create_task(store.ensure_tenant_current("tenant-a")) for _ in range(20)]
    await persistence.snapshot_started.wait()
    assert persistence.version_reads["tenant-a"] == 1
    assert persistence.snapshot_reads["tenant-a"] == 1

    persistence.snapshot_release.set()
    await asyncio.gather(*refreshes)

    assert persistence.version_reads["tenant-a"] == 2
    assert persistence.snapshot_reads["tenant-a"] == 1


async def test_stale_refresh_cannot_replace_newer_local_mutation() -> None:
    persistence = _ConvergencePersistence()
    initial = _user()
    persistence.seed_user(initial)
    store = ScimStore(
        persistence,
        canonical_identity_required=True,
    )
    await store.ensure_tenant_current("tenant-a")

    remote = copy.deepcopy(initial)
    remote.display_name = "Remote"
    persistence.replace_durable_user(remote, version=2)
    persistence.block_snapshot = True
    persistence.snapshot_started = asyncio.Event()
    persistence.snapshot_release = asyncio.Event()
    refresh = asyncio.create_task(store.ensure_tenant_current("tenant-a", force=True))
    await persistence.snapshot_started.wait()

    persistence.bump_on_save = False
    local = copy.deepcopy(initial)
    local.display_name = "Local"
    await store.replace_user(initial.id, local, "tenant-a")
    persistence.snapshot_release.set()
    await refresh

    assert store.get_user(initial.id, "tenant-a").display_name == "Local"


async def test_foreign_snapshot_fails_closed_and_preserves_local_state() -> None:
    persistence = _ConvergencePersistence()
    local = _user()
    persistence.seed_user(local)
    store = ScimStore(
        persistence,
        canonical_identity_required=True,
    )
    await store.ensure_tenant_current("tenant-a")
    persistence.users["tenant-a"]["foreign"] = _user(
        tenant_id="tenant-b",
        user_id="foreign",
    )
    persistence.versions["tenant-a"] = 2

    with pytest.raises(RuntimeError, match="foreign user"):
        await store.ensure_tenant_current("tenant-a", force=True)

    assert store.get_user(local.id, "tenant-a") is not None
    assert store.get_user("foreign", "tenant-a") is None


def test_canonical_route_fails_closed_when_version_is_unreadable(
    monkeypatch,
) -> None:
    persistence = _ConvergencePersistence()
    persistence.versions["tenant-a"] = None
    store = ScimStore(
        persistence,
        canonical_identity_required=True,
    )
    monkeypatch.setenv(
        "AXON_SCIM_TENANTS",
        json.dumps(
            {
                "tenant-a": {
                    "issuer": "https://idp.example.test",
                    "token": "tenant-secret",
                }
            }
        ),
    )
    app = Starlette(routes=create_scim_routes(ScimAPI(store, canonical_identity_required=True)))

    response = TestClient(app).get(
        "/scim/v2/Users",
        headers={"Authorization": "Bearer tenant-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == ("SCIM identity persistence is unavailable")
