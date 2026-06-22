"""Tests for multi-region hub-and-spoke routing."""

import pytest

from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
    active_active,
    active_passive,
    default_single_region,
)
from src.gateway.multi_region.region_router import RegionRouter


class TestSingleRegion:
    def test_single_region_routes_to_self(self):
        config = default_single_region("us-east-1")
        router = RegionRouter(hub_config=config)
        decision = router.route()
        assert decision is not None
        assert decision.target_spoke.region == "us-east-1"
        assert decision.reason == "single_available"

    def test_single_region_with_model_routes(self):
        config = default_single_region("us-east-1")
        router = RegionRouter(hub_config=config)
        decision = router.route(model="claude-opus")
        assert decision is not None
        assert decision.target_spoke.region == "us-east-1"

    def test_single_region_unhealthy_returns_none(self):
        config = default_single_region("us-east-1")
        config.spokes[0].status = SpokeStatus.UNHEALTHY
        router = RegionRouter(hub_config=config)
        decision = router.route()
        assert decision is None


class TestActivePassiveFailover:
    def test_primary_healthy_routes_to_primary(self):
        config = active_passive("us-east-1", "us-west-2")
        router = RegionRouter(hub_config=config)
        decision = router.route()
        assert decision.target_spoke.region == "us-east-1"
        assert decision.reason == "primary_healthy"

    def test_primary_down_failover_triggers(self):
        config = active_passive("us-east-1", "us-west-2")
        config.spokes[0].status = SpokeStatus.UNHEALTHY
        router = RegionRouter(hub_config=config)
        decision = router.route()
        assert decision is not None
        assert decision.target_spoke.region == "us-west-2"

    def test_explicit_failover(self):
        config = active_passive("us-east-1", "us-west-2")
        router = RegionRouter(hub_config=config)
        decision = router.failover()
        assert decision is not None
        assert decision.target_spoke.region == "us-west-2"
        assert decision.fallback_used is True

    def test_no_failover_candidates(self):
        config = HubConfig(
            hub_region="us-east-1",
            spokes=[SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)],
        )
        router = RegionRouter(hub_config=config)
        decision = router.failover()
        assert decision is None


class TestActiveActive:
    def test_distributes_across_regions(self):
        config = active_active([("us-east-1", 60), ("eu-west-1", 40)])
        router = RegionRouter(hub_config=config)

        regions_hit = set()
        for _ in range(100):
            decision = router.route()
            regions_hit.add(decision.target_spoke.region)

        assert "us-east-1" in regions_hit
        assert "eu-west-1" in regions_hit

    def test_unhealthy_region_excluded(self):
        config = active_active([("us-east-1", 50), ("eu-west-1", 50)])
        config.spokes[1].status = SpokeStatus.UNHEALTHY
        router = RegionRouter(hub_config=config)

        for _ in range(20):
            decision = router.route()
            assert decision.target_spoke.region == "us-east-1"

    def test_draining_excluded(self):
        config = active_active([("us-east-1", 50), ("eu-west-1", 50)])
        config.spokes[0].status = SpokeStatus.DRAINING
        router = RegionRouter(hub_config=config)

        for _ in range(20):
            decision = router.route()
            assert decision.target_spoke.region == "eu-west-1"


class TestDataResidency:
    def test_strict_residency_filters(self):
        config = HubConfig(
            hub_region="us-east-1",
            data_residency_strict=True,
            spokes=[
                SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY, weight=50,
                            data_residency_zones=["us", "na"]),
                SpokeConfig(region="eu-west-1", role=SpokeRole.ACTIVE, weight=50,
                            data_residency_zones=["eu", "gdpr"]),
            ],
        )
        router = RegionRouter(hub_config=config)

        decision = router.route(data_residency_zone="eu")
        assert decision.target_spoke.region == "eu-west-1"

    def test_strict_residency_no_match_returns_none(self):
        config = HubConfig(
            hub_region="us-east-1",
            data_residency_strict=True,
            spokes=[
                SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY, weight=100,
                            data_residency_zones=["us"]),
            ],
        )
        router = RegionRouter(hub_config=config)
        decision = router.route(data_residency_zone="eu")
        assert decision is None

    def test_non_strict_ignores_residency(self):
        config = HubConfig(
            hub_region="us-east-1",
            data_residency_strict=False,
            spokes=[
                SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY, weight=100,
                            data_residency_zones=["us"]),
            ],
        )
        router = RegionRouter(hub_config=config)
        decision = router.route(data_residency_zone="eu")
        assert decision is not None


class TestModelFiltering:
    def test_routes_to_spoke_with_model(self):
        config = HubConfig(
            hub_region="us-east-1",
            spokes=[
                SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY, weight=50,
                            models=["claude-sonnet", "gpt-4o"]),
                SpokeConfig(region="eu-west-1", role=SpokeRole.ACTIVE, weight=50,
                            models=["claude-opus", "claude-sonnet"]),
            ],
        )
        router = RegionRouter(hub_config=config)
        decision = router.route(model="claude-opus")
        assert decision.target_spoke.region == "eu-west-1"

    def test_empty_model_list_means_all(self):
        config = HubConfig(
            hub_region="us-east-1",
            spokes=[
                SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY, weight=100, models=[]),
            ],
        )
        router = RegionRouter(hub_config=config)
        decision = router.route(model="anything")
        assert decision is not None


class TestPreferredRegion:
    def test_preferred_region_wins(self):
        config = active_active([("us-east-1", 80), ("eu-west-1", 20)])
        router = RegionRouter(hub_config=config)
        decision = router.route(preferred_region="eu-west-1")
        assert decision.target_spoke.region == "eu-west-1"
        assert decision.reason == "preferred_region"

    def test_preferred_region_unhealthy_falls_back(self):
        config = active_active([("us-east-1", 80), ("eu-west-1", 20)])
        config.spokes[1].status = SpokeStatus.UNHEALTHY
        router = RegionRouter(hub_config=config)
        decision = router.route(preferred_region="eu-west-1")
        assert decision.target_spoke.region == "us-east-1"


class TestConfigHelpers:
    def test_default_single_region(self):
        config = default_single_region("ap-southeast-1")
        assert config.hub_region == "ap-southeast-1"
        assert config.is_single_region is True
        assert len(config.spokes) == 1

    def test_active_passive_config(self):
        config = active_passive("us-east-1", "us-west-2")
        assert config.get_primary().region == "us-east-1"
        assert len(config.get_failover_candidates()) == 1
        assert config.is_single_region is False

    def test_active_active_config(self):
        config = active_active([("us-east-1", 60), ("eu-west-1", 40)])
        assert len(config.active_spokes) == 2
