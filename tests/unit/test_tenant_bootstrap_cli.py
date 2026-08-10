"""Canonical tenant bootstrap and CLI service-key tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.gateway.auth import tenant_bootstrap
from src.gateway.auth.api_key_service import APIKeyService
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    Project,
    ScimUser,
    TenantRole,
)


class _Persistence:
    enabled = True

    def __init__(self) -> None:
        self.project: Project | None = None
        self.user: ScimUser | None = None
        self.membership_calls: list[tuple[str, str, str, bool]] = []

    async def get_project(
        self,
        project_id: str,
        tenant_id: str,
    ) -> Project | None:
        if (
            self.project is not None
            and self.project.project_id == project_id
            and self.project.tenant_id == tenant_id
        ):
            return self.project
        return None

    async def create_project(self, project: Project) -> int:
        if self.project is not None:
            raise ValueError("already exists")
        self.project = replace(project, revision=1)
        return 1

    async def set_tenant_project_membership(
        self,
        tenant_id: str,
        project_id: str,
        user_id: str,
        *,
        granted: bool,
    ) -> tuple[Project, bool]:
        self.membership_calls.append(
            (tenant_id, project_id, user_id, granted)
        )
        assert self.project is not None
        principal_id = f"scim:{user_id}"
        changed = principal_id not in self.project.members
        if changed:
            self.project = replace(
                self.project,
                members=[principal_id],
                revision=self.project.revision + 1,
            )
        return self.project, changed


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
        tenant_id: str,
        *,
        force: bool = False,
    ) -> None:
        assert tenant_id == "tenant-a"
        assert force is True

    def get_user_by_username(
        self,
        user_name: str,
        tenant_id: str,
    ) -> ScimUser | None:
        user = self.persistence.user
        if (
            user is not None
            and user.user_name.casefold() == user_name.casefold()
            and user.tenant_id == tenant_id
        ):
            return user
        return None

    async def create_user(self, user: ScimUser) -> ScimUser:
        assert self.persistence.user is None
        user.id = "user-a"
        user.authorization_version = 1
        self.persistence.user = user
        return user


class _Repository:
    def __init__(self, persistence: _Persistence) -> None:
        self.persistence = persistence

    async def resolve(self, identity) -> Principal | None:
        user = self.persistence.user
        project = self.persistence.project
        if user is None or project is None:
            return None
        return Principal(
            principal_id=f"scim:{user.id}",
            tenant_id=user.tenant_id,
            subject=user.subject,
            issuer=user.issuer,
            roles=frozenset({TenantRole.TENANT_ADMIN}),
            auth_method=AuthMethod.OIDC_JWT,
            membership_status=MembershipStatus.ACTIVE,
            project_ids=frozenset({project.project_id}),
            authorization_version=user.authorization_version,
        )


def _install_fakes(monkeypatch) -> None:
    monkeypatch.setattr(tenant_bootstrap, "ScimStore", _Store)
    monkeypatch.setattr(
        tenant_bootstrap,
        "DynamoPrincipalRepository",
        _Repository,
    )


def _run(persistence: _Persistence):
    return asyncio.run(
        tenant_bootstrap.bootstrap_tenant(
            persistence,
            tenant_id="tenant-a",
            project_id="project-a",
            project_name="Production",
            issuer="https://idp.example.test",
            subject="admin-subject",
            user_name="admin@example.test",
            display_name="Tenant Admin",
            email="admin@example.test",
            budget_limit=100.0,
        )
    )


def test_bootstrap_creates_and_strongly_verifies_authority(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    persistence = _Persistence()

    result = _run(persistence)

    assert result.project_created is True
    assert result.scim_user_created is True
    assert result.membership_changed is True
    assert result.principal_id == "scim:user-a"
    assert result.project_revision == 2
    assert persistence.project is not None
    assert persistence.project.tenant_id == "tenant-a"
    assert persistence.project.members == ["scim:user-a"]
    assert persistence.user is not None
    assert persistence.user.roles == ["tenant_admin"]
    assert persistence.user.project_id == ""
    assert persistence.membership_calls == [
        ("tenant-a", "project-a", "user-a", True)
    ]


def test_bootstrap_is_restartable_without_replacing_authority(
    monkeypatch,
) -> None:
    _install_fakes(monkeypatch)
    persistence = _Persistence()
    _run(persistence)

    result = _run(persistence)

    assert result.project_created is False
    assert result.scim_user_created is False
    assert result.membership_changed is False
    assert result.project_revision == 2


def test_bootstrap_refuses_username_bound_to_another_subject(
    monkeypatch,
) -> None:
    _install_fakes(monkeypatch)
    persistence = _Persistence()
    persistence.user = ScimUser(
        id="user-a",
        user_name="admin@example.test",
        tenant_id="tenant-a",
        issuer="https://idp.example.test",
        subject="other-subject",
        roles=["tenant_admin"],
    )

    with pytest.raises(
        tenant_bootstrap.TenantBootstrapError,
        match="different canonical identity",
    ):
        _run(persistence)


def test_issue_key_tenant_uses_canonical_service_defaults(
    monkeypatch,
    capsys,
) -> None:
    from src.gateway import cli

    captured: dict[str, object] = {}

    async def issue_key(self, **kwargs):
        captured.update(kwargs)
        return object(), "axon_" + "a" * 64

    monkeypatch.setattr(APIKeyService, "issue_key", issue_key)
    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    cli.cmd_issue_key(
        SimpleNamespace(
            project="project-a",
            tenant="tenant-a",
            name="service",
            scopes=None,
        )
    )

    assert captured["tenant_id"] == "tenant-a"
    assert captured["scopes"] == [
        "model.list",
        "inference.invoke",
        "query.select",
    ]
    assert "tenant 'tenant-a'" in capsys.readouterr().out


def test_issue_key_tenant_rejects_legacy_admin_scopes(monkeypatch) -> None:
    from src.gateway import cli

    monkeypatch.delenv("LLM_ROUTER_DYNAMODB_ENABLED", raising=False)
    with pytest.raises(
        ValueError,
        match="cannot carry legacy admin scopes",
    ):
        cli.cmd_issue_key(
            SimpleNamespace(
                project="project-a",
                tenant="tenant-a",
                name="service",
                scopes="inference.invoke,admin:*",
            )
        )
