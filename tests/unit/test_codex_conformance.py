"""Contracts for the executable Codex CLI conformance gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tools/codex_conformance.py"
EXAMPLE_CONFIG = ROOT / "config/codex/config.toml.example"
MODEL_CATALOG = ROOT / "config/codex/model_catalog.json"
LOCAL_DEMO_SCRIPT = ROOT / "scripts/codex_local_demo.sh"


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "codex_conformance",
        HARNESS,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_harness_generates_a_safe_exec_call() -> None:
    harness = _harness()
    name, arguments = harness._safe_tool_call(
        [
            {
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string"},
                            "yield_time_ms": {"type": "integer"},
                        },
                        "required": ["cmd"],
                    },
                },
            }
        ]
    )

    assert name == "exec_command"
    assert arguments == {"cmd": "printf AXONLLM_TOOL_OK"}


def test_codex_harness_uses_the_reviewed_model_catalog() -> None:
    harness = _harness()

    assert harness.MODEL_CATALOG == MODEL_CATALOG


def test_codex_contract_accepts_the_reviewed_transport() -> None:
    harness = _harness()
    request = {
        "model": harness.MODEL,
        "reasoning": {},
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": True,
        "store": False,
        "stream": True,
        "tools": [{"type": "function", "name": "exec_command"}],
        "input": harness.SUCCESS_PROMPT,
    }
    followup = {
        **request,
        "input": [
            harness.SUCCESS_PROMPT,
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "ok",
            },
        ],
    }

    harness._assert_request_contract([request, followup])


def test_codex_contract_rejects_non_function_tools() -> None:
    harness = _harness()
    request = {
        "model": harness.MODEL,
        "reasoning": {},
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": True,
        "store": False,
        "stream": True,
        "tools": [{"type": "web_search"}],
        "input": harness.SUCCESS_PROMPT,
    }

    with pytest.raises(AssertionError, match="non-function"):
        harness._assert_request_contract([request, request])


def test_codex_example_uses_the_reviewed_responses_profile() -> None:
    config = EXAMPLE_CONFIG.read_text()
    assert 'model = "claude-sonnet"' in config
    assert 'model_provider = "axonllm"' in config
    assert "model_catalog_json =" in config
    assert 'wire_api = "responses"' in config
    assert 'env_key = "AXONLLM_API_KEY"' in config
    assert 'requires_openai_auth = false' in config
    assert "model_supports_reasoning_summaries = false" in config
    assert 'model_reasoning_summary = "none"' in config
    assert 'web_search = "disabled"' in config
    assert "multi_agent = false" in config


def test_codex_model_catalog_covers_documented_demo_aliases() -> None:
    import json

    catalog = json.loads(MODEL_CATALOG.read_text())
    models = {model["slug"]: model for model in catalog["models"]}

    assert set(models) == {"claude-sonnet", "claude-haiku"}
    for model in models.values():
        assert model["shell_type"] == "unified_exec"
        assert model["supports_reasoning_summary_parameter"] is False
        assert model["supports_search_tool"] is False
        assert model["use_responses_lite"] is False
        assert model["input_modalities"] == ["text"]
        assert "answer without using tools" in model["model_messages"]["instructions_template"]


def test_local_demo_supplies_the_codex_model_catalog() -> None:
    script = LOCAL_DEMO_SCRIPT.read_text()

    assert 'MODEL_CATALOG="${AXON_LOCAL_DEMO_MODEL_CATALOG:-' in script
    assert '-c "model_catalog_json=\\"${MODEL_CATALOG}\\""' in script
