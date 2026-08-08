"""Property-based tests for src.gateway.config_loader."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.config_loader import (
    DemoSeedData,
    load_app_config,
    load_demo_seed_config,
    load_pricing_config,
    serialize_demo_seed_config,
    serialize_pricing_config,
)
from src.gateway.models import TokenPricing


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_positive_float = st.floats(min_value=0.0001, max_value=100.0, allow_nan=False, allow_infinity=False)
_optional_float = st.one_of(st.none(), _positive_float)

_token_pricing_st = st.builds(
    TokenPricing,
    prompt_token_cost=_positive_float,
    completion_token_cost=_positive_float,
    cached_token_cost=_optional_float,
    image_token_cost=_optional_float,
    reasoning_token_cost=_optional_float,
    per_request_cost=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)

_model_name_st = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_."),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() == s and len(s.strip()) > 0)

_provider_name_st = st.sampled_from(["openai", "anthropic", "bedrock", "vertex_ai", "cohere"])

_pricing_config_st = st.dictionaries(
    keys=_provider_name_st,
    values=st.dictionaries(
        keys=_model_name_st,
        values=_token_pricing_st,
        min_size=1,
        max_size=3,
    ),
    min_size=1,
    max_size=3,
)


# ---------------------------------------------------------------------------
# Property 1: Pricing round-trip
# ---------------------------------------------------------------------------


@given(pricing=_pricing_config_st)
@settings(max_examples=50)
def test_pricing_round_trip(pricing: dict[str, dict[str, TokenPricing]], tmp_path_factory):
    """Loading pricing YAML → serialize → reload produces equivalent TokenPricing objects."""
    tmp_path = tmp_path_factory.mktemp("pricing")
    serialized = serialize_pricing_config(pricing)
    yaml_path = tmp_path / "pricing.yaml"
    yaml_path.write_text(yaml.dump(serialized, default_flow_style=False))

    reloaded = load_pricing_config(str(yaml_path))

    for provider in pricing:
        assert provider in reloaded, f"Provider {provider} missing after round-trip"
        for model in pricing[provider]:
            assert model in reloaded[provider], f"Model {model} missing after round-trip"
            orig = pricing[provider][model]
            got = reloaded[provider][model]
            assert abs(orig.prompt_token_cost - got.prompt_token_cost) < 1e-9
            assert abs(orig.completion_token_cost - got.completion_token_cost) < 1e-9
            assert orig.cached_token_cost == got.cached_token_cost or (
                orig.cached_token_cost is not None
                and got.cached_token_cost is not None
                and abs(orig.cached_token_cost - got.cached_token_cost) < 1e-9
            )
            assert orig.image_token_cost == got.image_token_cost or (
                orig.image_token_cost is not None
                and got.image_token_cost is not None
                and abs(orig.image_token_cost - got.image_token_cost) < 1e-9
            )


# ---------------------------------------------------------------------------
# Property 2: Demo seed round-trip
# ---------------------------------------------------------------------------

_project_st = st.fixed_dictionaries({
    "project_id": st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop-"),
    "name": st.text(min_size=1, max_size=30),
})

_user_budget_st = st.fixed_dictionaries({
    "user_id": st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop-"),
    "budget_limit": _positive_float,
})

_demo_seed_st = st.builds(
    DemoSeedData,
    projects=st.lists(_project_st, min_size=0, max_size=3),
    user_budgets=st.lists(_user_budget_st, min_size=0, max_size=3),
    usage_seeds=st.just([]),
    policies=st.just([]),
    unhealthy_providers=st.just([]),
)


@given(seed=_demo_seed_st)
@settings(max_examples=50)
def test_demo_seed_round_trip(seed: DemoSeedData, tmp_path_factory):
    """Loading seed YAML → serialize → reload produces equivalent DemoSeedData."""
    tmp_path = tmp_path_factory.mktemp("seed")
    serialized = serialize_demo_seed_config(seed)
    yaml_path = tmp_path / "seed.yaml"
    yaml_path.write_text(yaml.dump(serialized, default_flow_style=False))

    reloaded = load_demo_seed_config(str(yaml_path))

    assert len(reloaded.projects) == len(seed.projects)
    for orig, got in zip(seed.projects, reloaded.projects):
        assert orig["project_id"] == got["project_id"]
        assert orig["name"] == got["name"]

    assert len(reloaded.user_budgets) == len(seed.user_budgets)


# ---------------------------------------------------------------------------
# Property 3: Optional defaults equivalence
# ---------------------------------------------------------------------------


@given(
    prompt_cost=_positive_float,
    completion_cost=_positive_float,
)
@settings(max_examples=50)
def test_pricing_optional_defaults_equivalence(
    prompt_cost: float, completion_cost: float, tmp_path_factory
):
    """Omitting optional fields and explicitly setting defaults produce identical TokenPricing."""
    tmp_path = tmp_path_factory.mktemp("defaults")

    # YAML with only required fields
    minimal = {
        "providers": {
            "test": {
                "model-a": {
                    "prompt_token_cost": prompt_cost,
                    "completion_token_cost": completion_cost,
                }
            }
        }
    }
    # YAML with explicit defaults
    explicit = {
        "providers": {
            "test": {
                "model-a": {
                    "prompt_token_cost": prompt_cost,
                    "completion_token_cost": completion_cost,
                    "per_request_cost": 0.0,
                }
            }
        }
    }

    p1 = tmp_path / "minimal.yaml"
    p2 = tmp_path / "explicit.yaml"
    p1.write_text(yaml.dump(minimal))
    p2.write_text(yaml.dump(explicit))

    r1 = load_pricing_config(str(p1))
    r2 = load_pricing_config(str(p2))

    tp1 = r1["test"]["model-a"]
    tp2 = r2["test"]["model-a"]
    assert tp1 == tp2


# ---------------------------------------------------------------------------
# Property 4: AppConfig env var override
# ---------------------------------------------------------------------------

_env_var_mapping = {
    "AWS_DEFAULT_REGION": ("aws_region", st.sampled_from(["us-east-1", "eu-west-1", "ap-southeast-1"])),
    "AXON_SERVER_PORT": ("server_port", st.integers(min_value=1024, max_value=65535).map(str)),
    "AXON_LOAD_DEMO_DATA": ("load_demo_data", st.sampled_from(["true", "false"])),
}


@given(
    env_var=st.sampled_from(list(_env_var_mapping.keys())),
    data=st.data(),
)
@settings(max_examples=30)
def test_app_config_env_override(env_var: str, data):
    """Setting a single env var changes only the corresponding AppConfig field."""
    field_name, value_st = _env_var_mapping[env_var]
    value = data.draw(value_st)

    # Save and clear relevant env vars
    saved = {}
    for key in list(os.environ):
        if key.startswith("AXON_") or key == "AWS_DEFAULT_REGION":
            saved[key] = os.environ.pop(key)

    try:
        os.environ[env_var] = value
        os.environ["AXON_DEPLOYMENT_PROFILE"] = "development"
        config = load_app_config()

        actual = getattr(config, field_name)
        if field_name == "server_port":
            assert actual == int(value)
        elif field_name == "load_demo_data":
            assert actual == (value == "true")
        else:
            assert actual == value
    finally:
        # Restore env vars
        os.environ.pop(env_var, None)
        for key in list(os.environ):
            if key.startswith("AXON_") or key == "AWS_DEFAULT_REGION":
                os.environ.pop(key, None)
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# Property 5: Idempotent load
# ---------------------------------------------------------------------------


@given(pricing=_pricing_config_st)
@settings(max_examples=30)
def test_pricing_idempotent_load(pricing: dict[str, dict[str, TokenPricing]], tmp_path_factory):
    """Loading the same pricing file twice produces identical results."""
    tmp_path = tmp_path_factory.mktemp("idempotent")
    serialized = serialize_pricing_config(pricing)
    yaml_path = tmp_path / "pricing.yaml"
    yaml_path.write_text(yaml.dump(serialized, default_flow_style=False))

    r1 = load_pricing_config(str(yaml_path))
    r2 = load_pricing_config(str(yaml_path))

    assert r1.keys() == r2.keys()
    for provider in r1:
        assert r1[provider].keys() == r2[provider].keys()
        for model in r1[provider]:
            assert r1[provider][model] == r2[provider][model]
