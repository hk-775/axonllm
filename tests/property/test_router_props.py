# Feature: litellm-service, Properties 8-14: Router property tests
"""Property-based tests for the Router, fallback/retry logic, and routing strategies.

Properties covered:
  8  – Retryable errors trigger retries with exponential backoff
  9  – Non-retryable errors skip to next fallback provider
  10 – Fallback chain exhaustion returns comprehensive error
  11 – Unhealthy providers are excluded from routing for the cooldown period
  12 – Weighted routing distributes proportionally to weights
  13 – Least-latency routing selects the fastest provider
  14 – Cost-optimized routing selects the cheapest healthy provider
"""

import asyncio
import time
from collections import Counter

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.router import Router, ProviderError, AllProvidersExhaustedError
from src.gateway.routing import (
    RoundRobinStrategy,
    WeightedStrategy,
    LeastLatencyStrategy,
    CostOptimizedStrategy,
    NoHealthyProviderError,
)
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ProviderModelMapping,
    TokenPricing,
    ChatCompletionRequest,
    ChatCompletionResponse,
    TokenUsage,
    ModelConfig,
)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

VALID_PROVIDERS = ["openai", "anthropic", "bedrock", "azure_openai", "vertex_ai", "cohere"]

provider_name_strategy = st.sampled_from(VALID_PROVIDERS)

RETRYABLE_CODES = [429, 500, 502, 503, 504]
NON_RETRYABLE_CODES = [400, 401, 403]

retryable_status_strategy = st.sampled_from(RETRYABLE_CODES)
non_retryable_status_strategy = st.sampled_from(NON_RETRYABLE_CODES)


def _make_request(model: str = "test-model") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model=model,
    )


def _make_response(provider: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": "hi"}}],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="test-model",
        provider=provider,
    )


def _build_registry(providers: list[ProviderModelMapping], model_name: str = "test-model") -> ModelRegistry:
    registry = ModelRegistry()
    registry.models[model_name] = ModelConfig(
        name=model_name,
        description="test model",
        providers=providers,
    )
    return registry


# ===========================================================================
# Property 8: Retryable errors trigger retries with exponential backoff
# Feature: litellm-service, Property 8
# Validates: Requirements 5.1
# ===========================================================================


@given(
    status_code=retryable_status_strategy,
    max_retries=st.integers(min_value=0, max_value=5),
)
@settings(max_examples=100)
def test_retryable_errors_trigger_retries(status_code, max_retries):
    """Property 8: Retryable errors trigger retries with exponential backoff.

    For any retryable HTTP status code (429, 500, 502, 503, 504) and any max
    retry count N, the Router SHALL attempt the request exactly N+1 times
    (1 initial + N retries) with exponentially increasing delays before moving
    to the fallback chain.

    **Validates: Requirements 5.1**
    """
    provider = ProviderModelMapping(
        provider="openai", model_id="gpt-4", fallback_order=1,
    )
    registry = _build_registry([provider])
    health_tracker = ProviderHealthTracker()
    router = Router(
        model_registry=registry,
        health_tracker=health_tracker,
        max_retries=max_retries,
        base_delay=0.0,  # no real delays
        cooldown_seconds=60,
    )

    call_count = 0

    async def provider_fn(mapping):
        nonlocal call_count
        call_count += 1
        raise ProviderError(status_code, mapping.provider, "error")

    loop = asyncio.new_event_loop()
    try:
        try:
            loop.run_until_complete(
                router.execute_with_fallback(_make_request(), provider_fn)
            )
        except AllProvidersExhaustedError:
            pass

        expected_calls = max_retries + 1  # 1 initial + N retries
        assert call_count == expected_calls, (
            f"status={status_code}, max_retries={max_retries}: "
            f"expected {expected_calls} calls, got {call_count}"
        )
    finally:
        loop.close()


# ===========================================================================
# Property 9: Non-retryable errors skip to next fallback provider
# Feature: litellm-service, Property 9
# Validates: Requirements 5.4
# ===========================================================================


@given(
    status_code=non_retryable_status_strategy,
    num_providers=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_non_retryable_errors_skip_to_next_provider(status_code, num_providers):
    """Property 9: Non-retryable errors skip to next fallback provider.

    For any non-retryable HTTP status code (400, 401, 403) returned by a
    provider, the Router SHALL not retry and SHALL immediately attempt the
    next provider in the Fallback_Chain.

    **Validates: Requirements 5.4**
    """
    providers = [
        ProviderModelMapping(
            provider=f"provider_{i}", model_id=f"model_{i}", fallback_order=i,
        )
        for i in range(num_providers)
    ]
    registry = _build_registry(providers)
    health_tracker = ProviderHealthTracker()
    router = Router(
        model_registry=registry,
        health_tracker=health_tracker,
        max_retries=3,  # retries should NOT happen for non-retryable
        base_delay=0.0,
        cooldown_seconds=60,
    )

    call_log: list[str] = []

    async def provider_fn(mapping):
        call_log.append(mapping.provider)
        raise ProviderError(status_code, mapping.provider, "error")

    loop = asyncio.new_event_loop()
    try:
        try:
            loop.run_until_complete(
                router.execute_with_fallback(_make_request(), provider_fn)
            )
        except AllProvidersExhaustedError:
            pass

        # Each provider should be called exactly once (no retries)
        assert len(call_log) == num_providers, (
            f"status={status_code}, providers={num_providers}: "
            f"expected {num_providers} calls, got {len(call_log)}"
        )
        # Each provider called in fallback order
        expected_order = [f"provider_{i}" for i in range(num_providers)]
        assert call_log == expected_order, (
            f"Expected call order {expected_order}, got {call_log}"
        )
    finally:
        loop.close()


# ===========================================================================
# Property 10: Fallback chain exhaustion returns comprehensive error
# Feature: litellm-service, Property 10
# Validates: Requirements 5.2, 5.3
# ===========================================================================


@given(
    num_providers=st.integers(min_value=1, max_value=5),
    status_code=st.sampled_from(RETRYABLE_CODES + NON_RETRYABLE_CODES),
)
@settings(max_examples=100)
def test_fallback_chain_exhaustion_returns_comprehensive_error(num_providers, status_code):
    """Property 10: Fallback chain exhaustion returns comprehensive error.

    For any Fallback_Chain where all providers fail, the Gateway_Agent SHALL
    return the last error received and a summary listing every attempted
    provider and its failure reason.

    **Validates: Requirements 5.2, 5.3**
    """
    providers = [
        ProviderModelMapping(
            provider=f"provider_{i}", model_id=f"model_{i}", fallback_order=i,
        )
        for i in range(num_providers)
    ]
    registry = _build_registry(providers)
    health_tracker = ProviderHealthTracker()
    router = Router(
        model_registry=registry,
        health_tracker=health_tracker,
        max_retries=1,
        base_delay=0.0,
        cooldown_seconds=60,
    )

    error_messages = {f"provider_{i}": f"failure_{i}" for i in range(num_providers)}

    async def provider_fn(mapping):
        raise ProviderError(status_code, mapping.provider, error_messages[mapping.provider])

    loop = asyncio.new_event_loop()
    try:
        try:
            loop.run_until_complete(
                router.execute_with_fallback(_make_request(), provider_fn)
            )
            # Should not reach here
            assert False, "Expected AllProvidersExhaustedError"
        except AllProvidersExhaustedError as exc:
            # Every provider must appear in the attempts summary
            attempted_providers = {a["provider"] for a in exc.attempts}
            expected_providers = {f"provider_{i}" for i in range(num_providers)}
            assert attempted_providers == expected_providers, (
                f"Expected providers {expected_providers} in attempts, "
                f"got {attempted_providers}"
            )

            # Each attempt must have a status_code and message
            for attempt in exc.attempts:
                assert "provider" in attempt
                assert "status_code" in attempt
                assert "message" in attempt
                assert attempt["status_code"] == status_code
                assert attempt["message"] == error_messages[attempt["provider"]]

            # The error string should mention all providers
            error_str = str(exc)
            for i in range(num_providers):
                assert f"provider_{i}" in error_str, (
                    f"provider_{i} not found in error summary: {error_str}"
                )
    finally:
        loop.close()


# ===========================================================================
# Property 11: Unhealthy providers are excluded from routing for cooldown
# Feature: litellm-service, Property 11
# Validates: Requirements 5.5, 6.5
# ===========================================================================


@given(
    cooldown=st.integers(min_value=1, max_value=300),
    num_providers=st.integers(min_value=2, max_value=5),
    unhealthy_index=st.integers(min_value=0),
)
@settings(max_examples=100)
def test_unhealthy_providers_excluded_during_cooldown(cooldown, num_providers, unhealthy_index):
    """Property 11: Unhealthy providers are excluded from routing for the cooldown period.

    For any provider marked as unhealthy with a cooldown period, the Router
    SHALL exclude that provider from all routing and load balancing decisions
    until the cooldown expires.

    **Validates: Requirements 5.5, 6.5**
    """
    unhealthy_index = unhealthy_index % num_providers

    providers = [
        ProviderModelMapping(
            provider=f"provider_{i}", model_id=f"model_{i}", fallback_order=i,
        )
        for i in range(num_providers)
    ]
    registry = _build_registry(providers)
    health_tracker = ProviderHealthTracker()

    unhealthy_provider = f"provider_{unhealthy_index}"

    # Freeze time for deterministic behavior
    base_time = 1000.0
    original_time = time.time
    time.time = lambda: base_time

    try:
        health_tracker.mark_unhealthy(unhealthy_provider, cooldown_seconds=cooldown)

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            max_retries=0,
            base_delay=0.0,
            cooldown_seconds=cooldown,
        )

        called_providers: list[str] = []

        async def provider_fn(mapping):
            called_providers.append(mapping.provider)
            return _make_response(mapping.provider)

        loop = asyncio.new_event_loop()
        try:
            # During cooldown: unhealthy provider should be skipped
            loop.run_until_complete(
                router.execute_with_fallback(_make_request(), provider_fn)
            )
            assert unhealthy_provider not in called_providers, (
                f"{unhealthy_provider} should be excluded during cooldown, "
                f"but was called: {called_providers}"
            )

            # After cooldown expires: provider should be available again
            called_providers.clear()
            time.time = lambda: base_time + cooldown + 1

            loop.run_until_complete(
                router.execute_with_fallback(_make_request(), provider_fn)
            )
            # The first provider in fallback order should be called
            # (which may or may not be the previously unhealthy one)
            # Key assertion: the unhealthy provider is no longer excluded
            assert health_tracker.is_healthy(unhealthy_provider), (
                f"{unhealthy_provider} should be healthy after cooldown expired"
            )
        finally:
            loop.close()
    finally:
        time.time = original_time


# ===========================================================================
# Property 12: Weighted routing distributes proportionally to weights
# Feature: litellm-service, Property 12
# Validates: Requirements 6.2
# ===========================================================================


@st.composite
def weighted_providers_strategy(draw):
    """Generate 2-5 providers with random positive weights."""
    n = draw(st.integers(min_value=2, max_value=5))
    providers = []
    for i in range(n):
        weight = draw(st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False))
        providers.append(
            ProviderModelMapping(
                provider=f"provider_{i}",
                model_id=f"model_{i}",
                weight=weight,
            )
        )
    return providers


@given(providers=weighted_providers_strategy())
@settings(max_examples=100)
def test_weighted_routing_distributes_proportionally(providers):
    """Property 12: Weighted routing distributes proportionally to weights.

    For any set of healthy providers with configured weights and a sufficiently
    large number of requests, the WeightedStrategy SHALL distribute requests
    such that each provider's share is approximately proportional to its weight
    (within statistical tolerance).

    **Validates: Requirements 6.2**
    """
    health_tracker = ProviderHealthTracker()
    strategy = WeightedStrategy()

    num_requests = 5000
    counts: Counter = Counter()

    for _ in range(num_requests):
        selected = strategy.select(providers, health_tracker)
        counts[selected.provider] += 1

    total_weight = sum(p.weight for p in providers)

    for p in providers:
        expected_ratio = p.weight / total_weight
        actual_ratio = counts[p.provider] / num_requests
        # Allow tolerance of 5 percentage points for statistical variance
        assert abs(actual_ratio - expected_ratio) < 0.05, (
            f"Provider {p.provider}: weight={p.weight}, "
            f"expected_ratio={expected_ratio:.3f}, actual_ratio={actual_ratio:.3f}, "
            f"diff={abs(actual_ratio - expected_ratio):.3f}"
        )


# ===========================================================================
# Property 13: Least-latency routing selects the fastest provider
# Feature: litellm-service, Property 13
# Validates: Requirements 6.3
# ===========================================================================


@st.composite
def latency_providers_strategy(draw):
    """Generate 2-5 providers with distinct latency values."""
    n = draw(st.integers(min_value=2, max_value=5))
    providers = []
    latencies = {}
    for i in range(n):
        latency = draw(st.floats(min_value=1.0, max_value=5000.0, allow_nan=False, allow_infinity=False))
        prov = ProviderModelMapping(
            provider=f"provider_{i}",
            model_id=f"model_{i}",
        )
        providers.append(prov)
        latencies[f"provider_{i}"] = latency
    return providers, latencies


@given(data=latency_providers_strategy())
@settings(max_examples=100)
def test_least_latency_selects_fastest_provider(data):
    """Property 13: Least-latency routing selects the fastest provider.

    For any set of healthy providers with known latency histories, the
    LeastLatencyStrategy SHALL route the request to the provider with the
    lowest average response time over the configured sliding window.

    **Validates: Requirements 6.3**
    """
    providers, latencies = data
    health_tracker = ProviderHealthTracker()

    base_time = 1000.0
    original_time = time.time
    time.time = lambda: base_time

    try:
        # Record latencies for each provider
        for prov_name, latency in latencies.items():
            health_tracker.record_latency(prov_name, latency)

        strategy = LeastLatencyStrategy(window_seconds=60)
        selected = strategy.select(providers, health_tracker)

        # Find the provider with the lowest latency
        expected_provider = min(latencies, key=latencies.get)
        assert selected.provider == expected_provider, (
            f"Expected {expected_provider} (latency={latencies[expected_provider]:.1f}), "
            f"got {selected.provider} (latency={latencies[selected.provider]:.1f})"
        )
    finally:
        time.time = original_time


# ===========================================================================
# Property 14: Cost-optimized routing selects the cheapest healthy provider
# Feature: litellm-service, Property 14
# Validates: Requirements 6.4
# ===========================================================================


@st.composite
def cost_providers_strategy(draw):
    """Generate 2-5 providers with random pricing, at least one healthy."""
    n = draw(st.integers(min_value=2, max_value=5))
    providers = []
    unhealthy_indices = set()

    # Ensure at least one provider is healthy
    guaranteed_healthy = draw(st.integers(min_value=0, max_value=n - 1))

    for i in range(n):
        prompt_cost = draw(st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False))
        completion_cost = draw(st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False))
        prov = ProviderModelMapping(
            provider=f"provider_{i}",
            model_id=f"model_{i}",
            pricing=TokenPricing(
                prompt_token_cost=prompt_cost,
                completion_token_cost=completion_cost,
            ),
        )
        providers.append(prov)

        # Randomly mark some as unhealthy (but not the guaranteed healthy one)
        if i != guaranteed_healthy and draw(st.booleans()):
            unhealthy_indices.add(i)

    return providers, unhealthy_indices


@given(data=cost_providers_strategy())
@settings(max_examples=100)
def test_cost_optimized_selects_cheapest_healthy_provider(data):
    """Property 14: Cost-optimized routing selects the cheapest healthy provider.

    For any set of providers with known per-token costs and health statuses,
    the CostOptimizedStrategy SHALL route the request to the healthy provider
    with the lowest per-token cost.

    **Validates: Requirements 6.4**
    """
    providers, unhealthy_indices = data
    health_tracker = ProviderHealthTracker()

    # Mark unhealthy providers
    for idx in unhealthy_indices:
        health_tracker.mark_unhealthy(f"provider_{idx}", cooldown_seconds=300)

    strategy = CostOptimizedStrategy()
    selected = strategy.select(providers, health_tracker)

    # Compute expected: cheapest healthy provider
    healthy_providers = [
        p for i, p in enumerate(providers) if i not in unhealthy_indices
    ]
    assert len(healthy_providers) > 0, "At least one provider must be healthy"

    def cost_key(p):
        if p.pricing is None:
            return float("inf")
        return p.pricing.prompt_token_cost + p.pricing.completion_token_cost

    expected = min(healthy_providers, key=cost_key)
    assert selected.provider == expected.provider, (
        f"Expected cheapest healthy provider {expected.provider} "
        f"(cost={cost_key(expected):.4f}), "
        f"got {selected.provider} (cost={cost_key(selected):.4f})"
    )
