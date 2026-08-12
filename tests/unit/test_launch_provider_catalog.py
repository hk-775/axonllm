"""Coverage for the provider mappings and rates that ship at launch."""

from __future__ import annotations

import yaml

from src.gateway.admin.pricing_drift import audit_pricing
from src.gateway.config_loader import load_pricing_config
from src.gateway.model_registry import ModelRegistry


LAUNCH_MAPPINGS = {
    ("azure_openai", "gpt-4o"),
    ("vertex_ai", "gemini-2.5-pro"),
    ("vertex_ai", "gemini-2.5-flash"),
    ("cohere", "command-r-08-2024"),
    ("cohere", "command-r-plus-08-2024"),
}

PUBLISHED_RATES = {
    ("azure_openai", "gpt-4o"): (0.0025, 0.01),
    ("vertex_ai", "gemini-2.5-pro"): (0.00125, 0.01),
    ("vertex_ai", "gemini-2.5-flash"): (0.0003, 0.0025),
    ("cohere", "command-r-08-2024"): (0.00015, 0.0006),
    ("cohere", "command-r-plus-08-2024"): (0.0025, 0.01),
}

UNPRICED_MANTLE_MAPPINGS = {
    ("bedrock-mantle", "openai.gpt-5.4"),
    ("bedrock-mantle", "openai.gpt-5.5"),
    ("bedrock-mantle", "openai.gpt-5.6-luna"),
    ("bedrock-mantle", "openai.gpt-5.6-sol"),
    ("bedrock-mantle", "openai.gpt-5.6-terra"),
}


def _registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.load("config/models.yaml")
    return registry


def test_launch_provider_mappings_are_routable_and_priced() -> None:
    registry = _registry()
    actual = {
        (mapping.provider, mapping.model_id)
        for model in registry.models.values()
        for mapping in model.providers
    }
    pricing = load_pricing_config("config/pricing.yaml")

    assert LAUNCH_MAPPINGS <= actual
    for provider, model_id in LAUNCH_MAPPINGS:
        assert pricing[provider][model_id].is_billable


def test_launch_provider_rates_match_the_published_standard_tiers() -> None:
    pricing = load_pricing_config("config/pricing.yaml")

    for (provider, model_id), expected in PUBLISHED_RATES.items():
        rate = pricing[provider][model_id]
        assert (rate.prompt_token_cost, rate.completion_token_cost) == expected


def test_launch_provider_mappings_are_described_in_the_catalog() -> None:
    with open("config/catalog.yaml", encoding="utf-8") as handle:
        catalog = yaml.safe_load(handle)["providers"]

    described = {
        (provider, model["model_id"])
        for provider, details in catalog.items()
        for model in details.get("models", [])
    }

    assert LAUNCH_MAPPINGS <= described


def test_deprecated_cohere_aliases_are_not_routed() -> None:
    routed_cohere = {
        mapping.model_id
        for model in _registry().models.values()
        for mapping in model.providers
        if mapping.provider == "cohere"
    }

    assert routed_cohere.isdisjoint({"command-r", "command-r-plus", "command-light"})


def test_only_unverifiable_mantle_rates_remain_missing() -> None:
    report = audit_pricing(_registry(), load_pricing_config("config/pricing.yaml"))

    assert {(item.provider, item.model_id) for item in report.unpriced} == (
        UNPRICED_MANTLE_MAPPINGS
    )
