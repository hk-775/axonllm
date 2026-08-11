"""Focused validation tests for Athena query-plane configuration."""

from __future__ import annotations

import json

import pytest

from src.gateway.query.models import (
    AthenaDatasource,
    AthenaRoleBinding,
    AthenaRoleBindings,
    QueryConfigurationError,
)


ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"
OTHER_ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-b"


def _datasource_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "datasource_id": "warehouse",
        "name": "Analytics warehouse",
        "role_arn": ROLE_ARN,
        "region": "us-east-1",
        "catalog": "AwsDataCatalog",
        "database": "analytics",
        "workgroup": "axon_read_only",
        "enabled": True,
    }
    value.update(overrides)
    return value


def test_datasource_validates_and_serializes_metadata_without_credentials() -> None:
    datasource = AthenaDatasource.from_mapping(
        _datasource_mapping(),
        tenant_id="tenant-a",
        project_id="project-a",
    )

    assert datasource.datasource_id == "warehouse"
    assert datasource.revision == 0
    assert datasource.created_at == datasource.updated_at
    assert datasource.to_dict()["role_arn"] == ROLE_ARN
    assert datasource.to_dict(include_role=False) == {
        "datasource_id": "warehouse",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "name": "Analytics warehouse",
        "region": "us-east-1",
        "catalog": "AwsDataCatalog",
        "database": "analytics",
        "workgroup": "axon_read_only",
        "enabled": True,
        "revision": 0,
        "created_at": datasource.created_at,
        "updated_at": datasource.updated_at,
        "role_configured": True,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", " Analytics"),
        ("role_arn", "arn:aws:iam::123456789012:role/*"),
        ("role_arn", "arn:aws:iam::not-an-account:role/query"),
        ("region", "US-EAST-1"),
        ("region", "us-east-1\n"),
        ("catalog", "catalog/name"),
        ("database", "analytics;drop"),
        ("workgroup", ""),
        ("enabled", 1),
    ],
)
def test_datasource_rejects_invalid_metadata(
    field: str,
    value: object,
) -> None:
    mapping = _datasource_mapping()
    mapping[field] = value

    with pytest.raises(QueryConfigurationError):
        AthenaDatasource.from_mapping(
            mapping,
            tenant_id="tenant-a",
            project_id="project-a",
        )


@pytest.mark.parametrize(
    "injected_field",
    [
        "access_key_id",
        "secret_access_key",
        "session_token",
        "password",
        "connection_string",
        "tenant_id",
        "project_id",
    ],
)
def test_datasource_rejects_unknown_and_credential_fields(
    injected_field: str,
) -> None:
    mapping = _datasource_mapping()
    mapping[injected_field] = "attacker-controlled"

    with pytest.raises(
        QueryConfigurationError,
        match="unsupported fields",
    ):
        AthenaDatasource.from_mapping(
            mapping,
            tenant_id="tenant-a",
            project_id="project-a",
        )


@pytest.mark.parametrize(
    "system_field",
    ["revision", "created_at", "updated_at"],
)
def test_mapping_rejects_client_supplied_system_fields(
    system_field: str,
) -> None:
    injected_values = {
        "revision": 999,
        "created_at": "2000-01-01T00:00:00+00:00",
        "updated_at": "2001-01-01T00:00:00+00:00",
    }

    with pytest.raises(
        QueryConfigurationError,
        match="unsupported fields",
    ):
        AthenaDatasource.from_mapping(
            _datasource_mapping(
                **{system_field: injected_values[system_field]}
            ),
            tenant_id="tenant-a",
            project_id="project-a",
        )


def test_server_supplied_system_fields_are_preserved() -> None:
    datasource = AthenaDatasource.from_mapping(
        _datasource_mapping(
            datasource_id="attacker-id",
        ),
        tenant_id="tenant-a",
        project_id="project-a",
        datasource_id="server-id",
        revision=7,
        created_at="2026-08-10T12:00:00+00:00",
        updated_at="2026-08-10T12:30:00+00:00",
    )

    assert datasource.datasource_id == "server-id"
    assert datasource.revision == 7
    assert datasource.created_at == "2026-08-10T12:00:00+00:00"
    assert datasource.updated_at == "2026-08-10T12:30:00+00:00"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("datasource_id", "../warehouse"),
        ("tenant_id", "tenant/a"),
        ("project_id", "project a"),
        ("revision", True),
        ("revision", -1),
        ("created_at", "2026-08-10T12:00:00"),
        ("updated_at", "not-a-timestamp"),
    ],
)
def test_datasource_dataclass_rejects_invalid_system_fields(
    field: str,
    value: object,
) -> None:
    values = {
        "datasource_id": "warehouse",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "name": "Analytics warehouse",
        "role_arn": ROLE_ARN,
        "region": "us-east-1",
        "catalog": "AwsDataCatalog",
        "database": "analytics",
        "workgroup": "axon_read_only",
        "revision": 1,
        "created_at": "2026-08-10T12:00:00+00:00",
        "updated_at": "2026-08-10T12:30:00+00:00",
    }
    values[field] = value

    with pytest.raises(QueryConfigurationError):
        AthenaDatasource(**values)


def test_role_bindings_are_exactly_scoped_to_tenant_project_and_role() -> None:
    bindings = AthenaRoleBindings.from_json(
        json.dumps(
            [
                {
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "role_arn": ROLE_ARN,
                },
                {
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "role_arn": OTHER_ROLE_ARN,
                },
            ]
        )
    )

    assert bindings.allows("tenant-a", "project-a", ROLE_ARN)
    assert bindings.allows("tenant-a", "project-a", OTHER_ROLE_ARN)
    assert not bindings.allows("tenant-b", "project-a", ROLE_ARN)
    assert not bindings.allows("tenant-a", "project-b", ROLE_ARN)
    assert not bindings.allows(
        "tenant-a",
        "project-a",
        "arn:aws:iam::123456789012:role/unapproved",
    )
    assert bindings.role_arns == frozenset({ROLE_ARN, OTHER_ROLE_ARN})


def test_empty_role_binding_configuration_is_default_deny() -> None:
    assert AthenaRoleBindings.from_json(None).empty
    assert AthenaRoleBindings.from_json("").empty
    assert not AthenaRoleBindings().allows(
        "tenant-a",
        "project-a",
        ROLE_ARN,
    )


def test_role_bindings_reject_duplicate_entries() -> None:
    binding = AthenaRoleBinding(
        tenant_id="tenant-a",
        project_id="project-a",
        role_arn=ROLE_ARN,
    )

    with pytest.raises(QueryConfigurationError, match="duplicates"):
        AthenaRoleBindings((binding, binding))


def test_role_bindings_reject_duplicate_json_fields() -> None:
    raw = (
        '[{"tenant_id":"tenant-a","tenant_id":"tenant-b",'
        f'"project_id":"project-a","role_arn":"{ROLE_ARN}"}}]'
    )

    with pytest.raises(QueryConfigurationError, match="duplicate field"):
        AthenaRoleBindings.from_json(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '"not-an-array"',
        "[{}]",
        (
            '[{"tenant_id":"tenant-a","project_id":"project-a",'
            '"role_arn":"arn:aws:iam::123456789012:role/*"}]'
        ),
        (
            '[{"tenant_id":"tenant-a","project_id":"project-a",'
            f'"role_arn":"{ROLE_ARN}","unexpected":true}}]'
        ),
        "not-json",
    ],
)
def test_role_bindings_reject_ambiguous_or_unsafe_json(raw: str) -> None:
    with pytest.raises(QueryConfigurationError):
        AthenaRoleBindings.from_json(raw)


def test_role_bindings_enforce_agentcore_character_boundary() -> None:
    at_limit = "[]" + (" " * (2_048 - 2))

    assert AthenaRoleBindings.from_json(at_limit).empty
    with pytest.raises(QueryConfigurationError, match="2,048-character"):
        AthenaRoleBindings.from_json(at_limit + " ")


def test_role_binding_limit_counts_characters_not_utf8_bytes() -> None:
    raw = json.dumps(
        ["\u00e9" * 1_024],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(raw) < 2_048
    assert len(raw.encode("utf-8")) > 2_048

    with pytest.raises(QueryConfigurationError, match="must be an object"):
        AthenaRoleBindings.from_json(raw)
