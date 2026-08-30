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

    def test_relative_base_seed_merges_and_tenantizes_rows(
        self,
        tmp_path: Path,
    ):
        (tmp_path / "base.yaml").write_text(
            textwrap.dedent(
                """\
                projects:
                  - project_id: proj-alpha
                    name: Acme
                unhealthy_providers:
                  - provider: example
                """
            )
        )
        (tmp_path / "child.yaml").write_text(
            textwrap.dedent(
                """\
                base_seed: base.yaml
                default_tenant_id: tenant-acme
                tenants:
                  - tenant_id: tenant-acme
                projects:
                  - tenant_id: tenant-globex
                    project_id: proj-alpha
                    name: Globex
                """
            )
        )

        result = load_demo_seed_config(str(tmp_path / "child.yaml"))

        assert result.default_tenant_id == "tenant-acme"
        assert result.tenants == [{"tenant_id": "tenant-acme"}]
        assert [
            (project["tenant_id"], project["name"])
            for project in result.projects
        ] == [
            ("tenant-acme", "Acme"),
            ("tenant-globex", "Globex"),
        ]
        assert result.unhealthy_providers == [{"provider": "example"}]

    def test_base_seed_must_be_relative(self, tmp_path: Path):
        seed_yaml = tmp_path / "seed.yaml"
        seed_yaml.write_text(f"base_seed: {tmp_path / 'base.yaml'}\n")

        with pytest.raises(ValueError, match="relative path"):
            load_demo_seed_config(str(seed_yaml))

    def test_missing_base_seed_is_rejected(self, tmp_path: Path):
        seed_yaml = tmp_path / "seed.yaml"
        seed_yaml.write_text("base_seed: missing.yaml\n")

        with pytest.raises(ValueError, match="base_seed not found"):
            load_demo_seed_config(str(seed_yaml))

    def test_base_seed_cycle_is_rejected(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text("base_seed: b.yaml\n")
        (tmp_path / "b.yaml").write_text("base_seed: a.yaml\n")

        with pytest.raises(ValueError, match="cycle"):
            load_demo_seed_config(str(tmp_path / "a.yaml"))

    def test_seed_sections_require_lists_of_mappings(self, tmp_path: Path):
        seed_yaml = tmp_path / "seed.yaml"
        seed_yaml.write_text("projects: null\n")

        with pytest.raises(ValueError, match="projects"):
            load_demo_seed_config(str(seed_yaml))


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
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")

        config = load_app_config()
        assert config.aws_region == "us-east-1"
        assert config.bedrock_region == "us-east-1"
        assert config.server_host == "0.0.0.0"
        assert config.server_port == 8000
        assert config.models_config_path == "config/models.yaml"
        assert config.enabled_providers is None
        assert config.load_demo_data is False
        assert config.alb_signer_arn == ""
        assert config.alb_client_id == ""
        assert config.alb_issuer == ""
        assert config.oidc_tenant_claim == "custom:tenant_id"
        assert config.oidc_project_claim == "custom:project_id"
        assert config.routing_config_signing_mode == "disabled"
        assert config.routing_config_signing_key_arn == ""

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        monkeypatch.setenv("AXON_SERVER_PORT", "9000")
        monkeypatch.setenv("AXON_LOAD_DEMO_DATA", "true")
        signer_arn = (
            "arn:aws:elasticloadbalancing:eu-west-1:123456789012:"
            "loadbalancer/app/axon-prod/50dc6c495c0c9188"
        )
        monkeypatch.setenv("AXON_ALB_SIGNER_ARN", signer_arn)
        monkeypatch.setenv("AXON_ALB_CLIENT_ID", "client-123")
        monkeypatch.setenv("AXON_OIDC_TENANT_CLAIM", "tenant")
        monkeypatch.setenv("AXON_OIDC_PROJECT_CLAIM", "project")
        monkeypatch.setenv(
            "AXON_ENABLED_PROVIDERS",
            "bedrock, openai",
        )
        monkeypatch.setenv(
            "AXON_ALB_ISSUER",
            "https://public-keys.auth.elb.eu-west-1.amazonaws.com",
        )

        config = load_app_config()
        assert config.aws_region == "eu-west-1"
        assert config.server_port == 9000
        assert config.load_demo_data is True
        assert config.alb_signer_arn == signer_arn
        assert config.alb_client_id == "client-123"
        assert config.oidc_tenant_claim == "tenant"
        assert config.oidc_project_claim == "project"
        assert config.enabled_providers == frozenset({"bedrock", "openai"})
        assert (
            config.alb_issuer
            == "https://public-keys.auth.elb.eu-west-1.amazonaws.com"
        )

    @pytest.mark.parametrize(
        "value",
        ["", "unknown", "bedrock,unknown"],
    )
    def test_invalid_enabled_provider_allowlist_fails_closed(
        self,
        monkeypatch,
        value,
    ):
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
        monkeypatch.setenv("AXON_ENABLED_PROVIDERS", value)

        with pytest.raises(ValueError, match="PROVIDERS|providers"):
            load_app_config()

    def test_routing_signing_configuration_is_exact_and_explicit(
        self,
        monkeypatch,
    ):
        key_arn = (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/11111111-2222-3333-4444-555555555555"
        )
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
        monkeypatch.setenv(
            "AXON_ROUTING_CONFIG_SIGNING_MODE",
            "verify",
        )
        monkeypatch.setenv(
            "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN",
            key_arn,
        )

        config = load_app_config()

        assert config.routing_config_signing_mode == "verify"
        assert config.routing_config_signing_key_arn == key_arn

    @pytest.mark.parametrize(
        ("mode", "key_arn", "message"),
        [
            ("invalid", "", "signing_mode"),
            ("verify", "", "exact KMS key ARN"),
            ("verify", "alias/axonllm", "full KMS key ARN"),
            (
                "verify",
                (
                    "arn:aws:kms:us-west-2:123456789012:"
                    "key/11111111-2222-3333-4444-555555555555"
                ),
                "region",
            ),
        ],
    )
    def test_invalid_routing_signing_configuration_fails_closed(
        self,
        monkeypatch,
        mode,
        key_arn,
        message,
    ):
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv(
            "AXON_ROUTING_CONFIG_SIGNING_MODE",
            mode,
        )
        monkeypatch.setenv(
            "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN",
            key_arn,
        )

        with pytest.raises((RuntimeError, ValueError), match=message):
            load_app_config()


class TestSemanticCacheConfig:
    """The threshold parser, whose failure mode is a working-looking cache.

    A threshold of 0 makes every cached entry a match, so the project starts
    answering unrelated questions with whatever it stored first. That presents
    as a suspiciously good hit rate rather than as an error, which is why a bad
    value falls back to the module default instead of to a number.
    """

    def _clean(self, monkeypatch):
        for key in list(os.environ):
            if key.startswith("AXON_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AXON_DEPLOYMENT_PROFILE", "development")

    def test_the_cache_is_off_unless_asked_for(self, monkeypatch):
        self._clean(monkeypatch)
        config = load_app_config()
        assert config.semantic_cache_enabled is False
        assert config.semantic_cache_threshold is None

    def test_enabling_and_setting_a_threshold(self, monkeypatch):
        self._clean(monkeypatch)
        monkeypatch.setenv("AXON_SEMANTIC_CACHE", "true")
        monkeypatch.setenv("AXON_SEMANTIC_CACHE_THRESHOLD", "0.9")
        config = load_app_config()
        assert config.semantic_cache_enabled is True
        assert config.semantic_cache_threshold == 0.9

    @pytest.mark.parametrize("raw", ["abc", "", "0", "0.0", "-0.5", "1.5", "95"])
    def test_an_unusable_threshold_falls_back_to_none_not_to_zero(self, monkeypatch, raw):
        """"95" is in the list deliberately: a percentage typed where a fraction
        belongs is the likeliest real typo, and it must not be accepted."""
        self._clean(monkeypatch)
        monkeypatch.setenv("AXON_SEMANTIC_CACHE_THRESHOLD", raw)
        assert load_app_config().semantic_cache_threshold is None

    def test_a_threshold_of_exactly_one_is_allowed(self, monkeypatch):
        """1.0 means "identical embeddings only" — restrictive, not invalid."""
        self._clean(monkeypatch)
        monkeypatch.setenv("AXON_SEMANTIC_CACHE_THRESHOLD", "1.0")
        assert load_app_config().semantic_cache_threshold == 1.0

    def test_the_embedding_region_defaults_to_the_bedrock_region(self, monkeypatch):
        """The embedder talks to Bedrock, so a deploy that moved Bedrock to
        another region should not silently keep embedding in us-east-1."""
        self._clean(monkeypatch)
        monkeypatch.setenv("AXON_BEDROCK_REGION", "eu-west-1")
        assert load_app_config().semantic_cache_region == "eu-west-1"

    def test_the_embedding_region_can_be_set_independently(self, monkeypatch):
        self._clean(monkeypatch)
        monkeypatch.setenv("AXON_BEDROCK_REGION", "eu-west-1")
        monkeypatch.setenv("AXON_SEMANTIC_CACHE_REGION", "us-west-2")
        assert load_app_config().semantic_cache_region == "us-west-2"


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
            projects=[
                {
                    "tenant_id": "tenant-a",
                    "project_id": "p1",
                    "name": "P1",
                }
            ],
            user_budgets=[
                {
                    "tenant_id": "tenant-a",
                    "user_id": "u1",
                    "budget_limit": 50.0,
                }
            ],
            usage_seeds=[],
            policies=[{"tenant_id": "tenant-a", "name": "pol1"}],
            unhealthy_providers=[],
            default_tenant_id="tenant-a",
            tenants=[{"tenant_id": "tenant-a"}],
        )
        serialized = serialize_demo_seed_config(original)
        yaml_path = tmp_path / "seed.yaml"
        yaml_path.write_text(yaml.dump(serialized))
        reloaded = load_demo_seed_config(str(yaml_path))

        assert reloaded.projects == original.projects
        assert reloaded.user_budgets == original.user_budgets
        assert reloaded.policies == original.policies
        assert reloaded.default_tenant_id == original.default_tenant_id
        assert reloaded.tenants == original.tenants
