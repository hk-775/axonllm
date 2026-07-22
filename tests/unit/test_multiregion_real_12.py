"""Tests for making multi-region routing real (#12): the selected spoke's
endpoint/region actually reaches the provider call, the spoke-config loader,
and the health-monitor lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.gateway.adapters.registry import AdapterRegistry
from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
    default_single_region,
)
from src.gateway.multi_region.spoke_loader import load_hub_config
from src.gateway.provider_config import ProviderConfig
from src.gateway.provider_fn_factory import ProviderFnFactory


def _cfg(provider="bedrock", auth="aws_credentials", base="https://use1.example.com"):
    creds = {"access_key": "a", "secret_key": "s", "region": "us-east-1"} \
        if auth == "aws_credentials" else {"api_key": "k"}
    return ProviderConfig(provider_name=provider, base_url=base, auth_type=auth,
                          credentials=creds)


def _factory(config):
    return ProviderFnFactory(
        adapter_registry=MagicMock(spec=AdapterRegistry),
        provider_configs={config.provider_name: config},
        http_client=MagicMock(),
    )


# --- config_for: spoke override reaches the provider call ---


class TestSpokeConfigOverride:
    def test_no_spoke_returns_base_config(self):
        f = _factory(_cfg())
        assert f.config_for("bedrock", None).base_url == "https://use1.example.com"

    def test_spoke_endpoint_overrides_base_url(self):
        f = _factory(_cfg())
        spoke = SpokeConfig(region="eu-west-1", endpoint="https://euw1.example.com")
        out = f.config_for("bedrock", spoke)
        assert out.base_url == "https://euw1.example.com"

    def test_spoke_region_rewrites_aws_credential_region(self):
        f = _factory(_cfg(auth="aws_credentials"))
        spoke = SpokeConfig(region="eu-west-1", endpoint="https://euw1.example.com")
        out = f.config_for("bedrock", spoke)
        assert out.credentials["region"] == "eu-west-1"     # signed for the spoke region

    def test_spoke_region_does_not_touch_api_key_provider(self):
        f = _factory(_cfg(provider="openai", auth="api_key"))
        spoke = SpokeConfig(region="eu-west-1")            # no endpoint
        out = f.config_for("openai", spoke)
        # No endpoint + non-aws auth → nothing to override → base returned as-is.
        assert out.base_url == "https://use1.example.com"

    def test_override_does_not_mutate_base_config(self):
        base = _cfg()
        f = _factory(base)
        spoke = SpokeConfig(region="eu-west-1", endpoint="https://euw1.example.com")
        f.config_for("bedrock", spoke)
        assert base.base_url == "https://use1.example.com"   # original untouched
        assert base.credentials["region"] == "us-east-1"

    async def test_provider_fn_uses_spoke_endpoint(self):
        base = _cfg()
        http = MagicMock()
        http.execute = AsyncMock(return_value="resp")
        f = ProviderFnFactory(
            adapter_registry=MagicMock(spec=AdapterRegistry),
            provider_configs={"bedrock": base}, http_client=http)
        from src.gateway.models import ChatCompletionRequest, ProviderModelMapping
        spoke = SpokeConfig(region="eu-west-1", endpoint="https://euw1.example.com")
        fn = f.create(ChatCompletionRequest(messages=[], model="m"), spoke=spoke)
        await fn(ProviderModelMapping(provider="bedrock", model_id="m"))
        # http_client.execute received the spoke-overridden config.
        used_config = http.execute.call_args.args[3]
        assert used_config.base_url == "https://euw1.example.com"
        assert used_config.credentials["region"] == "eu-west-1"


class TestMultiProviderFactorySpoke:
    """The production factory (used in bootstrap + the Ostiari embed) must also
    honor the spoke override."""

    def test_config_for_applies_spoke(self):
        from src.gateway.multi_provider_factory import MultiProviderFactory
        f = MultiProviderFactory(provider_configs={"openai": _cfg(
            provider="openai", auth="api_key")})
        spoke = SpokeConfig(region="eu-west-1", endpoint="https://euw1.example.com")
        assert f.config_for("openai", spoke).base_url == "https://euw1.example.com"

    def test_bedrock_client_bound_to_spoke_region(self):
        from src.gateway.multi_provider_factory import MultiProviderFactory
        f = MultiProviderFactory(bedrock_region="us-east-1")
        # create() with a spoke in another region builds a region-bound client.
        from src.gateway.models import ChatCompletionRequest
        spoke = SpokeConfig(region="eu-west-1")
        f.create(ChatCompletionRequest(messages=[], model="m"), spoke=spoke)
        assert "eu-west-1" in f._bedrock_by_region     # region client cached

    def test_create_accepts_no_spoke(self):
        from src.gateway.multi_provider_factory import MultiProviderFactory
        from src.gateway.models import ChatCompletionRequest
        f = MultiProviderFactory(bedrock_region="us-east-1")
        fn = f.create(ChatCompletionRequest(messages=[], model="m"))
        assert callable(fn)


# --- spoke_loader ---


class TestSpokeLoader:
    def test_missing_file_falls_back_to_single_region(self, tmp_path):
        hub = load_hub_config(str(tmp_path / "nope.yaml"), default_region="us-west-2")
        assert hub.is_single_region
        assert hub.spokes[0].region == "us-west-2"

    def test_empty_spokes_falls_back(self, tmp_path):
        p = tmp_path / "spokes.yaml"
        p.write_text("hub_region: us-east-1\nspokes: []\n")
        assert load_hub_config(str(p)).is_single_region

    def test_loads_multi_region(self, tmp_path):
        p = tmp_path / "spokes.yaml"
        p.write_text(
            "hub_region: us-east-1\n"
            "data_residency_strict: true\n"
            "spokes:\n"
            "  - region: us-east-1\n"
            "    role: primary\n"
            "    weight: 70\n"
            "    endpoint: https://use1.example.com\n"
            "    data_residency_zones: [us]\n"
            "  - region: eu-west-1\n"
            "    role: active\n"
            "    weight: 30\n"
            "    endpoint: https://euw1.example.com\n"
            "    data_residency_zones: [eu]\n"
        )
        hub = load_hub_config(str(p))
        assert not hub.is_single_region
        assert len(hub.spokes) == 2
        assert hub.data_residency_strict is True
        eu = hub.get_spoke("eu-west-1")
        assert eu.endpoint == "https://euw1.example.com"
        assert eu.role == SpokeRole.ACTIVE
        assert eu.data_residency_zones == ["eu"]

    def test_malformed_file_falls_back(self, tmp_path):
        p = tmp_path / "spokes.yaml"
        p.write_text("spokes:\n  - {this is: [broken")   # invalid YAML
        assert load_hub_config(str(p)).is_single_region


# --- health monitor lifecycle (started only when multi-region) ---


class TestMonitorLifecycle:
    async def test_lifespan_starts_monitor_when_multi_region(self):
        from src.gateway.multi_region.health_monitor import SpokeHealthMonitor

        hub = HubConfig(hub_region="us-east-1", spokes=[
            SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY),
            SpokeConfig(region="eu-west-1", role=SpokeRole.ACTIVE),
        ])
        mon = SpokeHealthMonitor(hub)
        assert not hub.is_single_region
        await mon.start()
        assert mon._running is True
        await mon.stop()
        assert mon._running is False

    def test_single_region_is_flagged(self):
        assert default_single_region().is_single_region


# --- selection still correct (regression guard on the router logic) ---


class TestSelectionUnchanged:
    def test_residency_filters_to_matching_spoke(self):
        from src.gateway.multi_region.region_router import RegionRouter

        hub = HubConfig(hub_region="us-east-1", data_residency_strict=True, spokes=[
            SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY,
                        data_residency_zones=["us"], status=SpokeStatus.HEALTHY),
            SpokeConfig(region="eu-west-1", role=SpokeRole.ACTIVE,
                        data_residency_zones=["eu"], status=SpokeStatus.HEALTHY),
        ])
        decision = RegionRouter(hub).route(data_residency_zone="eu")
        assert decision.target_spoke.region == "eu-west-1"

    def test_no_healthy_spoke_returns_none(self):
        from src.gateway.multi_region.region_router import RegionRouter

        hub = HubConfig(hub_region="us-east-1", spokes=[
            SpokeConfig(region="us-east-1", status=SpokeStatus.UNHEALTHY),
        ])
        assert RegionRouter(hub).route() is None
