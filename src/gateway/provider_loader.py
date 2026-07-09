"""Load provider configurations from YAML + environment variables."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from src.gateway.provider_config import ProviderConfig

# Environment variable names for API keys per provider
_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "google_ai": "GOOGLE_AI_API_KEY",
    "vertex_ai": "GCP_ACCESS_TOKEN",
}


def load_provider_configs(config_path: str = "config/providers.yaml") -> dict[str, ProviderConfig]:
    """Load provider configs from YAML, with env var overrides for API keys.

    Returns a dict of provider_name -> ProviderConfig for providers that
    have valid credentials (either from the YAML file or env vars).
    Providers without credentials are silently skipped.
    """
    configs: dict[str, ProviderConfig] = {}

    # Load YAML if it exists
    path = Path(config_path)
    yaml_providers: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        yaml_providers = raw.get("providers", {})

    for provider_name, provider_data in yaml_providers.items():
        if not isinstance(provider_data, dict):
            continue

        base_url = provider_data.get("base_url", "")
        auth_type = provider_data.get("auth_type", "api_key")

        # Build credentials from YAML + env var override
        credentials = _build_credentials(provider_name, provider_data)
        if not credentials:
            continue  # Skip providers without credentials

        extra_headers = provider_data.get("extra_headers", {})
        extra_params = provider_data.get("extra_params", {})
        connect_timeout = float(provider_data.get("connect_timeout", 30.0))
        read_timeout = float(provider_data.get("read_timeout", 120.0))

        configs[provider_name] = ProviderConfig(
            provider_name=provider_name,
            base_url=base_url,
            auth_type=auth_type,
            credentials=credentials,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            extra_headers=extra_headers,
            extra_params=extra_params,
        )

    return configs


def _build_credentials(provider_name: str, provider_data: dict) -> dict[str, str]:
    """Build credentials dict from YAML data + env var overrides."""
    auth_type = provider_data.get("auth_type", "api_key")

    if auth_type == "api_key":
        env_var = _ENV_KEY_MAP.get(provider_name, "")
        api_key = os.environ.get(env_var, "") or provider_data.get("api_key", "")
        if api_key:
            return {"api_key": api_key}
        return {}

    if auth_type == "azure_key":
        env_var = _ENV_KEY_MAP.get(provider_name, "")
        api_key = os.environ.get(env_var, "") or provider_data.get("api_key", "")
        if api_key:
            return {"api_key": api_key}
        return {}

    if auth_type == "aws_credentials":
        return {
            "access_key": os.environ.get("AWS_ACCESS_KEY_ID", provider_data.get("access_key", "")),
            "secret_key": os.environ.get("AWS_SECRET_ACCESS_KEY", provider_data.get("secret_key", "")),
            "region": os.environ.get("AWS_DEFAULT_REGION", provider_data.get("region", "us-east-1")),
        }

    if auth_type == "gcp_service_account":
        env_var = _ENV_KEY_MAP.get(provider_name, "")
        access_token = os.environ.get(env_var, "") or provider_data.get("access_token", "")
        if access_token:
            return {"access_token": access_token}
        return {}

    return {}
