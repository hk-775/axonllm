from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ROOT / "scripts" / "operations"
sys.path.insert(0, str(OPERATIONS))

import certify_agentcore as certification  # noqa: E402
import rehearse_agentcore_launch as rehearsal  # noqa: E402
import run_production_validation as validation  # noqa: E402


CERTIFICATION_EXAMPLE = OPERATIONS / "agentcore_certification.example.json"
LAUNCH_GATES_EXAMPLE = OPERATIONS / "agentcore_launch_gates.example.json"
PRODUCTION_VALIDATION_EXAMPLE = (
    OPERATIONS / "production_validation.example.json"
)
CLOUDFRONT_PRODUCTION_VALIDATION_EXAMPLE = (
    OPERATIONS / "production_validation.cloudfront.example.json"
)
MODELS_CONFIG = ROOT / "config" / "models.yaml"
REVIEWED_AT_PLACEHOLDER = "REPLACE_PER_LAUNCH_WITH_REVIEWED_AT_RFC3339"
EXPIRES_AT_PLACEHOLDER = "REPLACE_PER_LAUNCH_WITH_EXPIRES_AT_RFC3339"
EXPECTED_CERTIFICATION_MODELS = {
    "anthropic": "claude-opus",
    "azure_openai": "gpt-4o",
    "bedrock": "claude-opus",
    "bedrock-mantle": "gpt-oss-120b-mantle",
    "cohere": "cohere-command-r",
    "fireworks": "fireworks-deepseek-v4",
    "google_ai": "gemini-3.5-flash",
    "groq": "groq-llama-3.3-70b",
    "openai": "gpt-4.1",
    "together": "together-llama-3.3-70b",
    "vertex_ai": "gemini-2.5-pro",
    "xai": "grok-4.3",
}
EXPECTED_CREDENTIAL_ENVIRONMENTS = {
    "AXON_ACTIVE_CREDENTIAL",
    "AXON_INACTIVE_CREDENTIAL",
    "AXON_UNGRANTED_CREDENTIAL",
    "AXON_CROSS_TENANT_CREDENTIAL",
    "AXON_ADMIN_CREDENTIAL",
    "AXON_VIEWER_CREDENTIAL",
}
SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bgsk_[A-Za-z0-9_-]{20,}"
    r"|\bxai-[A-Za-z0-9_-]{32,}"
    r"|\bfw_[A-Za-z0-9_-]{16,}"
    r"|AQ\.[A-Za-z0-9_-]{8,}"
    r"|(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]"
    r"|bearer\s+[A-Za-z0-9._~+/-]{8,}"
    r")"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def _string_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            item
            for child in value.values()
            for item in _string_values(child)
        ]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    return [value] if isinstance(value, str) else []


def _configured_models() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(MODELS_CONFIG.read_text(encoding="utf-8"))
    return {
        model["name"]: model
        for model in raw["models"]
    }


def test_production_certification_example_uses_the_complete_launch_contract() -> None:
    raw = _load(CERTIFICATION_EXAMPLE)
    config = certification.load_config(CERTIFICATION_EXAMPLE)
    cases = {
        case.provider: case for case in config.providers
    }
    configured_models = _configured_models()

    assert config.profile == certification.PRODUCTION_LAUNCH_PROFILE
    assert set(cases) == (
        certification.PRODUCTION_LAUNCH_PROVIDERS
    )
    assert {
        provider: case.model
        for provider, case in cases.items()
    } == EXPECTED_CERTIFICATION_MODELS
    assert all(
        certification.PRODUCTION_REQUIRED_PROVIDER_FEATURES
        <= case.features
        for case in cases.values()
    )
    assert cases["fireworks"].features == (
        certification.PRODUCTION_REQUIRED_PROVIDER_FEATURES
    )
    for provider, case in cases.items():
        model = configured_models[case.model]
        assert provider in {
            mapping["provider"]
            for mapping in model["providers"]
        }
        if "tool_calling" in case.features:
            assert "tools" in model["capabilities"]
    assert config.tenant_config == certification.TenantConfigCase(
        tenant_id="tenant-launch",
        project_id="project-launch",
    )
    assert config.query.sql.startswith("SELECT ")
    assert config.query.workgroup == "axonllm_read_only"

    identity_values = set(raw["identities"].values())
    assert identity_values == EXPECTED_CREDENTIAL_ENVIRONMENTS
    assert all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) for name in identity_values)


def test_production_certification_accepts_valid_optional_direct_ai21() -> None:
    raw = _load(CERTIFICATION_EXAMPLE)
    raw["providers"].append(
        {
            "provider": "ai21",
            "model": "jamba-large",
            "features": [
                "completion",
                "stream",
                "tool_calling",
            ],
        }
    )

    config = certification.parse_config(raw)
    ai21 = next(
        case for case in config.providers if case.provider == "ai21"
    )
    model = _configured_models()[ai21.model]

    assert ai21.model == "jamba-large"
    assert ai21.features == certification.SUPPORTED_PROVIDER_FEATURES
    assert "ai21" in {
        mapping["provider"] for mapping in model["providers"]
    }
    assert "tools" in model["capabilities"]


def test_launch_gates_example_has_exact_replace_per_launch_bindings() -> None:
    raw = _load(LAUNCH_GATES_EXAMPLE)
    assert raw["schema"] == rehearsal.CONFIG_SCHEMA
    assert raw["review"]["reviewedAt"] == REVIEWED_AT_PLACEHOLDER
    assert raw["review"]["expiresAt"] == EXPIRES_AT_PLACEHOLDER

    with pytest.raises(rehearsal.LaunchOperationError):
        rehearsal.parse_config(
            raw,
            region="us-east-1",
            now=datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc),
            sha256="0" * 64,
        )

    reviewed_at = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
    materialized = deepcopy(raw)
    materialized["review"]["reviewedAt"] = reviewed_at.isoformat()
    materialized["review"]["expiresAt"] = (
        reviewed_at + timedelta(hours=4)
    ).isoformat()
    config = rehearsal.parse_config(
        materialized,
        region="us-east-1",
        now=reviewed_at,
        sha256="0" * 64,
    )

    assert config.account_id == "123456789012"
    assert config.resources.runtime_arn == materialized["resources"]["runtimeArn"]
    assert config.resources.runtime_endpoint_arn == (
        materialized["resources"]["runtimeEndpointArn"]
    )
    assert config.resources.agentcore_stack_name == (
        "AxonLLMAgentCoreStack-managed"
    )
    assert config.resources.control_plane_stack_name == (
        "AxonLLMControlPlaneStack-managed"
    )
    assert config.resources.state_table_name == (
        "axonllm-agentcore-state-managed"
    )
    assert config.coordinator.state_machine_version_arn == (
        materialized["coordinator"]["stateMachineVersionArn"]
    )
    assert config.coordinator.launch_role_arn == (
        materialized["coordinator"]["launchRoleArn"]
    )
    assert config.scenario.select_sql.startswith("SELECT ")
    assert config.scenario.tenant_id == "tenant-launch"
    assert config.scenario.project_id == "project-launch"
    assert config.scenario.model == "claude-opus"
    assert config.scenario.primary_provider == "anthropic"
    assert config.scenario.fallback_provider == "bedrock"
    model = _configured_models()[config.scenario.model]
    configured_providers = {
        mapping["provider"] for mapping in model["providers"]
    }
    assert {
        config.scenario.primary_provider,
        config.scenario.fallback_provider,
    } <= configured_providers


@pytest.mark.parametrize(
    ("path", "credential_type"),
    [
        (PRODUCTION_VALIDATION_EXAMPLE, "alb-session-cookie"),
        (
            CLOUDFRONT_PRODUCTION_VALIDATION_EXAMPLE,
            "browser-session-cookie",
        ),
    ],
)
def test_production_validation_examples_use_the_launch_project(
    path: Path,
    credential_type: str,
) -> None:
    raw = _load(path)

    validation.parse_config(raw)

    requests = [
        *raw["canaries"],
        raw["load"]["request"],
    ]
    assert raw["target"] == "fargate"
    assert all(
        request["path"] == "/admin/projects/project-launch"
        for request in requests
    )
    assert {
        request["credentialType"] for request in requests
    } == {credential_type}
    assert _load(CERTIFICATION_EXAMPLE)["tenantConfig"]["projectId"] == (
        "project-launch"
    )
    assert _load(LAUNCH_GATES_EXAMPLE)["scenario"]["projectId"] == (
        "project-launch"
    )


@pytest.mark.parametrize(
    "path",
    [
        CERTIFICATION_EXAMPLE,
        LAUNCH_GATES_EXAMPLE,
        PRODUCTION_VALIDATION_EXAMPLE,
        CLOUDFRONT_PRODUCTION_VALIDATION_EXAMPLE,
    ],
)
def test_agentcore_operation_examples_contain_no_secret_values(path: Path) -> None:
    raw = _load(path)
    values = _string_values(raw)

    assert all(SECRET_VALUE.search(value) is None for value in values)
    assert all("-----BEGIN " not in value for value in values)
    assert all("authorization" not in value.lower() for value in values)
