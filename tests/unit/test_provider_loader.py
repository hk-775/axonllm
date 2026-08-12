"""Provider metadata and runtime allowlist tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time

import pytest

import src.gateway.provider_loader as provider_loader
from src.gateway.multi_provider_factory import MultiProviderFactory
from src.gateway.models import ProviderModelMapping
from src.gateway.provider_config import (
    ProviderConfig,
    build_provider_stream_url,
    build_provider_url,
    get_auth_headers,
)
from src.gateway.provider_loader import load_provider_configs


def test_secret_free_example_enables_environment_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.yaml.example").write_text(
        """
providers:
  openai:
    base_url: https://api.openai.example
    auth_type: api_key
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "injected-secret")

    configs = load_provider_configs(str(config_dir / "providers.yaml"))

    assert configs["openai"].base_url == "https://api.openai.example"
    assert configs["openai"].credentials == {"api_key": "injected-secret"}


def test_runtime_allowlist_intersects_native_and_configured_providers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  openai:
    base_url: https://api.openai.example
    auth_type: api_key
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "injected-secret")
    configs = load_provider_configs(str(config))

    factory = MultiProviderFactory(
        configs,
        enabled_providers=frozenset({"bedrock", "openai"}),
    )

    assert factory.available_providers == frozenset({"bedrock", "openai"})


class _SecretsManager:
    def __init__(self, value: object) -> None:
        self.value = value

    def get_secret_value(self, *, SecretId: str) -> dict:
        assert SecretId == (
            "arn:aws:secretsmanager:us-east-1:123456789012:"
            "secret:axon-provider"
        )
        return {"SecretString": json.dumps(self.value)}


def _patch_secrets_manager(monkeypatch, client: _SecretsManager) -> dict:
    captured: dict[str, object] = {}

    def client_factory(service: str, *, config) -> _SecretsManager:
        captured.update(service=service, config=config)
        return client

    monkeypatch.setattr(
        provider_loader.boto3,
        "client",
        client_factory,
    )
    return captured


def test_provider_credentials_load_from_secrets_manager(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  openai:
    base_url: https://api.openai.example
    auth_type: api_key
  azure_openai:
    base_url: ""
    auth_type: azure_key
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AXON_PROVIDER_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:123456789012:"
        "secret:axon-provider",
    )
    client = _SecretsManager(
        {
            "OPENAI_API_KEY": "secret-openai",
            "AZURE_OPENAI_API_KEY": "secret-azure",
            "AZURE_OPENAI_ENDPOINT": "https://tenant.openai.azure.com",
            "placeholder": "ignored",
        }
    )
    captured = _patch_secrets_manager(monkeypatch, client)

    configs = load_provider_configs(str(config))

    assert configs["openai"].credentials == {"api_key": "secret-openai"}
    assert configs["azure_openai"].credentials == {
        "api_key": "secret-azure"
    }
    assert (
        configs["azure_openai"].base_url
        == "https://tenant.openai.azure.com"
    )
    assert captured["service"] == "secretsmanager"
    botocore_config = captured["config"]
    assert botocore_config.connect_timeout == 3
    assert botocore_config.read_timeout == 5
    assert botocore_config.retries == {
        "mode": "standard",
        "total_max_attempts": 3,
    }


def test_environment_credentials_override_secret_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  openai:
    base_url: https://api.openai.example
    auth_type: api_key
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AXON_PROVIDER_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:123456789012:"
        "secret:axon-provider",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "environment-openai")
    client = _SecretsManager({"OPENAI_API_KEY": "secret-openai"})
    captured = _patch_secrets_manager(monkeypatch, client)

    configs = load_provider_configs(str(config))

    assert configs["openai"].credentials == {
        "api_key": "environment-openai"
    }
    assert captured["config"] is provider_loader._SECRETS_MANAGER_CONFIG


def test_malformed_provider_secret_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text("providers: {}", encoding="utf-8")
    monkeypatch.setenv(
        "AXON_PROVIDER_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:123456789012:"
        "secret:axon-provider",
    )
    client = _SecretsManager({"OPENAI_API_KEY": ["not", "a", "string"]})
    captured = _patch_secrets_manager(monkeypatch, client)

    with pytest.raises(
        RuntimeError,
        match="Unable to load the configured provider credential secret",
    ):
        load_provider_configs(str(config))
    assert captured["config"] is provider_loader._SECRETS_MANAGER_CONFIG


def test_shipped_example_defines_every_direct_provider(monkeypatch) -> None:
    for env_name in provider_loader._ENV_KEY_MAP.values():
        monkeypatch.setenv(env_name, f"value-for-{env_name}")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://tenant.openai.azure.com",
    )
    monkeypatch.setenv("GCP_PROJECT_ID", "project-a")
    refreshable = type(
        "_Refreshable",
        (),
        {"project_id": None, "get_token": lambda self: "token"},
    )()
    monkeypatch.setattr(
        provider_loader,
        "_load_google_credential_provider",
        lambda _values: refreshable,
    )

    configs = load_provider_configs("config/providers.yaml")

    assert set(configs) == {
        *provider_loader._ENV_KEY_MAP,
        "vertex_ai",
    }


def test_google_ai_key_is_header_only_for_completion_and_streaming() -> None:
    config = ProviderConfig(
        provider_name="google_ai",
        base_url="https://generativelanguage.googleapis.com",
        auth_type="api_key",
        credentials={"api_key": "google-secret"},
    )
    mapping = ProviderModelMapping(
        provider="google_ai",
        model_id="gemini-test",
    )

    completion_url = build_provider_url(config, mapping)
    streaming_url = build_provider_stream_url(config, mapping)

    assert get_auth_headers(config) == {
        "x-goog-api-key": "google-secret"
    }
    assert "google-secret" not in completion_url
    assert "google-secret" not in streaming_url
    assert "?key=" not in completion_url
    assert "key=" not in streaming_url
    assert streaming_url.endswith("?alt=sse")


def test_vertex_credentials_are_refreshed_during_initialization() -> None:
    class _Credentials:
        valid = False
        token = None

        def __init__(self) -> None:
            self.refreshes = 0

        def refresh(self, request) -> None:
            assert request == "bounded-request"
            self.refreshes += 1
            self.token = f"short-lived-{self.refreshes}"
            self.valid = True

    credentials = _Credentials()
    provider = provider_loader.GoogleCredentialProvider(
        credentials,
        request="bounded-request",
        auto_refresh=False,
    )
    config = ProviderConfig(
        provider_name="vertex_ai",
        base_url="https://us-central1-aiplatform.googleapis.com",
        auth_type="gcp_service_account",
        credentials={"credential_source": "google-auth"},
        credential_provider=provider,
    )

    assert get_auth_headers(config) == {
        "Authorization": "Bearer short-lived-1"
    }
    assert credentials.refreshes == 1


@pytest.mark.asyncio
async def test_vertex_expiry_refresh_never_blocks_the_event_loop() -> None:
    refresh_started = threading.Event()
    allow_refresh = threading.Event()
    refresh_finished = threading.Event()
    loop_thread = threading.get_ident()
    refresh_threads: list[int] = []

    class _Credentials:
        valid = True
        token = "initial-token"

        def refresh(self, request) -> None:
            assert request == "bounded-request"
            refresh_threads.append(threading.get_ident())
            refresh_started.set()
            assert allow_refresh.wait(timeout=2)
            self.token = "refreshed-token"
            self.valid = True
            refresh_finished.set()

    credentials = _Credentials()
    provider = provider_loader.GoogleCredentialProvider(
        credentials,
        request="bounded-request",
        auto_refresh=False,
    )
    config = ProviderConfig(
        provider_name="vertex_ai",
        base_url="https://us-central1-aiplatform.googleapis.com",
        auth_type="gcp_service_account",
        credentials={"credential_source": "google-auth"},
        credential_provider=provider,
    )
    credentials.valid = False
    credentials.token = None

    started = time.monotonic()
    with pytest.raises(Exception, match="Unable to refresh"):
        get_auth_headers(config)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert await asyncio.to_thread(refresh_started.wait, 1)
    assert len(refresh_threads) == 1
    assert refresh_threads[0] != loop_thread
    allow_refresh.set()
    assert await asyncio.to_thread(refresh_finished.wait, 1)
    assert get_auth_headers(config) == {
        "Authorization": "Bearer refreshed-token"
    }


def test_vertex_external_account_json_uses_google_auth(
    monkeypatch,
) -> None:
    import google.auth

    captured: dict[str, object] = {}
    credentials = type(
        "_Credentials",
        (),
        {"valid": True, "token": "initial-token"},
    )()

    def load_credentials(payload, *, scopes):
        captured.update(payload=payload, scopes=scopes)
        return credentials, "project-a"

    monkeypatch.setenv(
        "GCP_CREDENTIALS_JSON",
        json.dumps(
            {
                "type": "external_account",
                "audience": (
                    "//iam.googleapis.com/projects/1/locations/global/"
                    "workloadIdentityPools/aws/providers/axon"
                ),
            }
        ),
    )
    monkeypatch.setattr(
        google.auth,
        "load_credentials_from_dict",
        load_credentials,
    )

    provider = provider_loader._load_google_credential_provider({})

    assert provider is not None
    assert provider.project_id == "project-a"
    assert captured["payload"]["type"] == "external_account"
    assert captured["scopes"] == [provider_loader._GOOGLE_CLOUD_SCOPE]


def test_vertex_static_access_token_is_not_accepted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  vertex_ai:
    base_url: https://us-central1-aiplatform.googleapis.com
    auth_type: gcp_service_account
    extra_params:
      project: project-a
      location: us-central1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "static-token")
    monkeypatch.setattr(
        provider_loader,
        "_load_google_credential_provider",
        lambda _values: None,
    )

    assert "vertex_ai" not in load_provider_configs(str(config))
