"""Managed Cognito and canonical-authority lifecycle contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.gateway.auth import managed_identity_lifecycle as lifecycle
from src.gateway.auth.dynamo_principal_repository import (
    PrincipalConflictError,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    ScimUser,
    TenantRole,
)


ISSUER = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_POOL"
)


class _AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Cognito:
    def __init__(self) -> None:
        self.user: dict | None = None
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.disable_calls: list[dict] = []
        self.sign_out_calls: list[dict] = []

    def admin_get_user(self, **kwargs):
        if self.user is None:
            raise _AwsError("UserNotFoundException")
        attributes = {
            item["Name"]: item["Value"]
            for item in self.user["UserAttributes"]
        }
        if kwargs["Username"] not in {
            self.user["Username"],
            attributes["email"],
        }:
            raise _AwsError("UserNotFoundException")
        return deepcopy(self.user)

    def admin_create_user(self, **kwargs):
        self.create_calls.append(deepcopy(kwargs))
        attributes = deepcopy(kwargs["UserAttributes"])
        attributes.append({"Name": "sub", "Value": "cognito-subject"})
        self.user = {
            "Username": "cognito-subject",
            "Enabled": True,
            "UserStatus": "FORCE_CHANGE_PASSWORD",
            "UserAttributes": attributes,
        }
        return {"User": deepcopy(self.user)}

    def admin_update_user_attributes(self, **kwargs):
        self.update_calls.append(deepcopy(kwargs))
        assert self.user is not None
        attributes = {
            item["Name"]: item["Value"]
            for item in self.user["UserAttributes"]
        }
        attributes.update(
            {
                item["Name"]: item["Value"]
                for item in kwargs["UserAttributes"]
            }
        )
        self.user["UserAttributes"] = [
            {"Name": name, "Value": value}
            for name, value in attributes.items()
        ]

    def admin_disable_user(self, **kwargs):
        self.disable_calls.append(deepcopy(kwargs))
        assert self.user is not None
        self.user["Enabled"] = False

    def admin_user_global_sign_out(self, **kwargs):
        self.sign_out_calls.append(deepcopy(kwargs))


class _Persistence:
    enabled = True

    def __init__(self) -> None:
        self.user: ScimUser | None = None
        self.principal: Principal | None = None
        self.membership_calls: list[tuple[str, str, str, bool]] = []
        self.principal_puts = 0

    def sync_user(self) -> None:
        assert self.user is not None
        projects = set(self.user.project_ids)
        if self.user.project_id:
            projects.add(self.user.project_id)
        self.principal = Principal(
            principal_id=f"scim:{self.user.id}",
            tenant_id=self.user.tenant_id,
            subject=self.user.subject,
            issuer=self.user.issuer,
            roles=frozenset(
                TenantRole(role) for role in self.user.roles
            ),
            auth_method=AuthMethod.OIDC_JWT,
            membership_status=(
                MembershipStatus.ACTIVE
                if self.user.active and not self.user.deleted
                else MembershipStatus.DEPROVISIONED
            ),
            project_ids=frozenset(projects),
            authorization_version=self.user.authorization_version,
            email=self.user.primary_email or None,
        )

    async def set_tenant_project_membership(
        self,
        tenant_id: str,
        project_id: str,
        user_id: str,
        *,
        granted: bool,
    ):
        self.membership_calls.append(
            (tenant_id, project_id, user_id, granted)
        )
        assert self.user is not None
        assert self.user.tenant_id == tenant_id
        assert self.user.id == user_id
        projects = set(self.user.project_ids)
        changed = project_id not in projects if granted else project_id in projects
        if changed:
            if granted:
                projects.add(project_id)
            else:
                projects.discard(project_id)
            self.user = replace(
                self.user,
                project_ids=sorted(projects),
                authorization_version=(
                    self.user.authorization_version + 1
                ),
            )
            self.sync_user()
        return SimpleNamespace(), changed


class _Store:
    def __init__(
        self,
        persistence: _Persistence,
        *,
        canonical_identity_required: bool,
    ) -> None:
        assert canonical_identity_required is True
        self.persistence = persistence

    async def initialize(self) -> None:
        return None

    async def ensure_tenant_current(
        self,
        _tenant_id: str,
        *,
        force: bool = False,
    ) -> None:
        assert force is True

    def get_user_by_username(
        self,
        user_name: str,
        tenant_id: str,
    ) -> ScimUser | None:
        user = self.persistence.user
        if (
            user is not None
            and user.tenant_id == tenant_id
            and user.user_name.casefold() == user_name.casefold()
        ):
            return user
        return None

    async def create_user(self, user: ScimUser) -> ScimUser:
        assert self.persistence.user is None
        created = replace(
            user,
            id="user-a",
            authorization_version=1,
        )
        self.persistence.user = created
        self.persistence.sync_user()
        return created

    async def replace_user(
        self,
        user_id: str,
        user: ScimUser,
        tenant_id: str,
    ) -> ScimUser:
        existing = self.persistence.user
        assert existing is not None
        assert existing.id == user_id
        updated = replace(
            user,
            id=user_id,
            tenant_id=tenant_id,
            authorization_version=(
                existing.authorization_version + 1
            ),
        )
        self.persistence.user = updated
        self.persistence.sync_user()
        return updated

    async def set_user_active(
        self,
        user_id: str,
        active: bool,
        tenant_id: str,
    ) -> ScimUser:
        user = self.persistence.user
        assert user is not None
        assert user.id == user_id
        assert user.tenant_id == tenant_id
        if user.active != active:
            user = replace(
                user,
                active=active,
                authorization_version=user.authorization_version + 1,
            )
            self.persistence.user = user
            self.persistence.sync_user()
        return user

    def roles_for_user(self, user: ScimUser) -> list[str]:
        return sorted(user.roles)


class _Repository:
    def __init__(self, persistence: _Persistence) -> None:
        self.persistence = persistence

    async def resolve(self, identity) -> Principal | None:
        principal = self.persistence.principal
        if (
            principal is None
            or principal.membership_status is not MembershipStatus.ACTIVE
            or principal.issuer != identity.issuer
            or principal.subject != identity.subject
            or (
                identity.tenant_hint is not None
                and principal.tenant_id != identity.tenant_hint
            )
        ):
            return None
        return principal

    async def put(
        self,
        principal: Principal,
        *,
        expected_authorization_version: int | None = None,
    ) -> None:
        assert expected_authorization_version is None
        if self.persistence.principal is not None:
            raise PrincipalConflictError("exists")
        self.persistence.principal = principal
        self.persistence.principal_puts += 1


@pytest.fixture(autouse=True)
def _install_fakes(monkeypatch):
    monkeypatch.setattr(lifecycle, "ScimStore", _Store)
    monkeypatch.setattr(
        lifecycle,
        "DynamoPrincipalRepository",
        _Repository,
    )


def _user_arguments(
    cognito: _Cognito,
    persistence: _Persistence,
) -> dict:
    return {
        "cognito_client": cognito,
        "persistence": persistence,
        "user_pool_id": "us-east-1_POOL",
        "issuer": ISSUER,
        "user_name": "user@example.com",
        "email": "user@example.com",
        "display_name": "Managed User",
        "tenant_id": "tenant-a",
        "role": "tenant_member",
        "project_ids": ["project-a"],
        "default_project_id": "project-a",
    }


async def test_invite_is_restartable_and_grants_exact_authority() -> None:
    cognito = _Cognito()
    persistence = _Persistence()
    arguments = _user_arguments(cognito, persistence)

    first = await lifecycle.invite_managed_tenant_user(**arguments)
    second = await lifecycle.invite_managed_tenant_user(**arguments)

    assert first.cognito_created is True
    assert first.canonical_created is True
    assert first.canonical_changed is True
    assert second.cognito_created is False
    assert second.canonical_created is False
    assert second.canonical_changed is False
    assert len(cognito.create_calls) == 1
    assert persistence.principal is not None
    assert persistence.principal.roles == frozenset(
        {TenantRole.TENANT_MEMBER}
    )
    assert persistence.principal.project_ids == frozenset({"project-a"})
    assert persistence.membership_calls == [
        ("tenant-a", "project-a", "user-a", True)
    ]


async def test_update_reconciles_role_grants_and_token_hint_once() -> None:
    cognito = _Cognito()
    persistence = _Persistence()
    arguments = _user_arguments(cognito, persistence)
    await lifecycle.invite_managed_tenant_user(**arguments)
    persistence.membership_calls.clear()

    updated = {
        **arguments,
        "email": "renamed@example.com",
        "display_name": "Auditor",
        "role": "tenant_auditor",
        "project_ids": ["project-b"],
        "default_project_id": "project-b",
    }
    first = await lifecycle.update_managed_tenant_user(**updated)
    second = await lifecycle.update_managed_tenant_user(**updated)

    assert first.canonical_changed is True
    assert first.cognito_changed is True
    assert second.canonical_changed is False
    assert second.cognito_changed is False
    assert persistence.membership_calls == [
        ("tenant-a", "project-a", "user-a", False),
        ("tenant-a", "project-b", "user-a", True),
    ]
    assert persistence.principal is not None
    assert persistence.principal.roles == frozenset(
        {TenantRole.TENANT_AUDITOR}
    )
    assert persistence.principal.project_ids == frozenset({"project-b"})
    assert len(cognito.update_calls) == 1
    assert len(cognito.sign_out_calls) == 1


async def test_disable_is_idempotent_and_revokes_all_authority() -> None:
    cognito = _Cognito()
    persistence = _Persistence()
    arguments = _user_arguments(cognito, persistence)
    arguments["project_ids"] = ["project-a", "project-b"]
    await lifecycle.invite_managed_tenant_user(**arguments)

    disable = {
        key: arguments[key]
        for key in (
            "cognito_client",
            "persistence",
            "user_pool_id",
            "issuer",
            "user_name",
            "tenant_id",
        )
    }
    first = await lifecycle.disable_managed_tenant_user(**disable)
    second = await lifecycle.disable_managed_tenant_user(**disable)

    assert first.cognito_changed is True
    assert first.canonical_changed is True
    assert second.cognito_changed is False
    assert second.canonical_changed is False
    assert cognito.user is not None
    assert cognito.user["Enabled"] is False
    assert len(cognito.disable_calls) == 1
    assert persistence.principal is not None
    assert (
        persistence.principal.membership_status
        is MembershipStatus.DEPROVISIONED
    )
    assert persistence.principal.project_ids == frozenset()


async def test_tenant_lifecycle_cannot_assign_platform_admin() -> None:
    cognito = _Cognito()
    persistence = _Persistence()
    arguments = _user_arguments(cognito, persistence)
    arguments["role"] = "platform_admin"

    with pytest.raises(
        ValueError,
        match="may only be tenant_admin, tenant_member, or tenant_auditor",
    ):
        await lifecycle.invite_managed_tenant_user(**arguments)

    assert cognito.create_calls == []
    assert persistence.user is None


async def test_platform_operator_bootstrap_is_separate_and_idempotent() -> None:
    cognito = _Cognito()
    persistence = _Persistence()
    arguments = {
        "cognito_client": cognito,
        "persistence": persistence,
        "user_pool_id": "us-east-1_POOL",
        "issuer": ISSUER,
        "user_name": "operator@example.com",
        "email": "operator@example.com",
    }

    first = await lifecycle.bootstrap_platform_operator(**arguments)
    second = await lifecycle.bootstrap_platform_operator(**arguments)

    assert first.cognito_created is True
    assert first.canonical_created is True
    assert second.cognito_created is False
    assert second.canonical_created is False
    assert persistence.user is None
    assert persistence.principal_puts == 1
    assert persistence.principal is not None
    assert persistence.principal.roles == frozenset(
        {TenantRole.PLATFORM_ADMIN}
    )
    assert persistence.principal.tenant_id == "platform-home"
    assert persistence.principal.project_ids == frozenset()


async def test_update_refuses_cognito_tenant_reassignment() -> None:
    cognito = _Cognito()
    persistence = _Persistence()
    arguments = _user_arguments(cognito, persistence)
    await lifecycle.invite_managed_tenant_user(**arguments)
    assert cognito.user is not None
    for attribute in cognito.user["UserAttributes"]:
        if attribute["Name"] == "custom:tenant_id":
            attribute["Value"] = "tenant-b"

    with pytest.raises(
        lifecycle.ManagedIdentityError,
        match="different tenant",
    ):
        await lifecycle.update_managed_tenant_user(**arguments)
