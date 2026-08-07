"""Adversarial tests for the tenant authorization baseline."""

from __future__ import annotations

import pytest

from src.gateway.auth.authorization import (
    Action,
    AuthorizationDenied,
    ResourceRef,
    authorize,
    require_authorized,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)


def _principal(
    role: TenantRole,
    *,
    tenant_id: str = "tenant-a",
    projects: frozenset[str] = frozenset({"project-a"}),
    scopes: frozenset[str] = frozenset(),
    status: MembershipStatus = MembershipStatus.ACTIVE,
) -> Principal:
    return Principal(
        principal_id=f"principal:{role.value}",
        tenant_id=tenant_id,
        subject=f"subject:{role.value}",
        issuer="https://idp.example.test",
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=status,
        project_ids=projects,
        scopes=scopes,
    )


def _tenant_resource(
    *,
    tenant_id: str = "tenant-a",
    project_id: str | None = "project-a",
) -> ResourceRef:
    return ResourceRef(
        resource_type="project",
        resource_id=project_id or tenant_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )


@pytest.mark.parametrize(
    "role",
    [
        TenantRole.TENANT_ADMIN,
        TenantRole.TENANT_MEMBER,
        TenantRole.TENANT_AUDITOR,
    ],
)
@pytest.mark.parametrize(
    "action",
    [
        Action.TENANT_CONFIG_READ,
        Action.MODEL_LIST,
        Action.INFERENCE_INVOKE,
        Action.QUERY_SELECT,
    ],
)
def test_tenant_users_can_read_and_run_select_queries(
    role: TenantRole,
    action: Action,
) -> None:
    assert authorize(_principal(role), action, _tenant_resource()).allowed


@pytest.mark.parametrize(
    "action",
    [
        Action.TENANT_CONFIG_WRITE,
        Action.MEMBERSHIP_WRITE,
        Action.API_KEY_MANAGE,
        Action.POLICY_WRITE,
        Action.QUOTA_WRITE,
        Action.WEBHOOK_WRITE,
    ],
)
def test_only_tenant_admin_can_mutate_tenant_configuration(action: Action) -> None:
    resource = _tenant_resource()

    assert authorize(
        _principal(TenantRole.TENANT_ADMIN),
        action,
        resource,
    ).allowed
    assert not authorize(
        _principal(TenantRole.TENANT_MEMBER),
        action,
        resource,
    ).allowed
    assert not authorize(
        _principal(TenantRole.TENANT_AUDITOR),
        action,
        resource,
    ).allowed


@pytest.mark.parametrize("role", list(TenantRole))
def test_no_role_receives_query_mutation_authority(role: TenantRole) -> None:
    decision = authorize(_principal(role), Action.QUERY_MUTATE, _tenant_resource())

    assert not decision.allowed
    assert decision.reason == "query_mutation_not_supported"


@pytest.mark.parametrize(
    "role",
    [
        TenantRole.TENANT_ADMIN,
        TenantRole.TENANT_MEMBER,
        TenantRole.TENANT_AUDITOR,
        TenantRole.SERVICE,
    ],
)
def test_cross_tenant_resources_are_concealed(role: TenantRole) -> None:
    decision = authorize(
        _principal(role),
        Action.TENANT_CONFIG_READ,
        _tenant_resource(tenant_id="tenant-b"),
    )

    assert not decision.allowed
    assert decision.reason == "resource_not_found"
    assert decision.conceal_resource
    assert decision.status_code == 404


def test_same_project_id_in_another_tenant_is_still_denied() -> None:
    decision = authorize(
        _principal(TenantRole.TENANT_ADMIN),
        Action.TENANT_CONFIG_READ,
        _tenant_resource(tenant_id="tenant-b", project_id="project-a"),
    )

    assert not decision.allowed
    assert decision.status_code == 404


def test_member_requires_an_explicit_project_grant() -> None:
    decision = authorize(
        _principal(TenantRole.TENANT_MEMBER, projects=frozenset()),
        Action.INFERENCE_INVOKE,
        _tenant_resource(),
    )

    assert not decision.allowed
    assert decision.status_code == 404


def test_tenant_admin_requires_explicit_project_grant_until_ownership_exists() -> None:
    decision = authorize(
        _principal(TenantRole.TENANT_ADMIN, projects=frozenset()),
        Action.TENANT_CONFIG_WRITE,
        _tenant_resource(project_id="unlisted-project"),
    )

    assert not decision.allowed
    assert decision.status_code == 404


def test_service_principal_requires_both_project_grant_and_scope() -> None:
    principal = _principal(
        TenantRole.SERVICE,
        scopes=frozenset({Action.INFERENCE_INVOKE.value}),
    )

    assert authorize(
        principal,
        Action.INFERENCE_INVOKE,
        _tenant_resource(),
    ).allowed
    assert not authorize(
        principal,
        Action.TENANT_CONFIG_READ,
        _tenant_resource(),
    ).allowed


def test_service_namespace_wildcard_does_not_grant_other_namespaces() -> None:
    principal = _principal(
        TenantRole.SERVICE,
        scopes=frozenset({"inference.*"}),
    )

    assert authorize(
        principal,
        Action.INFERENCE_INVOKE,
        _tenant_resource(),
    ).allowed
    assert not authorize(
        principal,
        Action.TENANT_CONFIG_WRITE,
        _tenant_resource(),
    ).allowed


@pytest.mark.parametrize(
    "action",
    [
        Action.TENANT_CONFIG_WRITE,
        Action.MEMBERSHIP_WRITE,
        Action.API_KEY_MANAGE,
        Action.POLICY_WRITE,
        Action.QUOTA_WRITE,
        Action.WEBHOOK_WRITE,
    ],
)
def test_service_wildcard_scope_cannot_cross_admin_only_ceiling(
    action: Action,
) -> None:
    principal = _principal(
        TenantRole.SERVICE,
        scopes=frozenset({"*"}),
    )

    decision = authorize(principal, action, _tenant_resource())

    assert not decision.allowed
    assert decision.reason == "role_not_allowed"


@pytest.mark.parametrize(
    "roles",
    [
        frozenset({
            TenantRole.SERVICE,
            TenantRole.TENANT_ADMIN,
        }),
        frozenset({
            TenantRole.PLATFORM_ADMIN,
            TenantRole.TENANT_ADMIN,
        }),
    ],
)
def test_service_and_platform_roles_are_exclusive(
    roles: frozenset[TenantRole],
) -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        Principal(
            principal_id="principal:mixed",
            tenant_id="tenant-a",
            subject="subject:mixed",
            issuer="https://idp.example.test",
            roles=roles,
            auth_method=AuthMethod.OIDC_JWT,
            project_ids=frozenset({"project-a"}),
        )


@pytest.mark.parametrize(
    "status",
    [
        MembershipStatus.INVITED,
        MembershipStatus.SUSPENDED,
        MembershipStatus.DEPROVISIONED,
    ],
)
def test_inactive_membership_denies_before_role_evaluation(
    status: MembershipStatus,
) -> None:
    decision = authorize(
        _principal(TenantRole.TENANT_ADMIN, status=status),
        Action.TENANT_CONFIG_WRITE,
        _tenant_resource(),
    )

    assert not decision.allowed
    assert decision.reason == "membership_inactive"


def test_unknown_actions_default_deny() -> None:
    decision = authorize(
        _principal(TenantRole.TENANT_ADMIN),
        "tenant.config.destroy",
        _tenant_resource(),
    )

    assert not decision.allowed
    assert decision.reason == "unknown_action"


def test_platform_admin_requires_an_explicit_break_glass_reason_for_tenant() -> None:
    principal = _principal(
        TenantRole.PLATFORM_ADMIN,
        tenant_id="platform",
        projects=frozenset(),
    )
    resource = _tenant_resource()

    assert not authorize(principal, Action.TENANT_CONFIG_READ, resource).allowed
    assert not authorize(
        principal,
        Action.TENANT_CONFIG_READ,
        resource,
        break_glass_reason=" ",
    ).allowed
    decision = authorize(
        principal,
        Action.TENANT_CONFIG_READ,
        resource,
        break_glass_reason="incident INC-1234",
    )
    assert decision.allowed
    assert decision.break_glass


def test_platform_actions_are_separate_from_tenant_authority() -> None:
    platform_resource = ResourceRef(
        resource_type="model_catalog",
        resource_id="global",
        tenant_id=None,
    )

    assert authorize(
        _principal(TenantRole.PLATFORM_ADMIN, tenant_id="platform"),
        Action.PLATFORM_WRITE,
        platform_resource,
    ).allowed
    assert not authorize(
        _principal(TenantRole.TENANT_ADMIN),
        Action.PLATFORM_READ,
        platform_resource,
    ).allowed


def test_require_authorized_preserves_concealment_decision() -> None:
    with pytest.raises(AuthorizationDenied) as caught:
        require_authorized(
            _principal(TenantRole.TENANT_MEMBER),
            Action.TENANT_CONFIG_READ,
            _tenant_resource(tenant_id="tenant-b"),
        )

    assert caught.value.decision.status_code == 404
