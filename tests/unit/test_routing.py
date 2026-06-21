"""Unit tests for routing strategies."""

import pytest

from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.models import ProviderModelMapping, TokenPricing
from src.gateway.routing import (
    CostOptimizedStrategy,
    LeastLatencyStrategy,
    NoHealthyProviderError,
    RoundRobinStrategy,
    WeightedStrategy,
)


@pytest.fixture
def health_tracker():
    return ProviderHealthTracker()


@pytest.fixture
def providers():
    return [
        ProviderModelMapping(
            provider="openai",
            model_id="gpt-4",
            weight=0.7,
            pricing=TokenPricing(0.01, 0.03),
        ),
        ProviderModelMapping(
            provider="anthropic",
            model_id="claude-3",
            weight=0.3,
            pricing=TokenPricing(0.003, 0.015),
        ),
        ProviderModelMapping(
            provider="bedrock",
            model_id="titan",
            weight=0.5,
            pricing=TokenPricing(0.005, 0.02),
        ),
    ]


# --- NoHealthyProviderError ---

class TestNoHealthyProviders:
    def test_round_robin_raises(self, providers, health_tracker):
        for p in providers:
            health_tracker.mark_unhealthy(p.provider, cooldown_seconds=300)
        with pytest.raises(NoHealthyProviderError):
            RoundRobinStrategy().select(providers, health_tracker)

    def test_weighted_raises(self, providers, health_tracker):
        for p in providers:
            health_tracker.mark_unhealthy(p.provider, cooldown_seconds=300)
        with pytest.raises(NoHealthyProviderError):
            WeightedStrategy().select(providers, health_tracker)

    def test_least_latency_raises(self, providers, health_tracker):
        for p in providers:
            health_tracker.mark_unhealthy(p.provider, cooldown_seconds=300)
        with pytest.raises(NoHealthyProviderError):
            LeastLatencyStrategy().select(providers, health_tracker)

    def test_cost_optimized_raises(self, providers, health_tracker):
        for p in providers:
            health_tracker.mark_unhealthy(p.provider, cooldown_seconds=300)
        with pytest.raises(NoHealthyProviderError):
            CostOptimizedStrategy().select(providers, health_tracker)

    def test_empty_providers_list(self, health_tracker):
        with pytest.raises(NoHealthyProviderError):
            RoundRobinStrategy().select([], health_tracker)


# --- RoundRobinStrategy ---

class TestRoundRobin:
    def test_cycles_through_providers(self, providers, health_tracker):
        strategy = RoundRobinStrategy()
        results = [strategy.select(providers, health_tracker) for _ in range(6)]
        # Should cycle: 0, 1, 2, 0, 1, 2
        expected = [providers[i % 3] for i in range(6)]
        assert results == expected

    def test_skips_unhealthy(self, providers, health_tracker):
        health_tracker.mark_unhealthy("anthropic", cooldown_seconds=300)
        strategy = RoundRobinStrategy()
        results = [strategy.select(providers, health_tracker) for _ in range(4)]
        for r in results:
            assert r.provider != "anthropic"

    def test_single_provider(self, health_tracker):
        single = [ProviderModelMapping(provider="openai", model_id="gpt-4")]
        strategy = RoundRobinStrategy()
        assert strategy.select(single, health_tracker).provider == "openai"


# --- WeightedStrategy ---

class TestWeighted:
    def test_returns_valid_provider(self, providers, health_tracker):
        strategy = WeightedStrategy()
        result = strategy.select(providers, health_tracker)
        assert result in providers

    def test_skips_unhealthy(self, providers, health_tracker):
        health_tracker.mark_unhealthy("openai", cooldown_seconds=300)
        strategy = WeightedStrategy()
        for _ in range(20):
            result = strategy.select(providers, health_tracker)
            assert result.provider != "openai"

    def test_distribution_roughly_proportional(self, providers, health_tracker):
        """With enough samples, weighted distribution should be roughly proportional."""
        strategy = WeightedStrategy()
        counts = {"openai": 0, "anthropic": 0, "bedrock": 0}
        n = 3000
        for _ in range(n):
            result = strategy.select(providers, health_tracker)
            counts[result.provider] += 1
        # weights: 0.7, 0.3, 0.5 → total 1.5
        # expected ratios: ~0.467, ~0.2, ~0.333
        assert counts["openai"] > counts["anthropic"]
        assert counts["openai"] > counts["bedrock"]


# --- LeastLatencyStrategy ---

class TestLeastLatency:
    def test_selects_lowest_latency(self, providers, health_tracker):
        health_tracker.record_latency("openai", 100.0)
        health_tracker.record_latency("anthropic", 50.0)
        health_tracker.record_latency("bedrock", 200.0)
        strategy = LeastLatencyStrategy(window_seconds=60)
        result = strategy.select(providers, health_tracker)
        assert result.provider == "anthropic"

    def test_no_latency_data_uses_inf(self, providers, health_tracker):
        # Only record for one provider — others default to inf
        health_tracker.record_latency("bedrock", 150.0)
        strategy = LeastLatencyStrategy(window_seconds=60)
        result = strategy.select(providers, health_tracker)
        assert result.provider == "bedrock"

    def test_skips_unhealthy(self, providers, health_tracker):
        health_tracker.record_latency("anthropic", 10.0)
        health_tracker.record_latency("openai", 100.0)
        health_tracker.mark_unhealthy("anthropic", cooldown_seconds=300)
        strategy = LeastLatencyStrategy(window_seconds=60)
        result = strategy.select(providers, health_tracker)
        assert result.provider == "openai"


# --- CostOptimizedStrategy ---

class TestCostOptimized:
    def test_selects_cheapest(self, providers, health_tracker):
        strategy = CostOptimizedStrategy()
        result = strategy.select(providers, health_tracker)
        # anthropic: 0.003 + 0.015 = 0.018 (cheapest)
        assert result.provider == "anthropic"

    def test_skips_unhealthy(self, providers, health_tracker):
        health_tracker.mark_unhealthy("anthropic", cooldown_seconds=300)
        strategy = CostOptimizedStrategy()
        result = strategy.select(providers, health_tracker)
        # bedrock: 0.005 + 0.02 = 0.025 (next cheapest)
        assert result.provider == "bedrock"

    def test_no_pricing_treated_as_inf(self, health_tracker):
        providers = [
            ProviderModelMapping(provider="a", model_id="m1", pricing=None),
            ProviderModelMapping(
                provider="b",
                model_id="m2",
                pricing=TokenPricing(0.001, 0.002),
            ),
        ]
        strategy = CostOptimizedStrategy()
        result = strategy.select(providers, health_tracker)
        assert result.provider == "b"
