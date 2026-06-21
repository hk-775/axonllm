"""Routing strategies for distributing requests across healthy providers."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod

from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.models import ProviderModelMapping


class NoHealthyProviderError(Exception):
    """Raised when no healthy providers are available for routing."""


class RoutingStrategyBase(ABC):
    """Base class for all routing strategies."""

    @abstractmethod
    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        """Select a provider from the list of healthy providers.

        Raises ``NoHealthyProviderError`` if no healthy providers exist.
        """
        ...

    def _healthy_providers(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> list[ProviderModelMapping]:
        return [p for p in providers if health_tracker.is_healthy(p.provider)]


class RoundRobinStrategy(RoutingStrategyBase):
    """Cycles through healthy providers sequentially."""

    def __init__(self) -> None:
        self._index = 0

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        selected = healthy[self._index % len(healthy)]
        self._index += 1
        return selected


class WeightedStrategy(RoutingStrategyBase):
    """Distributes requests proportionally to configured weights among healthy providers."""

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        weights = [p.weight for p in healthy]
        return random.choices(healthy, weights=weights, k=1)[0]


class LeastLatencyStrategy(RoutingStrategyBase):
    """Routes to the provider with the lowest average latency in a sliding window."""

    def __init__(self, window_seconds: int = 60) -> None:
        self.window_seconds = window_seconds

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        return min(
            healthy,
            key=lambda p: health_tracker.get_average_latency(
                p.provider, self.window_seconds
            ),
        )


class CostOptimizedStrategy(RoutingStrategyBase):
    """Routes to the cheapest healthy provider based on per-token pricing."""

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        return min(healthy, key=self._cost_key)

    @staticmethod
    def _cost_key(p: ProviderModelMapping) -> float:
        if p.pricing is None:
            return float("inf")
        return p.pricing.prompt_token_cost + p.pricing.completion_token_cost
