"""Unit tests for ModelLeaderboard."""

import logging

import pytest

from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.models import ModelScore


VALID_YAML = """\
task_types:
  coding:
    models:
      - name: claude-opus
        score: 95
      - name: gpt-4o
        score: 90
      - name: claude-sonnet
        score: 85
  reasoning:
    models:
      - name: claude-opus
        score: 97
      - name: deepseek-r1
        score: 90
smart_routing:
  confidence_threshold: 0.3
  cost_quality_tradeoff: 0.3
  default_model: claude-sonnet
"""

VALID_MODELS = {"claude-opus", "gpt-4o", "claude-sonnet", "deepseek-r1"}


class TestFromYaml:
    def test_load_valid_yaml(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        rankings = lb.get_rankings("coding")
        assert len(rankings) == 3
        assert rankings[0].model_name == "claude-opus"
        assert rankings[0].score == 95

    def test_rankings_sorted_descending(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        rankings = lb.get_rankings("coding")
        scores = [r.score for r in rankings]
        assert scores == sorted(scores, reverse=True)

    def test_unknown_task_type_returns_empty(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        assert lb.get_rankings("unknown_type") == []

    def test_smart_routing_config_loaded(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        assert lb.config["confidence_threshold"] == 0.3
        assert lb.config["default_model"] == "claude-sonnet"


class TestGetScore:
    def test_get_score_existing(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        score = lb.get_score("coding", "claude-opus")
        assert score == 95

    def test_get_score_nonexistent_model(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        score = lb.get_score("coding", "nonexistent-model")
        assert score is None

    def test_get_score_nonexistent_task_type(self):
        lb = ModelLeaderboard.from_yaml(VALID_YAML, VALID_MODELS)
        score = lb.get_score("unknown", "claude-opus")
        assert score is None


class TestUnknownModels:
    def test_unknown_model_skipped_with_warning(self, caplog):
        yaml_str = """\
task_types:
  coding:
    models:
      - name: claude-opus
        score: 95
      - name: unknown-model
        score: 80
"""
        valid = {"claude-opus"}
        with caplog.at_level(logging.WARNING):
            lb = ModelLeaderboard.from_yaml(yaml_str, valid)

        rankings = lb.get_rankings("coding")
        assert len(rankings) == 1
        assert rankings[0].model_name == "claude-opus"
        assert "unknown-model" in caplog.text

    def test_no_valid_models_filter_accepts_all(self):
        yaml_str = """\
task_types:
  coding:
    models:
      - name: any-model
        score: 80
"""
        lb = ModelLeaderboard.from_yaml(yaml_str, valid_models=None)
        rankings = lb.get_rankings("coding")
        assert len(rankings) == 1
        assert rankings[0].model_name == "any-model"


class TestMalformedYaml:
    def test_malformed_yaml_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR):
            lb = ModelLeaderboard.from_yaml("{{invalid yaml: [", None)
        assert lb.get_rankings("coding") == []
        assert "Malformed" in caplog.text or "YAML" in caplog.text

    def test_non_dict_yaml_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR):
            lb = ModelLeaderboard.from_yaml("- just a list", None)
        assert lb.get_rankings("coding") == []

    def test_missing_task_types_section(self, caplog):
        with caplog.at_level(logging.WARNING):
            lb = ModelLeaderboard.from_yaml("other_key: value", None)
        assert lb.get_rankings("coding") == []


class TestLoadFromFile:
    def test_load_missing_file(self, caplog, tmp_path):
        lb = ModelLeaderboard()
        with caplog.at_level(logging.ERROR):
            lb.load(str(tmp_path / "nonexistent.yaml"), VALID_MODELS)
        assert lb.get_rankings("coding") == []
        assert "not found" in caplog.text

    def test_load_valid_file(self, tmp_path):
        config_file = tmp_path / "leaderboard.yaml"
        config_file.write_text(VALID_YAML)
        lb = ModelLeaderboard()
        lb.load(str(config_file), VALID_MODELS)
        rankings = lb.get_rankings("coding")
        assert len(rankings) == 3
        assert rankings[0].model_name == "claude-opus"
