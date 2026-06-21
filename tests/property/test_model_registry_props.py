# Feature: litellm-service, Property 1: Model Registry YAML round-trip
# Validates: Requirements 12.1, 12.2, 12.6, 12.7
"""Property-based test: Model Registry YAML round-trip.

For any valid Model_Registry YAML configuration, loading the YAML into a
ModelRegistry object, pretty-printing it back to YAML, and loading that YAML
again SHALL produce an equivalent ModelRegistry object.
"""

from enum import Enum

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.model_registry import ModelRegistry, VALID_PROVIDERS
from gateway.models import RoutingStrategy

# ---------------------------------------------------------------------------
# Hypothesis strategies for generating valid YAML model registry configs
# ---------------------------------------------------------------------------

PROVIDERS = sorted(VALID_PROVIDERS)
ROUTING_STRATEGIES = [s.value for s in RoutingStrategy]

# Non-empty printable strings that won't confuse YAML parsing.
# Avoid characters that are special in YAML (: # [ ] { } , & * ? | - > ' " %)
_safe_chars = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789 _"
)
safe_text = st.text(_safe_chars, min_size=1, max_size=30).map(str.strip).filter(
    lambda s: len(s) > 0
)

pricing_strategy = st.fixed_dictionaries({
    "prompt_token_cost": st.floats(min_value=0.0001, max_value=1.0, allow_nan=False, allow_infinity=False),
    "completion_token_cost": st.floats(min_value=0.0001, max_value=1.0, allow_nan=False, allow_infinity=False),
})

provider_mapping_strategy = st.fixed_dictionaries({
    "provider": st.sampled_from(PROVIDERS),
    "model_id": safe_text,
    "weight": st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
    "fallback_order": st.integers(min_value=0, max_value=100),
}, optional={
    "pricing": pricing_strategy,
})

capability_strategy = st.lists(
    st.sampled_from(["chat", "streaming", "function_calling", "vision", "embeddings"]),
    min_size=1,
    max_size=4,
    unique=True,
)


def virtual_model_entry_strategy(name_strategy):
    """Strategy for a single virtual model entry dict."""
    return st.fixed_dictionaries({
        "name": name_strategy,
        "description": safe_text,
        "routing_strategy": st.sampled_from(ROUTING_STRATEGIES),
        "providers": st.lists(provider_mapping_strategy, min_size=1, max_size=4),
    }, optional={
        "capabilities": capability_strategy,
    })


@st.composite
def valid_yaml_config(draw):
    """Generate a valid YAML model registry configuration with unique model names."""
    num_models = draw(st.integers(min_value=1, max_value=5))

    # Generate unique model names
    names = draw(
        st.lists(safe_text, min_size=num_models, max_size=num_models, unique=True)
    )

    entries = []
    for name in names:
        entry = draw(virtual_model_entry_strategy(st.just(name)))
        entries.append(entry)

    return {"virtual_models": entries}


# ---------------------------------------------------------------------------
# Equivalence helper
# ---------------------------------------------------------------------------

def registries_equivalent(reg1: ModelRegistry, reg2: ModelRegistry) -> None:
    """Assert two ModelRegistry instances are equivalent."""
    assert set(reg1.models.keys()) == set(reg2.models.keys()), (
        f"Model names differ: {set(reg1.models.keys())} vs {set(reg2.models.keys())}"
    )

    for name in reg1.models:
        m1 = reg1.models[name]
        m2 = reg2.models[name]

        assert m1.name == m2.name
        assert m1.description == m2.description
        assert m1.routing_strategy == m2.routing_strategy
        assert m1.capabilities == m2.capabilities

        assert len(m1.providers) == len(m2.providers), (
            f"Provider count differs for '{name}': {len(m1.providers)} vs {len(m2.providers)}"
        )

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


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------

@given(config=valid_yaml_config())
@settings(max_examples=100)
def test_model_registry_yaml_round_trip(config):
    """Property 1: Model Registry YAML round-trip.

    For any valid Model_Registry YAML configuration, loading the YAML into a
    ModelRegistry object, pretty-printing it back to YAML, and loading that
    YAML again SHALL produce an equivalent ModelRegistry object.

    **Validates: Requirements 12.1, 12.2, 12.6, 12.7**
    """
    # Step 1: Convert config dict to YAML string
    yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)

    # Step 2: Load YAML into ModelRegistry
    reg1 = ModelRegistry.from_yaml(yaml_str)

    # Verify all models were loaded (config is valid, so all should load)
    assert len(reg1.models) == len(config["virtual_models"])

    # Step 3: Pretty-print back to YAML
    yaml_out = reg1.pretty_print()

    # Step 4: Load the pretty-printed YAML into a second ModelRegistry
    reg2 = ModelRegistry.from_yaml(yaml_out)

    # Step 5: Assert equivalence
    registries_equivalent(reg1, reg2)


# ---------------------------------------------------------------------------
# Feature: litellm-service, Property 2: Model Registry validation rejects invalid entries and keeps valid ones
# Validates: Requirements 12.4, 12.5
# ---------------------------------------------------------------------------
"""Property-based test: Model Registry validation rejects invalid entries and keeps valid ones.

For any YAML configuration containing a mix of valid and invalid virtual model
entries, loading the configuration SHALL load all valid entries, reject all
invalid entries, and report specific validation errors (missing required fields,
invalid provider references, duplicate names) for each invalid entry.
"""


# --- Strategies for generating invalid entries ---

class InvalidKind(Enum):
    """Kinds of invalid entries we can generate."""
    MISSING_NAME = "missing_name"
    MISSING_DESCRIPTION = "missing_description"
    MISSING_PROVIDERS = "missing_providers"
    EMPTY_PROVIDERS = "empty_providers"
    INVALID_PROVIDER_NAME = "invalid_provider_name"
    MISSING_MODEL_ID = "missing_model_id"


def _make_invalid_entry(kind: InvalidKind, base_name: str) -> dict:
    """Create an invalid entry dict for the given kind of invalidity."""
    # Start from a valid-looking entry and break it
    valid_base = {
        "name": base_name,
        "description": "A valid description",
        "providers": [
            {"provider": "openai", "model_id": "gpt-4", "weight": 1.0, "fallback_order": 0}
        ],
    }

    if kind == InvalidKind.MISSING_NAME:
        entry = dict(valid_base)
        del entry["name"]
        return entry

    if kind == InvalidKind.MISSING_DESCRIPTION:
        entry = dict(valid_base)
        del entry["description"]
        return entry

    if kind == InvalidKind.MISSING_PROVIDERS:
        entry = dict(valid_base)
        del entry["providers"]
        return entry

    if kind == InvalidKind.EMPTY_PROVIDERS:
        entry = dict(valid_base)
        entry["providers"] = []
        return entry

    if kind == InvalidKind.INVALID_PROVIDER_NAME:
        entry = dict(valid_base)
        entry["providers"] = [
            {"provider": "not_a_real_provider", "model_id": "some-model", "weight": 1.0, "fallback_order": 0}
        ]
        return entry

    if kind == InvalidKind.MISSING_MODEL_ID:
        entry = dict(valid_base)
        entry["providers"] = [
            {"provider": "openai", "weight": 1.0, "fallback_order": 0}
        ]
        return entry

    raise ValueError(f"Unknown invalid kind: {kind}")


invalid_kind_strategy = st.sampled_from(list(InvalidKind))


@st.composite
def invalid_entry_strategy(draw, name_strategy=safe_text):
    """Generate a single invalid virtual model entry."""
    kind = draw(invalid_kind_strategy)
    name = draw(name_strategy)
    return _make_invalid_entry(kind, name), kind


@st.composite
def mixed_valid_invalid_config(draw):
    """Generate a YAML config with a mix of valid and invalid entries.

    Returns (config_dict, valid_names, invalid_indices) where:
    - config_dict is the full config
    - valid_names is the set of names that should be loaded
    - invalid_indices is the set of indices that are invalid
    """
    num_valid = draw(st.integers(min_value=0, max_value=4))
    num_invalid = draw(st.integers(min_value=1, max_value=4))

    # Generate unique names for valid entries
    all_names = draw(
        st.lists(
            safe_text,
            min_size=num_valid + num_invalid,
            max_size=num_valid + num_invalid,
            unique=True,
        )
    )

    valid_names_list = all_names[:num_valid]
    invalid_names_list = all_names[num_valid:]

    # Build valid entries
    valid_entries = []
    for name in valid_names_list:
        entry = draw(virtual_model_entry_strategy(st.just(name)))
        valid_entries.append(entry)

    # Build invalid entries
    invalid_entries = []
    invalid_kinds = []
    for name in invalid_names_list:
        kind = draw(invalid_kind_strategy)
        entry = _make_invalid_entry(kind, name)
        invalid_entries.append(entry)
        invalid_kinds.append(kind)

    # Interleave valid and invalid entries in a random order
    all_entries = []
    valid_indices = set()
    invalid_indices = set()

    # Create tagged list and shuffle
    tagged = [(e, True) for e in valid_entries] + [(e, False) for e in invalid_entries]
    # Use hypothesis to generate a permutation
    indices = list(range(len(tagged)))
    shuffled = draw(st.permutations(indices))
    for new_idx, orig_idx in enumerate(shuffled):
        entry, is_valid = tagged[orig_idx]
        all_entries.append(entry)
        if is_valid:
            valid_indices.add(new_idx)
        else:
            invalid_indices.add(new_idx)

    config = {"virtual_models": all_entries}

    # Compute expected valid names (entries at valid_indices)
    expected_valid_names = set()
    for idx in valid_indices:
        expected_valid_names.add(all_entries[idx]["name"])

    return config, expected_valid_names, invalid_indices, invalid_kinds


@given(data=mixed_valid_invalid_config())
@settings(max_examples=100)
def test_model_registry_validation(data):
    """Property 2: Model Registry validation rejects invalid entries and keeps valid ones.

    For any YAML configuration containing a mix of valid and invalid virtual
    model entries, loading the configuration SHALL load all valid entries,
    reject all invalid entries, and report specific validation errors (missing
    required fields, invalid provider references, duplicate names) for each
    invalid entry.

    **Validates: Requirements 12.4, 12.5**
    """
    config, expected_valid_names, invalid_indices, invalid_kinds = data

    # --- 1. validate() returns errors for each invalid entry ---
    registry_for_validation = ModelRegistry()
    errors = registry_for_validation.validate(config)

    # Each invalid entry should produce at least one validation error
    entries = config["virtual_models"]
    for idx in invalid_indices:
        prefix = f"virtual_models[{idx}]"
        entry_errors = [e for e in errors if e.field.startswith(prefix)]
        assert len(entry_errors) > 0, (
            f"Expected validation error(s) for invalid entry at index {idx}, "
            f"but found none. Entry: {entries[idx]}"
        )

    # Valid entries should NOT have validation errors
    valid_indices = set(range(len(entries))) - invalid_indices
    for idx in valid_indices:
        prefix = f"virtual_models[{idx}]"
        entry_errors = [e for e in errors if e.field.startswith(prefix)]
        assert len(entry_errors) == 0, (
            f"Unexpected validation error(s) for valid entry at index {idx}: "
            f"{[(e.field, e.message) for e in entry_errors]}"
        )

    # --- 2. from_yaml loads all valid entries ---
    yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
    registry = ModelRegistry.from_yaml(yaml_str)

    loaded_names = set(registry.models.keys())

    # All valid entries should be loaded
    assert expected_valid_names == loaded_names, (
        f"Expected valid models {expected_valid_names} but got {loaded_names}"
    )

    # --- 3. No invalid entries are loaded ---
    for idx in invalid_indices:
        entry = entries[idx]
        entry_name = entry.get("name")
        if entry_name is not None and entry_name not in expected_valid_names:
            assert entry_name not in loaded_names, (
                f"Invalid entry '{entry_name}' at index {idx} should not be loaded"
            )


# ---------------------------------------------------------------------------
# Feature: litellm-service, Property 3: Virtual model resolution returns correct provider mappings
# Validates: Requirements 2.4, 2.5, 12.3
# ---------------------------------------------------------------------------
"""Property-based test: Virtual model resolution returns correct provider mappings.

For any loaded ModelRegistry and any registered virtual model name, resolving
the model SHALL return the correct list of ProviderModelMappings with the
configured provider, model_id, weight, and fallback_order. For any unregistered
model name, resolution SHALL raise an error.
"""


@st.composite
def unregistered_name(draw, registered_names):
    """Generate a name that is NOT in the set of registered names."""
    candidate = draw(safe_text)
    # Keep generating until we find one not in the registry
    from hypothesis import assume
    assume(candidate not in registered_names)
    return candidate


@given(config=valid_yaml_config())
@settings(max_examples=100)
def test_virtual_model_resolution(config):
    """Property 3: Virtual model resolution returns correct provider mappings.

    For any loaded ModelRegistry and any registered virtual model name,
    resolving the model SHALL return the correct list of ProviderModelMappings
    with the configured provider, model_id, weight, and fallback_order. For any
    unregistered model name, resolution SHALL raise an error.

    **Validates: Requirements 2.4, 2.5, 12.3**
    """
    import pytest

    # Step 1: Load config into a ModelRegistry
    yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
    registry = ModelRegistry.from_yaml(yaml_str)

    entries = config["virtual_models"]

    # Step 2: For each registered model, resolve and verify mappings match config
    for entry in entries:
        name = entry["name"]
        mappings = registry.resolve(name)

        assert len(mappings) == len(entry["providers"]), (
            f"Provider count mismatch for '{name}': "
            f"expected {len(entry['providers'])}, got {len(mappings)}"
        )

        for mapping, prov_cfg in zip(mappings, entry["providers"]):
            assert mapping.provider == prov_cfg["provider"], (
                f"Provider mismatch for '{name}': "
                f"expected '{prov_cfg['provider']}', got '{mapping.provider}'"
            )
            assert mapping.model_id == prov_cfg["model_id"], (
                f"model_id mismatch for '{name}': "
                f"expected '{prov_cfg['model_id']}', got '{mapping.model_id}'"
            )
            assert mapping.weight == float(prov_cfg.get("weight", 1.0)), (
                f"weight mismatch for '{name}': "
                f"expected {prov_cfg.get('weight', 1.0)}, got {mapping.weight}"
            )
            assert mapping.fallback_order == int(prov_cfg.get("fallback_order", 0)), (
                f"fallback_order mismatch for '{name}': "
                f"expected {prov_cfg.get('fallback_order', 0)}, got {mapping.fallback_order}"
            )

            # Verify pricing if present in config
            if "pricing" in prov_cfg:
                assert mapping.pricing is not None, (
                    f"Expected pricing for '{name}' provider '{mapping.provider}'"
                )
                assert mapping.pricing.prompt_token_cost == float(prov_cfg["pricing"]["prompt_token_cost"]), (
                    f"prompt_token_cost mismatch for '{name}'"
                )
                assert mapping.pricing.completion_token_cost == float(prov_cfg["pricing"]["completion_token_cost"]), (
                    f"completion_token_cost mismatch for '{name}'"
                )
            else:
                assert mapping.pricing is None, (
                    f"Expected no pricing for '{name}' provider '{mapping.provider}'"
                )

    # Step 3: Verify that an unregistered name raises KeyError
    registered_names = {e["name"] for e in entries}
    fake_name = "___nonexistent_model_that_should_never_exist___"
    # Ensure our fake name isn't accidentally registered
    while fake_name in registered_names:
        fake_name += "_x"

    with pytest.raises(KeyError):
        registry.resolve(fake_name)
