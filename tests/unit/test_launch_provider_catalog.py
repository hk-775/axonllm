"""Coverage for the provider mappings and rates that ship at launch."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.gateway.adapters.google_ai_adapter import GoogleAIAdapter
from src.gateway.admin.pricing_drift import audit_pricing
from src.gateway.config_loader import load_pricing_config
from src.gateway.model_registry import ModelRegistry


_REPO = Path(__file__).resolve().parents[2]
_PACKAGED_CONFIG = _REPO / "src" / "gateway" / "resources" / "runtime" / "config"
_ROOT_CONFIG = _REPO / "config"

_CAPABILITIES = {"chat", "streaming", "tools", "vision", "reasoning"}

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


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects keys PyYAML would otherwise overwrite."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict:
    loader.flatten_mapping(node)
    keys = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        keys.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_UniqueKeyLoader)


def _model_entries() -> list[dict]:
    return _load_yaml(_PACKAGED_CONFIG / "models.yaml")["models"]


def _catalog() -> dict:
    return _load_yaml(_PACKAGED_CONFIG / "catalog.yaml")["providers"]


def _routed_mappings() -> list[tuple[str, str]]:
    return [
        (mapping["provider"], mapping["model_id"])
        for model in _model_entries()
        for mapping in model["providers"]
    ]


def _catalog_mappings() -> list[tuple[str, str]]:
    return [
        (provider, model["model_id"])
        for provider, details in _catalog().items()
        for model in details.get("models", [])
    ]


def _registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.load(str(_PACKAGED_CONFIG / "models.yaml"))
    return registry


def _pricing():
    return load_pricing_config(str(_PACKAGED_CONFIG / "pricing.yaml"))


def test_repository_config_is_an_exact_mirror_of_packaged_runtime() -> None:
    packaged_files = {
        path.name: path.read_bytes()
        for path in _PACKAGED_CONFIG.iterdir()
        if path.is_file()
    }
    root_files = {
        path.name: path.read_bytes()
        for path in _ROOT_CONFIG.iterdir()
        if path.is_file()
    }

    assert root_files == packaged_files


def test_launch_yaml_rejects_duplicate_mapping_keys() -> None:
    for root in (_ROOT_CONFIG, _PACKAGED_CONFIG):
        for filename in ("models.yaml", "catalog.yaml"):
            _load_yaml(root / filename)


def test_routed_and_catalog_mappings_are_unique() -> None:
    routed = _routed_mappings()
    catalog = _catalog_mappings()

    assert len(routed) == len(set(routed))
    assert len(catalog) == len(set(catalog))


def test_routed_mappings_exactly_match_the_catalog() -> None:
    assert sorted(_routed_mappings()) == sorted(_catalog_mappings())


def test_models_declare_nonempty_conservative_capabilities() -> None:
    catalog_capabilities = {
        (provider, model["model_id"]): set(model["capabilities"])
        for provider, details in _catalog().items()
        for model in details["models"]
    }

    for model in _model_entries():
        capabilities = model.get("capabilities")
        assert isinstance(capabilities, list) and capabilities, model["name"]
        assert "chat" in capabilities, model["name"]
        assert set(capabilities) <= _CAPABILITIES, model["name"]
        assert len(capabilities) == len(set(capabilities)), model["name"]

        guaranteed = set.intersection(
            *(
                catalog_capabilities[
                    (mapping["provider"], mapping["model_id"])
                ]
                for mapping in model["providers"]
            )
        )
        assert set(capabilities) <= guaranteed, model["name"]


def test_catalog_entries_declare_nonempty_capabilities() -> None:
    for provider, details in _catalog().items():
        for model in details["models"]:
            capabilities = model.get("capabilities")
            identity = (provider, model["model_id"])
            assert isinstance(capabilities, list) and capabilities, identity
            assert "chat" in capabilities, identity
            assert set(capabilities) <= _CAPABILITIES, identity
            assert len(capabilities) == len(set(capabilities)), identity


def test_launch_provider_mappings_are_routable_and_priced() -> None:
    registry = _registry()
    actual = {
        (mapping.provider, mapping.model_id)
        for model in registry.models.values()
        for mapping in model.providers
    }
    pricing = _pricing()

    assert LAUNCH_MAPPINGS <= actual
    for provider, model_id in LAUNCH_MAPPINGS:
        assert pricing[provider][model_id].is_billable


def test_launch_provider_rates_match_the_published_standard_tiers() -> None:
    pricing = _pricing()

    for (provider, model_id), expected in PUBLISHED_RATES.items():
        rate = pricing[provider][model_id]
        assert (rate.prompt_token_cost, rate.completion_token_cost) == expected


def test_launch_provider_mappings_are_described_in_the_catalog() -> None:
    described = {
        (provider, model["model_id"])
        for provider, details in _catalog().items()
        for model in details.get("models", [])
    }

    assert LAUNCH_MAPPINGS <= described


def test_new_adopters_do_not_route_gemini_2_5_pro_through_google_ai() -> None:
    routed = set(_routed_mappings())

    assert ("google_ai", "gemini-2.5-pro") not in routed
    assert ("vertex_ai", "gemini-2.5-pro") in routed


def test_google_ai_adapter_models_match_launch_catalog() -> None:
    adapter_models = {
        model.model_id
        for model in GoogleAIAdapter._MODELS
    }
    catalog_models = {
        model["model_id"]
        for model in _catalog()["google_ai"]["models"]
    }

    assert adapter_models == catalog_models


def test_deprecated_cohere_aliases_are_not_routed() -> None:
    routed_cohere = {
        mapping.model_id
        for model in _registry().models.values()
        for mapping in model.providers
        if mapping.provider == "cohere"
    }

    assert routed_cohere.isdisjoint(
        {"command-r", "command-r-plus", "command-light"}
    )


def test_only_unverifiable_mantle_rates_remain_missing() -> None:
    report = audit_pricing(_registry(), _pricing())

    assert {(item.provider, item.model_id) for item in report.unpriced} == (
        UNPRICED_MANTLE_MAPPINGS
    )
