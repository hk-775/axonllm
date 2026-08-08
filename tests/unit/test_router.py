"""Unit tests for Router with retry and fallback logic."""

import pytest

from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    TokenPricing,
    TokenUsage,
    ModelConfig,
)
from src.gateway.router import (
    AllProvidersExhaustedError,
    ProviderError,
    Router,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(model: str = "gpt-4") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        model=model,
    )


def _make_response(provider: str = "openai") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": "hello"}}],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="gpt-4",
        provider=provider,
    )


def _build_registry() -> ModelRegistry:
    """Build a ModelRegistry with a single model backed by 3 providers."""
    registry = ModelRegistry()
    registry.models["gpt-4"] = ModelConfig(
        name="gpt-4",
        description="GPT-4",
        providers=[
            ProviderModelMapping(
                provider="openai", model_id="gpt-4-turbo",
                fallback_order=1, pricing=TokenPricing(0.01, 0.03),
            ),
            ProviderModelMapping(
                provider="azure", model_id="gpt-4-azure",
                fallback_order=2, pricing=TokenPricing(0.01, 0.03),
            ),
            ProviderModelMapping(
                provider="bedrock", model_id="gpt-4-bedrock",
                fallback_order=3, pricing=TokenPricing(0.005, 0.02),
            ),
        ],
    )
    return registry


@pytest.fixture
def registry():
    return _build_registry()


@pytest.fixture
def health_tracker():
    return ProviderHealthTracker()


@pytest.fixture
def router(registry, health_tracker):
    return Router(
        model_registry=registry,
        health_tracker=health_tracker,
        max_retries=2,
        base_delay=0.0,  # no real delay in tests
        cooldown_seconds=60,
    )


# ---------------------------------------------------------------------------
# get_fallback_chain
# ---------------------------------------------------------------------------

class TestGetFallbackChain:
    def test_returns_sorted_by_fallback_order(self, router):
        chain = router.get_fallback_chain("gpt-4")
        orders = [m.fallback_order for m in chain]
        assert orders == sorted(orders)

    def test_unknown_model_raises(self, router):
        with pytest.raises(KeyError):
            router.get_fallback_chain("nonexistent")

    def test_runtime_provider_allowlist_filters_the_chain(
        self,
        registry,
        health_tracker,
    ):
        router = Router(
            registry,
            health_tracker,
            available_providers=frozenset({"bedrock"}),
        )

        assert [
            mapping.provider
            for mapping in router.get_fallback_chain("gpt-4")
        ] == ["bedrock"]


# ---------------------------------------------------------------------------
# execute_with_fallback — success cases
# ---------------------------------------------------------------------------

class TestExecuteSuccess:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self, router):
        """First provider succeeds — no fallback needed."""
        async def provider_fn(mapping):
            return _make_response(mapping.provider)

        resp = await router.execute_with_fallback(_make_request(), provider_fn)
        assert resp.provider == "openai"

    @pytest.mark.asyncio
    async def test_falls_back_on_non_retryable_error(self, router):
        """First provider returns 400 → skip to second which succeeds."""
        call_count = 0

        async def provider_fn(mapping):
            nonlocal call_count
            call_count += 1
            if mapping.provider == "openai":
                raise ProviderError(400, "openai", "bad request")
            return _make_response(mapping.provider)

        resp = await router.execute_with_fallback(_make_request(), provider_fn)
        assert resp.provider == "azure"
        assert call_count == 2  # openai once (no retry), azure once

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self, router):
        """Provider fails with 500 once, then succeeds on retry."""
        attempts = 0

        async def provider_fn(mapping):
            nonlocal attempts
            attempts += 1
            if mapping.provider == "openai" and attempts == 1:
                raise ProviderError(500, "openai", "internal error")
            return _make_response(mapping.provider)

        resp = await router.execute_with_fallback(_make_request(), provider_fn)
        assert resp.provider == "openai"
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_disabled_providers_are_never_invoked(
        self,
        registry,
        health_tracker,
    ):
        router = Router(
            registry,
            health_tracker,
            available_providers=frozenset({"bedrock"}),
        )
        invoked: list[str] = []

        async def provider_fn(mapping):
            invoked.append(mapping.provider)
            return _make_response(mapping.provider)

        response = await router.execute_with_fallback(
            _make_request(),
            provider_fn,
        )

        assert response.provider == "bedrock"
        assert invoked == ["bedrock"]


# ---------------------------------------------------------------------------
# execute_with_fallback — retry behavior
# ---------------------------------------------------------------------------

class TestRetryBehavior:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    async def test_retries_retryable_errors(self, status):
        """Retryable errors trigger retries up to max_retries per provider."""
        r = Router(_build_registry(), ProviderHealthTracker(), max_retries=2, base_delay=0.0)
        call_count = 0

        async def provider_fn(mapping):
            nonlocal call_count
            call_count += 1
            raise ProviderError(status, mapping.provider, "error")

        with pytest.raises(AllProvidersExhaustedError):
            await r.execute_with_fallback(_make_request(), provider_fn)

        # Each provider: 1 initial + 2 retries = 3 attempts; 3 providers = 9
        assert call_count == 9, f"status {status}: expected 9 calls, got {call_count}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_no_retry_for_non_retryable(self, status):
        """Non-retryable errors skip to next provider immediately."""
        r = Router(_build_registry(), ProviderHealthTracker(), max_retries=2, base_delay=0.0)
        call_count = 0

        async def provider_fn(mapping):
            nonlocal call_count
            call_count += 1
            raise ProviderError(status, mapping.provider, "error")

        with pytest.raises(AllProvidersExhaustedError):
            await r.execute_with_fallback(_make_request(), provider_fn)

        # 3 providers × 1 attempt each (no retries) = 3
        assert call_count == 3, f"status {status}: expected 3 calls, got {call_count}"


# ---------------------------------------------------------------------------
# execute_with_fallback — all providers exhausted
# ---------------------------------------------------------------------------

class TestAllProvidersExhausted:
    @pytest.mark.asyncio
    async def test_raises_with_attempt_summary(self, router):
        """When all providers fail, error includes summary of all attempts."""
        async def provider_fn(mapping):
            raise ProviderError(500, mapping.provider, "down")

        with pytest.raises(AllProvidersExhaustedError) as exc_info:
            await router.execute_with_fallback(_make_request(), provider_fn)

        err = exc_info.value
        providers_in_attempts = [a["provider"] for a in err.attempts]
        assert "openai" in providers_in_attempts
        assert "azure" in providers_in_attempts
        assert "bedrock" in providers_in_attempts

    @pytest.mark.asyncio
    async def test_mixed_errors_comprehensive_summary(self, router):
        """Mix of retryable and non-retryable errors across providers."""
        async def provider_fn(mapping):
            if mapping.provider == "openai":
                raise ProviderError(429, "openai", "rate limited")
            elif mapping.provider == "azure":
                raise ProviderError(401, "azure", "unauthorized")
            else:
                raise ProviderError(503, "bedrock", "unavailable")

        with pytest.raises(AllProvidersExhaustedError) as exc_info:
            await router.execute_with_fallback(_make_request(), provider_fn)

        err = exc_info.value
        assert len(err.attempts) == 3
        # openai: retried and exhausted (retryable)
        assert err.attempts[0]["provider"] == "openai"
        assert err.attempts[0]["status_code"] == 429
        # azure: skipped immediately (non-retryable)
        assert err.attempts[1]["provider"] == "azure"
        assert err.attempts[1]["status_code"] == 401
        # bedrock: retried and exhausted (retryable)
        assert err.attempts[2]["provider"] == "bedrock"
        assert err.attempts[2]["status_code"] == 503


# ---------------------------------------------------------------------------
# Unhealthy provider handling
# ---------------------------------------------------------------------------

class TestUnhealthyProviders:
    @pytest.mark.asyncio
    async def test_skips_unhealthy_providers(self, router, health_tracker):
        """Unhealthy providers are skipped in the fallback chain."""
        health_tracker.mark_unhealthy("openai", cooldown_seconds=300)

        async def provider_fn(mapping):
            return _make_response(mapping.provider)

        resp = await router.execute_with_fallback(_make_request(), provider_fn)
        assert resp.provider == "azure"

    @pytest.mark.asyncio
    async def test_marks_unhealthy_after_retries_exhausted(self, router, health_tracker):
        """Provider is marked unhealthy after all retries are exhausted."""
        async def provider_fn(mapping):
            if mapping.provider == "openai":
                raise ProviderError(500, "openai", "down")
            return _make_response(mapping.provider)

        resp = await router.execute_with_fallback(_make_request(), provider_fn)
        assert resp.provider == "azure"
        assert not health_tracker.is_healthy("openai")

    @pytest.mark.asyncio
    async def test_non_retryable_does_not_mark_unhealthy(self, router, health_tracker):
        """Non-retryable errors do NOT mark the provider as unhealthy."""
        async def provider_fn(mapping):
            if mapping.provider == "openai":
                raise ProviderError(400, "openai", "bad request")
            return _make_response(mapping.provider)

        resp = await router.execute_with_fallback(_make_request(), provider_fn)
        assert resp.provider == "azure"
        # openai should still be healthy — 400 is not a health issue
        assert health_tracker.is_healthy("openai")

    @pytest.mark.asyncio
    async def test_all_unhealthy_raises(self, router, health_tracker):
        """If all providers are unhealthy, raises with skip summary."""
        health_tracker.mark_unhealthy("openai", cooldown_seconds=300)
        health_tracker.mark_unhealthy("azure", cooldown_seconds=300)
        health_tracker.mark_unhealthy("bedrock", cooldown_seconds=300)

        async def provider_fn(mapping):
            return _make_response(mapping.provider)

        with pytest.raises(AllProvidersExhaustedError) as exc_info:
            await router.execute_with_fallback(_make_request(), provider_fn)

        err = exc_info.value
        assert all(a["message"] == "skipped (unhealthy)" for a in err.attempts)


# ---------------------------------------------------------------------------
# ProviderError
# ---------------------------------------------------------------------------

class TestProviderError:
    def test_attributes(self):
        err = ProviderError(429, "openai", "rate limited")
        assert err.status_code == 429
        assert err.provider == "openai"
        assert err.message == "rate limited"

    def test_str_representation(self):
        err = ProviderError(500, "azure", "internal error")
        assert "[azure] 500: internal error" in str(err)
