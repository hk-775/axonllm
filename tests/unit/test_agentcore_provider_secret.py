from __future__ import annotations

import json

import pytest

from src.gateway.deployment.provider_secret import (
    ALLOWED_SECRET_FIELDS,
    ProviderSecretError,
    collect_provider_secret,
    load_provider_environment_file,
    rollback_provider_secret,
    synchronize_provider_secret,
)


SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:123456789012:"
    "secret:axon-provider-AbCd12"
)


class _SecretsManager:
    def __init__(self, value: dict[str, str], version_id: str = "version-1"):
        self.versions = {version_id: dict(value)}
        self.current = version_id
        self.put_calls: list[dict] = []
        self.update_calls: list[dict] = []

    def get_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == SECRET_ARN
        version_id = kwargs.get("VersionId", self.current)
        if "VersionStage" in kwargs:
            assert kwargs["VersionStage"] == "AWSCURRENT"
        return {
            "ARN": SECRET_ARN,
            "VersionId": version_id,
            "SecretString": json.dumps(self.versions[version_id]),
        }

    def put_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == SECRET_ARN
        assert kwargs["VersionStages"] == ["AWSCURRENT"]
        assert isinstance(kwargs["ClientRequestToken"], str)
        self.put_calls.append(kwargs)
        version_id = "version-2"
        self.versions[version_id] = json.loads(kwargs["SecretString"])
        self.current = version_id
        return {"ARN": SECRET_ARN, "VersionId": version_id}

    def describe_secret(self, **kwargs):
        assert kwargs == {"SecretId": SECRET_ARN}
        return {
            "VersionIdsToStages": {
                version_id: (
                    ["AWSCURRENT"] if version_id == self.current else ["AWSPREVIOUS"]
                )
                for version_id in self.versions
            }
        }

    def update_secret_version_stage(self, **kwargs):
        assert kwargs["SecretId"] == SECRET_ARN
        assert kwargs["VersionStage"] == "AWSCURRENT"
        assert kwargs["RemoveFromVersionId"] == self.current
        self.update_calls.append(kwargs)
        self.current = kwargs["MoveToVersionId"]


def _environment() -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "openai-secret",
        "UNAPPROVED_SECRET": "must-not-be-copied",
    }


def test_collects_only_approved_fields_and_requires_enabled_credentials() -> None:
    values = collect_provider_secret(
        _environment(),
        ["bedrock", "bedrock-mantle", "anthropic", "openai"],
    )

    assert values == {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "openai-secret",
    }
    assert "UNAPPROVED_SECRET" not in ALLOWED_SECRET_FIELDS

    with pytest.raises(
        ProviderSecretError,
        match="cohere:COHERE_API_KEY",
    ):
        collect_provider_secret(_environment(), ["cohere"])


def test_collect_excludes_credentials_for_disabled_providers() -> None:
    values = collect_provider_secret(
        _environment(),
        ["openai", "bedrock"],
    )

    assert values == {"OPENAI_API_KEY": "openai-secret"}


def test_env_file_is_data_not_shell_and_loads_only_approved_fields(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# deployment values",
                "export OPENAI_API_KEY='openai secret'",
                "UNRELATED_PASSWORD=do-not-load",
                "ANTHROPIC_API_KEY=anthropic-secret",
                "SHELL_PAYLOAD=$(touch /tmp/must-not-run)",
            ]
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    values = load_provider_environment_file(env_file)

    assert values == {
        "OPENAI_API_KEY": "openai secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
    }


def test_env_file_requires_owner_only_permissions(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    env_file.chmod(0o640)

    with pytest.raises(ProviderSecretError, match="group or others"):
        load_provider_environment_file(env_file)


def test_vertex_document_is_canonicalized_and_requires_project() -> None:
    environment = {
        "GCP_CREDENTIALS_JSON": json.dumps(
            {
                "type": "external_account",
                "audience": "test",
            },
            indent=2,
        ),
        "GCP_PROJECT_ID": "project-a",
    }

    values = collect_provider_secret(environment, ["vertex_ai"])

    assert values["GCP_CREDENTIALS_JSON"] == (
        '{"audience":"test","type":"external_account"}'
    )
    assert values["GCP_PROJECT_ID"] == "project-a"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("OPENAI_API_KEY", " value"),
        ("OPENAI_API_KEY", "value\n"),
        ("AZURE_OPENAI_ENDPOINT", "http://tenant.example"),
        ("AZURE_OPENAI_ENDPOINT", "https://user:pass@tenant.example"),
        ("GCP_PROJECT_ID", "project with spaces"),
        ("GCP_CREDENTIALS_JSON", '{"type":"authorized_user"}'),
    ],
)
def test_rejects_malformed_approved_values(field_name: str, value: str) -> None:
    environment = {field_name: value}
    provider = {
        "OPENAI_API_KEY": "openai",
        "AZURE_OPENAI_ENDPOINT": "azure_openai",
        "GCP_PROJECT_ID": "vertex_ai",
        "GCP_CREDENTIALS_JSON": "vertex_ai",
    }[field_name]
    if provider == "azure_openai":
        environment["AZURE_OPENAI_API_KEY"] = "key"
    if provider == "vertex_ai":
        environment.setdefault(
            "GCP_CREDENTIALS_JSON",
            '{"type":"external_account"}',
        )
        environment.setdefault("GCP_PROJECT_ID", "project-a")

    with pytest.raises(ProviderSecretError):
        collect_provider_secret(environment, [provider])


def test_sync_replaces_the_complete_document_without_exposing_values() -> None:
    client = _SecretsManager(
        {
            "ANTHROPIC_API_KEY": "old-secret",
            "placeholder": "generated",
        }
    )

    result = synchronize_provider_secret(
        client,
        secret_arn=SECRET_ARN,
        environ=_environment(),
        enabled_providers=["anthropic", "openai", "bedrock"],
    )

    assert result.changed is True
    assert result.version_id == "version-2"
    assert result.previous_version_id == "version-1"
    assert result.configured_fields == (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    )
    assert len(result.fingerprint) == 64
    assert client.versions["version-2"] == {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "openai-secret",
    }
    serialized_metadata = json.dumps(result.to_dict())
    for secret in _environment().values():
        assert secret not in serialized_metadata


def test_unchanged_sync_reuses_the_current_version() -> None:
    client = _SecretsManager(
        {
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OPENAI_API_KEY": "openai-secret",
        }
    )

    result = synchronize_provider_secret(
        client,
        secret_arn=SECRET_ARN,
        environ=_environment(),
        enabled_providers=["anthropic", "openai"],
    )

    assert result.changed is False
    assert result.version_id == "version-1"
    assert result.previous_version_id is None
    assert client.put_calls == []


def test_rollback_moves_current_and_returns_only_metadata() -> None:
    client = _SecretsManager({"OPENAI_API_KEY": "old-secret"})
    client.versions["version-2"] = {"OPENAI_API_KEY": "new-secret"}
    client.current = "version-2"

    result = rollback_provider_secret(
        client,
        secret_arn=SECRET_ARN,
        version_id="version-1",
        enabled_providers=["openai"],
    )

    assert result.changed is True
    assert result.version_id == "version-1"
    assert result.previous_version_id == "version-2"
    assert client.current == "version-1"
    assert client.update_calls[0]["MoveToVersionId"] == "version-1"
    serialized_metadata = json.dumps(result.to_dict())
    assert "old-secret" not in serialized_metadata
    assert "new-secret" not in serialized_metadata


def test_sync_replaces_bootstrap_placeholder_when_no_key_is_needed() -> None:
    client = _SecretsManager({"placeholder": "generated"})

    result = synchronize_provider_secret(
        client,
        secret_arn=SECRET_ARN,
        environ={},
        enabled_providers=["bedrock"],
    )

    assert result.changed is True
    assert client.versions["version-2"] == {}


def test_rollback_validates_target_before_moving_current() -> None:
    client = _SecretsManager({"OPENAI_API_KEY": "current-secret"})
    client.versions["version-0"] = {"placeholder": "generated"}

    with pytest.raises(
        ProviderSecretError,
        match="unsupported fields",
    ):
        rollback_provider_secret(
            client,
            secret_arn=SECRET_ARN,
            version_id="version-0",
            enabled_providers=["openai"],
        )

    assert client.current == "version-1"
    assert client.update_calls == []


def test_rollback_rejects_credentials_for_disabled_providers() -> None:
    client = _SecretsManager({"OPENAI_API_KEY": "current-secret"})
    client.versions["version-0"] = {
        "ANTHROPIC_API_KEY": "disabled-secret",
        "OPENAI_API_KEY": "reviewed-secret",
    }

    with pytest.raises(
        ProviderSecretError,
        match="disabled providers",
    ):
        rollback_provider_secret(
            client,
            secret_arn=SECRET_ARN,
            version_id="version-0",
            enabled_providers=["openai"],
        )

    assert client.current == "version-1"
    assert client.update_calls == []
