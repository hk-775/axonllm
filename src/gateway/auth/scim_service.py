"""SCIM 2.0 provisioning store — IdP-driven user/group lifecycle.

Backs the ``/scim/v2`` endpoints. An identity provider (Okta, Entra ID, etc.)
creates/updates/deactivates users and groups here; the gateway then resolves a
provisioned user's groups → roles when authenticating that user.

Storage mirrors the rest of AxonLLM: DynamoPersistence when enabled
(``SCIM#USER#<id>`` / ``SCIM#GROUP#<id>`` items), an in-memory dict otherwise.
The in-memory map is always kept in sync so reads are O(1) and don't hit Dynamo
on the hot path.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from src.gateway.models import ScimGroup, ScimUser

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence


class ScimConflictError(Exception):
    """Raised when creating a user whose userName already exists (SCIM 409)."""


class ScimNotFoundError(Exception):
    """Raised when a referenced resource id doesn't exist (SCIM 404)."""


class ScimStore:
    """CRUD + lookup for SCIM Users and Groups."""

    def __init__(self, persistence: DynamoPersistence | None = None) -> None:
        self._persistence = persistence
        self._users: dict[str, ScimUser] = {}
        self._groups: dict[str, ScimGroup] = {}
        self._username_index: dict[str, str] = {}  # lower(userName) -> user id

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Load provisioned users/groups from the durable store on startup."""
        if not (self._persistence and self._persistence.enabled):
            return
        for u in await self._persistence.load_scim_users():
            self._users[u.id] = u
            self._username_index[u.user_name.lower()] = u.id
        for g in await self._persistence.load_scim_groups():
            self._groups[g.id] = g

    # -- users ---------------------------------------------------------------

    async def create_user(self, user: ScimUser) -> ScimUser:
        key = user.user_name.lower()
        if key in self._username_index:
            raise ScimConflictError(f"userName '{user.user_name}' already exists")
        if not user.id:
            user.id = f"scu_{secrets.token_hex(12)}"
        self._users[user.id] = user
        self._username_index[key] = user.id
        await self._persist_user(user)
        return user

    def get_user(self, user_id: str) -> ScimUser | None:
        return self._users.get(user_id)

    def get_user_by_username(self, user_name: str) -> ScimUser | None:
        uid = self._username_index.get(user_name.lower())
        return self._users.get(uid) if uid else None

    async def replace_user(self, user_id: str, user: ScimUser) -> ScimUser:
        existing = self._users.get(user_id)
        if existing is None:
            raise ScimNotFoundError(user_id)
        # userName is the stable index key; keep the index consistent on rename.
        if existing.user_name.lower() != user.user_name.lower():
            self._username_index.pop(existing.user_name.lower(), None)
            self._username_index[user.user_name.lower()] = user_id
        user.id = user_id
        user.created_at = existing.created_at
        self._users[user_id] = user
        await self._persist_user(user)
        return user

    async def set_user_active(self, user_id: str, active: bool) -> ScimUser:
        """The joiner/mover/leaver switch — IdP deprovision sets active=false."""
        user = self._users.get(user_id)
        if user is None:
            raise ScimNotFoundError(user_id)
        user.active = active
        await self._persist_user(user)
        return user

    async def delete_user(self, user_id: str) -> None:
        user = self._users.pop(user_id, None)
        if user is None:
            raise ScimNotFoundError(user_id)
        self._username_index.pop(user.user_name.lower(), None)
        if self._persistence and self._persistence.enabled:
            await self._persistence.delete_scim_user(user_id)

    def list_users(
        self, user_name: str | None = None, start: int = 1, count: int = 100,
    ) -> tuple[list[ScimUser], int]:
        """List users, optionally filtered by exact userName. Returns (page, total).

        ``start`` is 1-based (SCIM startIndex). Only the ``userName eq`` filter is
        supported — the one every IdP uses to reconcile.
        """
        if user_name is not None:
            u = self.get_user_by_username(user_name)
            results = [u] if u else []
        else:
            results = sorted(self._users.values(), key=lambda x: x.created_at)
        total = len(results)
        page = results[max(0, start - 1): max(0, start - 1) + count]
        return page, total

    # -- groups --------------------------------------------------------------

    async def create_group(self, group: ScimGroup) -> ScimGroup:
        if not group.id:
            group.id = f"scg_{secrets.token_hex(12)}"
        self._groups[group.id] = group
        await self._persist_group(group)
        return group

    def get_group(self, group_id: str) -> ScimGroup | None:
        return self._groups.get(group_id)

    async def replace_group(self, group_id: str, group: ScimGroup) -> ScimGroup:
        existing = self._groups.get(group_id)
        if existing is None:
            raise ScimNotFoundError(group_id)
        group.id = group_id
        group.created_at = existing.created_at
        self._groups[group_id] = group
        await self._persist_group(group)
        return group

    async def delete_group(self, group_id: str) -> None:
        if self._groups.pop(group_id, None) is None:
            raise ScimNotFoundError(group_id)
        if self._persistence and self._persistence.enabled:
            await self._persistence.delete_scim_group(group_id)

    def list_groups(self, start: int = 1, count: int = 100) -> tuple[list[ScimGroup], int]:
        results = sorted(self._groups.values(), key=lambda x: x.created_at)
        total = len(results)
        return results[max(0, start - 1): max(0, start - 1) + count], total

    # -- role resolution -----------------------------------------------------

    def roles_for_user(self, user: ScimUser) -> list[str]:
        """Effective roles = the user's own roles ∪ the roles of their groups."""
        roles = set(user.roles)
        for gid in user.groups:
            g = self._groups.get(gid)
            if g:
                roles.update(g.roles)
        return sorted(roles)

    # -- persistence helpers -------------------------------------------------

    async def _persist_user(self, user: ScimUser) -> None:
        if self._persistence and self._persistence.enabled:
            await self._persistence.save_scim_user(user)

    async def _persist_group(self, group: ScimGroup) -> None:
        if self._persistence and self._persistence.enabled:
            await self._persistence.save_scim_group(group)
