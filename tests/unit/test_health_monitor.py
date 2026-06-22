"""Tests for spoke health monitor."""

import asyncio

import pytest

from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
    active_passive,
)


def _run(coro):
    return asyncio.run(coro)


class TestHealthCheckNoURL:
    def test_local_spoke_always_healthy(self):
        config = HubConfig(
            hub_region="us-east-1",
            spokes=[SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)],
        )
        monitor = SpokeHealthMonitor(hub_config=config)
        result = _run(monitor.check_spoke(config.spokes[0]))
        assert result.healthy is True
        assert result.region == "us-east-1"

    def test_remote_spoke_without_url_assumed_healthy(self):
        config = HubConfig(
            hub_region="us-east-1",
            spokes=[SpokeConfig(region="eu-west-1", role=SpokeRole.ACTIVE)],
        )
        monitor = SpokeHealthMonitor(hub_config=config)
        result = _run(monitor.check_spoke(config.spokes[0]))
        assert result.healthy is True
        assert result.error == "no_health_url_configured"


class TestConsecutiveFailures:
    def test_single_failure_stays_healthy(self):
        config = HubConfig(
            hub_region="us-east-1",
            failover_threshold_consecutive=3,
            spokes=[SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)],
        )
        monitor = SpokeHealthMonitor(hub_config=config)
        spoke = config.spokes[0]

        from src.gateway.multi_region.health_monitor import HealthCheckResult
        bad_result = HealthCheckResult(region="us-east-1", healthy=False, error="timeout")
        monitor._update_spoke_status(spoke, bad_result)

        assert spoke.status == SpokeStatus.HEALTHY
        assert monitor._consecutive_failures["us-east-1"] == 1

    def test_threshold_failures_marks_unhealthy(self):
        config = HubConfig(
            hub_region="us-east-1",
            failover_threshold_consecutive=3,
            spokes=[SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)],
        )
        monitor = SpokeHealthMonitor(hub_config=config)
        spoke = config.spokes[0]

        from src.gateway.multi_region.health_monitor import HealthCheckResult
        bad_result = HealthCheckResult(region="us-east-1", healthy=False)

        for _ in range(3):
            monitor._update_spoke_status(spoke, bad_result)

        assert spoke.status == SpokeStatus.UNHEALTHY

    def test_recovery_resets_failures(self):
        config = HubConfig(
            hub_region="us-east-1",
            failover_threshold_consecutive=3,
            failover_cooldown_seconds=0,
            spokes=[SpokeConfig(region="us-east-1", role=SpokeRole.PRIMARY)],
        )
        monitor = SpokeHealthMonitor(hub_config=config)
        spoke = config.spokes[0]

        from src.gateway.multi_region.health_monitor import HealthCheckResult

        # Fail it
        bad = HealthCheckResult(region="us-east-1", healthy=False)
        for _ in range(3):
            monitor._update_spoke_status(spoke, bad)
        assert spoke.status == SpokeStatus.UNHEALTHY

        # Recover it
        good = HealthCheckResult(region="us-east-1", healthy=True)
        monitor._update_spoke_status(spoke, good)
        assert spoke.status == SpokeStatus.HEALTHY
        assert monitor._consecutive_failures["us-east-1"] == 0


class TestManualOverride:
    def test_mark_unhealthy(self):
        config = active_passive("us-east-1", "us-west-2")
        monitor = SpokeHealthMonitor(hub_config=config)

        monitor.mark_unhealthy("us-east-1")
        assert config.spokes[0].status == SpokeStatus.UNHEALTHY

    def test_mark_healthy(self):
        config = active_passive("us-east-1", "us-west-2")
        monitor = SpokeHealthMonitor(hub_config=config)

        config.spokes[0].status = SpokeStatus.UNHEALTHY
        monitor.mark_healthy("us-east-1")
        assert config.spokes[0].status == SpokeStatus.HEALTHY

    def test_mark_draining(self):
        config = active_passive("us-east-1", "us-west-2")
        monitor = SpokeHealthMonitor(hub_config=config)

        monitor.mark_draining("us-east-1")
        assert config.spokes[0].status == SpokeStatus.DRAINING


class TestCheckAll:
    def test_checks_all_spokes(self):
        config = active_passive("us-east-1", "us-west-2")
        monitor = SpokeHealthMonitor(hub_config=config)
        results = _run(monitor.check_all())
        assert len(results) == 2
        assert all(r.healthy for r in results)

    def test_status_summary(self):
        config = active_passive("us-east-1", "us-west-2")
        monitor = SpokeHealthMonitor(hub_config=config)
        _run(monitor.check_all())

        summary = monitor.get_status_summary()
        assert summary["hub_region"] == "us-east-1"
        assert len(summary["spokes"]) == 2
        assert summary["spokes"][0]["role"] == "primary"
        assert summary["spokes"][0]["status"] == "healthy"
