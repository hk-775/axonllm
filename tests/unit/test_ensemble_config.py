"""Unit tests for EnsembleConfig loader."""

import logging

import pytest

from src.gateway.ensemble import EnsembleConfigError
from src.gateway.ensemble_config import EnsembleConfig


VALID_YAML = """\
ensemble:
  default_preset: budget
  presets:
    budget:
      panel:
        - llama-4-scout
        - qwen3-235b
        - deepseek-r1
      judge: claude-sonnet
      quorum: 2
      fallback_policy: best-single
      cost_ceiling: 0.50
      ranking_criteria: length
    quality:
      panel:
        - llama-3.3-70b
        - mistral-large
        - deepseek-r1
        - gpt-oss-120b
      judge: claude-opus
      quorum: 1
      fallback_policy: error
      cost_ceiling: null
      ranking_criteria: length
"""


class TestFromYaml:
    def test_loads_both_presets(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        assert set(config.presets.keys()) == {"budget", "quality"}

    def test_budget_preset_fields(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        budget = config.get_preset("budget")
        assert budget is not None
        assert budget.name == "budget"
        assert budget.panel == ["llama-4-scout", "qwen3-235b", "deepseek-r1"]
        assert budget.judge == "claude-sonnet"
        assert budget.quorum == 2
        assert budget.fallback_policy == "best-single"
        assert budget.cost_ceiling == 0.50
        assert budget.ranking_criteria == "length"

    def test_quality_preset_fields(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        quality = config.get_preset("quality")
        assert quality is not None
        assert quality.name == "quality"
        assert quality.panel == [
            "llama-3.3-70b",
            "mistral-large",
            "deepseek-r1",
            "gpt-oss-120b",
        ]
        assert quality.judge == "claude-opus"
        assert quality.quorum == 1
        assert quality.fallback_policy == "error"
        assert quality.cost_ceiling is None
        assert quality.ranking_criteria == "length"


class TestParseTimeDefaults:
    def test_missing_quorum_defaults_to_one(self):
        yaml_str = """\
ensemble:
  default_preset: solo
  presets:
    solo:
      panel:
        - model-a
        - model-b
      judge: judge-model
      fallback_policy: error
"""
        config = EnsembleConfig.from_yaml(yaml_str)
        preset = config.get_preset("solo")
        assert preset is not None
        assert preset.quorum == 1

    def test_missing_fallback_policy_defaults_to_error(self):
        yaml_str = """\
ensemble:
  default_preset: solo
  presets:
    solo:
      panel:
        - model-a
        - model-b
      judge: judge-model
      quorum: 1
"""
        config = EnsembleConfig.from_yaml(yaml_str)
        preset = config.get_preset("solo")
        assert preset is not None
        assert preset.fallback_policy == "error"


class TestAccessors:
    def test_get_preset_named(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        assert config.get_preset("quality").name == "quality"

    def test_get_preset_unknown_returns_none(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        assert config.get_preset("nonexistent") is None

    def test_default_preset_returns_configured_default(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        default = config.default_preset()
        assert default is not None
        assert default.name == "budget"

    def test_presets_accessor_returns_all(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        presets = config.presets
        assert len(presets) == 2
        assert presets["budget"].name == "budget"

    def test_is_configured_true_when_presets_loaded(self):
        config = EnsembleConfig.from_yaml(VALID_YAML)
        assert config.is_configured is True

    def test_default_preset_none_when_unset(self):
        yaml_str = """\
ensemble:
  presets:
    solo:
      panel:
        - model-a
      judge: judge-model
"""
        config = EnsembleConfig.from_yaml(yaml_str)
        assert config.default_preset() is None


class TestInvalidPreset:
    def test_invalid_quorum_raises_identifying_preset(self):
        # quorum 5 exceeds panel size 2 → invalid
        yaml_str = """\
ensemble:
  presets:
    bad:
      panel:
        - model-a
        - model-b
      judge: judge-model
      quorum: 5
"""
        with pytest.raises(EnsembleConfigError) as exc_info:
            EnsembleConfig.from_yaml(yaml_str)
        assert "bad" in str(exc_info.value)

    def test_invalid_fallback_policy_raises(self):
        yaml_str = """\
ensemble:
  presets:
    bogus:
      panel:
        - model-a
      judge: judge-model
      fallback_policy: surrender
"""
        with pytest.raises(EnsembleConfigError) as exc_info:
            EnsembleConfig.from_yaml(yaml_str)
        assert "bogus" in str(exc_info.value)


class TestMissingFile:
    def test_missing_file_empty_config_and_warning(self, caplog, tmp_path):
        config = EnsembleConfig()
        with caplog.at_level(logging.WARNING):
            config.load(str(tmp_path / "nonexistent.yaml"))
        assert config.presets == {}
        assert config.is_configured is False
        assert config.default_preset() is None
        assert "not found" in caplog.text

    def test_missing_file_raises_no_exception(self, tmp_path):
        config = EnsembleConfig()
        # Should not raise
        config.load(str(tmp_path / "nope.yaml"))
        assert config.is_configured is False


class TestLoadFromFile:
    def test_load_valid_file(self, tmp_path):
        config_file = tmp_path / "ensemble.yaml"
        config_file.write_text(VALID_YAML)
        config = EnsembleConfig()
        config.load(str(config_file))
        assert config.is_configured is True
        assert set(config.presets.keys()) == {"budget", "quality"}
        assert config.default_preset().name == "budget"
