"""Unit tests for src.gateway.config_loader."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from src.gateway.config import AppConfig
from src.gateway.config_loader import (
    DemoSeedData,
    load_app_config,
    load_catalog_config,
    load_demo_seed_config,
    load_pricing_config,
    serialize_demo_seed_config,
    serialize_pricing_config,
)
from src.gateway.models import TokenPricing


# ---------------------------------------------------------------------------
# load_pricing_config
# ---------------------------------------------------------------------------


class TestLoadPricingConfig:
    def test_valid_pricing(self, tmp_path: Path):
        pricing_yaml = tmp_path / "pricing.yaml"
        pricing_yaml.write_text(textwrap.dedent("""\
            providers:
              openai:
                gpt-4:
                  prompt_token_cost: 0.03
                  completion_token_cost: 0.06
                gpt-3.5-turbo:
                  prompt_token_cost: 0.001
                  completion_token_cost: 0.002
              anthropic:
                claude-3:
                  prompt_token_cost: 0.003
                  completion_token_cost: 0.015
        """))
        result = load_pricing_config(str(pricing_yaml))
        assert "openai" in result
        assert "anthropic" in result
        assert isinstance(result["openai"]["gpt-4"], TokenPricing)
        assert result["openai"]["gpt-4"].prompt_token_cost == 0.03
        assert result["openai"]["gpt-4"].completion_token_cost == 0.06

    def test_missing_file_returns_empty(self, tmp_path: Path):
        result = load_pricing_config(str(tmp_path / "nonexistent.yaml"))
        assert result == {}

    def test_malformed_entry_skipped(self, tmp_path: Path):
        pricing_yaml = tmp_path / "pricing.yaml"
        pricing_yaml.write_text(textwrap.dedent("""\
            providers:
              openai:
                gpt-4:
                  prompt_token_cost: 0.03
                bad-model:
                  prompt_token_cost: 0.01
        """))
        result = load_pricing_config(str(pricing_yaml))
        assert "gpt-4" not in result.get("openai", {})  # gpt-4 also missing completion_token_cost
        # Both entries are missing completion_token_cost, so openai should be empty or absent
        assert result.get("openai", {}).get("bad-model") is None

    def test_optional_defaults(self, tmp_path: Path):
        pricing_yaml = tmp_path / "pricing.yaml"
        pricing_yaml.write_text(textwrap.dedent("""\
            providers:
              openai:
                gpt-4:
                  prompt_token_cost: 0.03
                  completion_token_cost: 0.06
        """))
        result = load_pricing_config(str(pricing_yaml))
        tp = result["openai"]["gpt-4"]
        assert tp.cached_token_cost is None
        assert tp.image_token_cost is None
        assert tp.reasoning_token_cost is None
        assert tp.per_request_cost == 0.0

    def test_optional_fields_set(self, tmp_path: Path):
        pricing_yaml = tmp_path / "pricing.yaml"
        pricing_yaml.write_text(textwrap.dedent("""\
            providers:
              openai:
                gpt-4:
                  prompt_token_cost: 0.03
                  completion_token_cost: 0.06
                  cached_token_cost: 0.01
                  image_token_cost: 0.02
                  reasoning_token_cost: 0.04
                  per_request_cost: 0.001
        """))
        result = load_pricing_config(str(pricing_yaml))
        tp = result["openai"]["gpt-4"]
        assert tp.cached_token_cost == 0.01
        assert tp.image_token_cost == 0.02
        assert tp.reasoning_token_cost == 0.04
        assert tp.per_request_cost == 0.001


# ---------------------------------------------------------------------------
# load_demo_seed_config
# ---------------------------------------------------------------------------


class TestLoadDemoSeedConfig:
    def test_valid_seed(self, tmp_path: Path):
        seed_yaml = tmp_path / "seed.yaml"
        seed_yaml.write_text(textwrap.dedent("""\
            projects:
              - project_id: proj-alpha
                name: Alpha
                budget_limit: 500.0
            user_budgets:
              - user_id: alice
                budget_limit: 50.0
            usage_seeds:
              - project_id: proj-alpha
                user_id: alice
                provider: openai
                model: gpt-4
                prompt_tokens: 100
                completion_tokens: 50
                cost: 0.01
            policies:
              - name: test-policy
                description: Test
                policy_text: "permit all"
                mode: ENFORCE
            unhealthy_providers:
              - provider: azure_openai
                cooldown_seconds: 600
        """))
        result = load_demo_seed_config(str(seed_yaml))
        assert len(result.projects) == 1
        assert result.projects[0]["project_id"] == "proj-alpha"
        assert len(result.user_budgets) == 1
        assert len(result.usage_seeds) == 1
        assert len(result.policies) == 1
        assert len(result.unhealthy_providers) == 1

    def test_missing_file_returns_empty(self, tmp_path: Path):
        result = load_demo_seed_config(str(tmp_path / "nonexistent.yaml"))
        assert result.projects == []
        assert result.user_budgets == []
        assert result.usage_seeds == []
        assert result.policies == []
        assert result.unhealthy_providers == []


# ---------------------------------------------------------------------------
# load_catalog_config
# ---------------------------------------------------------------------------


class TestLoadCatalogConfig:
    def test_valid_catalog(self, tmp_path: Path):
        catalog_yaml = tmp_path / "catalog.yaml"
        catalog_yaml.write_text(textwrap.dedent("""\
            providers:
              openai:
                display_name: OpenAI
                auth_type: api_key
                models:
                  - model_id: gpt-4o
                    name: GPT-4o
                    capabilities: [chat, vision]
        """))
        result = load_catalog_config(str(catalog_yaml))
        assert "openai" in result
        assert result["openai"]["display_name"] == "OpenAI"

    def test_missing_file_returns_fallback(self, tmp_path: Path):
        fallback = {"test": "data"}
        result = load_catalog_config(str(tmp_path / "nonexistent.yaml"), fallback=fallback)
        assert result == fallback

    def test_missing_file_no_fallback(self, tmp_path: Path):
        result = load_catalog_config(str(tmp_path / "nonexistent.yaml"))
        assert result == {}


# ---------------------------------------------------------------------------
# load_app_config
# ---------------------------------------------------------------------------


class TestLoadAppConfig:
    def test_defaults(self, monkeypatch):
        # Clear all AXON_ env vars
        for key in list(os.environ):
            if key.startswith("AXON_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        config = load_app_config()
        assert config.aws_region == "us-east-1"
        assert config.bedrock_region == "us-east-1"
        assert config.server_host == "0.0.0.0"
        assert config.server_port == 8000
        assert config.models_config_path == "config/models.yaml"
        assert config.load_demo_data is False

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        monkeypatch.setenv("AXON_SERVER_PORT", "9000")
        monkeypatch.setenv("AXON_LOAD_DEMO_DATA", "true")

        config = load_app_config()
        assert config.aws_region == "eu-west-1"
        assert config.server_port == 9000
        assert config.load_demo_data is True


# ---------------------------------------------------------------------------
# Serialization round-trips
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_pricing_round_trip(self, tmp_path: Path):
        original = {
            "openai": {
                "gpt-4": TokenPricing(0.03, 0.06),
                "gpt-3.5": TokenPricing(0.001, 0.002, cached_token_cost=0.0005),
            }
        }
        serialized = serialize_pricing_config(original)
        yaml_path = tmp_path / "pricing.yaml"
        yaml_path.write_text(yaml.dump(serialized))
        reloaded = load_pricing_config(str(yaml_path))

        for provider in original:
            for model in original[provider]:
                orig_tp = original[provider][model]
                reload_tp = reloaded[provider][model]
                assert orig_tp.prompt_token_cost == reload_tp.prompt_token_cost
                assert orig_tp.completion_token_cost == reload_tp.completion_token_cost
                assert orig_tp.cached_token_cost == reload_tp.cached_token_cost
                assert orig_tp.per_request_cost == reload_tp.per_request_cost

    def test_demo_seed_round_trip(self, tmp_path: Path):
        original = DemoSeedData(
            projects=[{"project_id": "p1", "name": "P1"}],
            user_budgets=[{"user_id": "u1", "budget_limit": 50.0}],
            usage_seeds=[],
            policies=[{"name": "pol1"}],
            unhealthy_providers=[],
        )
        serialized = serialize_demo_seed_config(original)
        yaml_path = tmp_path / "seed.yaml"
        yaml_path.write_text(yaml.dump(serialized))
        reloaded = load_demo_seed_config(str(yaml_path))

        assert reloaded.projects == original.projects
        assert reloaded.user_budgets == original.user_budgets
        assert reloaded.policies == original.policies
