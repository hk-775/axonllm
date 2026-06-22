"""Unit tests for ModelRegistry."""

import tempfile
from pathlib import Path

import pytest
import yaml

from src.gateway.model_registry import VALID_PROVIDERS, ModelRegistry


# --- Fixtures ---

MINIMAL_VALID_YAML = """\
virtual_models:
  - name: "test-model"
    description: "A test model"
    providers:
      - provider: "openai"
        model_id: "gpt-4"
"""

FULL_VALID_YAML = """\
virtual_models:
  - name: "gpt-4"
    description: "GPT-4 class model"
    routing_strategy: "weighted"
    capabilities: ["chat", "streaming", "function_calling"]
    providers:
      - provider: "openai"
        model_id: "gpt-4-turbo"
        weight: 0.7
        fallback_order: 1
        pricing:
          prompt_token_cost: 0.01
          completion_token_cost: 0.03
      - provider: "azure_openai"
        model_id: "gpt-4-turbo-2024"
        weight: 0.3
        fallback_order: 2
        pricing:
          prompt_token_cost: 0.01
          completion_token_cost: 0.03
  - name: "claude-3"
    description: "Claude 3 class model"
    routing_strategy: "cost-optimized"
    capabilities: ["chat", "streaming"]
    providers:
      - provider: "anthropic"
        model_id: "claude-3-sonnet-20240229"
        fallback_order: 1
        pricing:
          prompt_token_cost: 0.003
          completion_token_cost: 0.015
"""


# --- from_yaml / load ---


class TestFromYaml:
    def test_minimal_valid(self):
        reg = ModelRegistry.from_yaml(MINIMAL_VALID_YAML)
        assert len(reg.models) == 1
        assert "test-model" in reg.models
        m = reg.models["test-model"]
        assert m.name == "test-model"
        assert m.description == "A test model"
        assert len(m.providers) == 1
        assert m.providers[0].provider == "openai"
        assert m.providers[0].model_id == "gpt-4"

    def test_full_valid(self):
        reg = ModelRegistry.from_yaml(FULL_VALID_YAML)
        assert len(reg.models) == 2
        gpt4 = reg.models["gpt-4"]
        assert gpt4.routing_strategy.value == "weighted"
        assert gpt4.capabilities == ["chat", "streaming", "function_calling"]
        assert len(gpt4.providers) == 2
        assert gpt4.providers[0].weight == 0.7
        assert gpt4.providers[0].pricing is not None
        assert gpt4.providers[0].pricing.prompt_token_cost == 0.01

    def test_defaults(self):
        reg = ModelRegistry.from_yaml(MINIMAL_VALID_YAML)
        m = reg.models["test-model"]
        assert m.routing_strategy.value == "round-robin"
        assert m.capabilities is None
        p = m.providers[0]
        assert p.weight == 1.0
        assert p.fallback_order == 0
        assert p.pricing is None

    def test_empty_yaml(self):
        reg = ModelRegistry.from_yaml("")
        assert len(reg.models) == 0

    def test_empty_virtual_models_list(self):
        reg = ModelRegistry.from_yaml("virtual_models: []")
        assert len(reg.models) == 0


class TestLoad:
    def test_load_from_file(self, tmp_path: Path):
        config_file = tmp_path / "models.yaml"
        config_file.write_text(MINIMAL_VALID_YAML)
        reg = ModelRegistry()
        reg.load(str(config_file))
        assert "test-model" in reg.models


# --- resolve ---


class TestResolve:
    def test_resolve_existing(self):
        reg = ModelRegistry.from_yaml(FULL_VALID_YAML)
        providers = reg.resolve("gpt-4")
        assert len(providers) == 2
        assert providers[0].provider == "openai"
        assert providers[1].provider == "azure_openai"

    def test_resolve_unknown_raises(self):
        reg = ModelRegistry.from_yaml(FULL_VALID_YAML)
        with pytest.raises(KeyError, match="Unknown virtual model"):
            reg.resolve("nonexistent-model")

    def test_resolve_preserves_all_fields(self):
        reg = ModelRegistry.from_yaml(FULL_VALID_YAML)
        providers = reg.resolve("gpt-4")
        p = providers[0]
        assert p.provider == "openai"
        assert p.model_id == "gpt-4-turbo"
        assert p.weight == 0.7
        assert p.fallback_order == 1
        assert p.pricing is not None
        assert p.pricing.prompt_token_cost == 0.01
        assert p.pricing.completion_token_cost == 0.03


# --- list_models ---


class TestListModels:
    def test_list_models(self):
        reg = ModelRegistry.from_yaml(FULL_VALID_YAML)
        models = reg.list_models()
        assert len(models) == 2
        names = {m.name for m in models}
        assert names == {"gpt-4", "claude-3"}

    def test_list_models_empty(self):
        reg = ModelRegistry()
        assert reg.list_models() == []


# --- validate ---


class TestValidate:
    def _validate(self, config: dict) -> list:
        return ModelRegistry().validate(config)

    def test_valid_config_no_errors(self):
        config = yaml.safe_load(FULL_VALID_YAML)
        errors = self._validate(config)
        assert errors == []

    def test_missing_virtual_models_key(self):
        errors = self._validate({})
        assert len(errors) == 1
        assert "virtual_models" in errors[0].field

    def test_missing_name(self):
        config = {"virtual_models": [{"description": "x", "providers": [{"provider": "openai", "model_id": "m"}]}]}
        errors = self._validate(config)
        assert any("name" in e.field for e in errors)

    def test_missing_description(self):
        config = {"virtual_models": [{"name": "x", "providers": [{"provider": "openai", "model_id": "m"}]}]}
        errors = self._validate(config)
        assert any("description" in e.field for e in errors)

    def test_missing_providers(self):
        config = {"virtual_models": [{"name": "x", "description": "d"}]}
        errors = self._validate(config)
        assert any("providers" in e.field for e in errors)

    def test_empty_providers(self):
        config = {"virtual_models": [{"name": "x", "description": "d", "providers": []}]}
        errors = self._validate(config)
        assert any("empty" in e.message.lower() for e in errors)

    def test_invalid_provider_name(self):
        config = {"virtual_models": [{"name": "x", "description": "d", "providers": [{"provider": "invalid_prov", "model_id": "m"}]}]}
        errors = self._validate(config)
        assert any("invalid_prov" in e.message for e in errors)

    def test_missing_model_id(self):
        config = {"virtual_models": [{"name": "x", "description": "d", "providers": [{"provider": "openai"}]}]}
        errors = self._validate(config)
        assert any("model_id" in e.field for e in errors)

    def test_duplicate_names(self):
        config = {"virtual_models": [
            {"name": "dup", "description": "d1", "providers": [{"provider": "openai", "model_id": "m1"}]},
            {"name": "dup", "description": "d2", "providers": [{"provider": "openai", "model_id": "m2"}]},
        ]}
        errors = self._validate(config)
        assert any("duplicate" in e.message.lower() for e in errors)

    def test_invalid_routing_strategy(self):
        config = {"virtual_models": [{"name": "x", "description": "d", "routing_strategy": "invalid", "providers": [{"provider": "openai", "model_id": "m"}]}]}
        errors = self._validate(config)
        assert any("routing strategy" in e.message.lower() for e in errors)

    def test_all_valid_providers_accepted(self):
        for prov in VALID_PROVIDERS:
            config = {"virtual_models": [{"name": f"m-{prov}", "description": "d", "providers": [{"provider": prov, "model_id": "m"}]}]}
            errors = self._validate(config)
            assert errors == [], f"Provider {prov} should be valid but got errors: {errors}"


# --- partial loading ---


class TestPartialLoading:
    def test_valid_entries_loaded_invalid_skipped(self):
        yaml_str = """\
virtual_models:
  - name: "good-model"
    description: "Valid model"
    providers:
      - provider: "openai"
        model_id: "gpt-4"
  - name: "bad-model"
    providers:
      - provider: "openai"
        model_id: "gpt-4"
"""
        reg = ModelRegistry.from_yaml(yaml_str)
        assert "good-model" in reg.models
        assert "bad-model" not in reg.models

    def test_invalid_provider_entry_skipped(self):
        yaml_str = """\
virtual_models:
  - name: "good"
    description: "Valid"
    providers:
      - provider: "openai"
        model_id: "gpt-4"
  - name: "bad"
    description: "Invalid provider"
    providers:
      - provider: "fake_provider"
        model_id: "m"
"""
        reg = ModelRegistry.from_yaml(yaml_str)
        assert "good" in reg.models
        assert "bad" not in reg.models

    def test_duplicate_name_second_skipped(self):
        yaml_str = """\
virtual_models:
  - name: "dup"
    description: "First"
    providers:
      - provider: "openai"
        model_id: "gpt-4"
  - name: "dup"
    description: "Second"
    providers:
      - provider: "anthropic"
        model_id: "claude-3"
"""
        reg = ModelRegistry.from_yaml(yaml_str)
        assert len(reg.models) == 1
        # First one wins
        assert reg.models["dup"].description == "First"


# --- pretty_print ---


class TestPrettyPrint:
    def test_round_trip(self):
        reg1 = ModelRegistry.from_yaml(FULL_VALID_YAML)
        yaml_out = reg1.pretty_print()
        reg2 = ModelRegistry.from_yaml(yaml_out)
        assert len(reg1.models) == len(reg2.models)
        for name in reg1.models:
            m1 = reg1.models[name]
            m2 = reg2.models[name]
            assert m1.name == m2.name
            assert m1.description == m2.description
            assert m1.routing_strategy == m2.routing_strategy
            assert m1.capabilities == m2.capabilities
            assert len(m1.providers) == len(m2.providers)
            for p1, p2 in zip(m1.providers, m2.providers):
                assert p1.provider == p2.provider
                assert p1.model_id == p2.model_id
                assert p1.weight == p2.weight
                assert p1.fallback_order == p2.fallback_order
                if p1.pricing is not None:
                    assert p2.pricing is not None
                    assert p1.pricing.prompt_token_cost == p2.pricing.prompt_token_cost
                    assert p1.pricing.completion_token_cost == p2.pricing.completion_token_cost
                else:
                    assert p2.pricing is None

    def test_pretty_print_produces_valid_yaml(self):
        reg = ModelRegistry.from_yaml(FULL_VALID_YAML)
        yaml_out = reg.pretty_print()
        parsed = yaml.safe_load(yaml_out)
        assert "virtual_models" in parsed
        assert isinstance(parsed["virtual_models"], list)

    def test_empty_registry_pretty_print(self):
        reg = ModelRegistry()
        yaml_out = reg.pretty_print()
        parsed = yaml.safe_load(yaml_out)
        assert parsed["virtual_models"] == []
