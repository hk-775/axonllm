"""Provider metadata and runtime allowlist tests."""

from __future__ import annotations

from pathlib import Path

from src.gateway.multi_provider_factory import MultiProviderFactory
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
