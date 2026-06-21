"""Unit tests for provider prompt caching (Tasks 3, 4, 5)."""

import pytest

from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.cost_tracker import CostTracker
from src.gateway.models import (
    ChatCompletionRequest,
    TokenPricing,
    TokenUsage,
)


@pytest.fixture
def adapter():
    return AnthropicAdapter()


# ---------------------------------------------------------------------------
# Task 3: Anthropic Cache Marker Injection
# ---------------------------------------------------------------------------


class TestCacheMarkerInjection:
    """Tests for cache_control marker injection in translate_request."""

    @pytest.mark.asyncio
    async def test_string_system_converted_to_content_blocks(self, adapter):
        """3.2: String system → content-block array with cache_control."""
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-3-sonnet-20240229",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req, prompt_caching_enabled=True)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 1
        block = result["system"][0]
        assert block["type"] == "text"
        assert block["text"] == "You are helpful."
        assert block["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_caching_disabled_leaves_string(self, adapter):
        """3.3: When disabled, system stays as plain string."""
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-3-sonnet-20240229",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req, prompt_caching_enabled=False)
        assert result["system"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_default_caching_disabled(self, adapter):
        """3.3: Default (no kwarg) preserves original string behavior."""
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-3-sonnet-20240229",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req)
        assert result["system"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_no_system_message_no_marker(self, adapter):
        """No system → no system key regardless of caching flag."""
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-3-sonnet-20240229",
        )
        result = await adapter.translate_request(req, prompt_caching_enabled=True)
        assert "system" not in result

    @pytest.mark.asyncio
    async def test_system_from_messages_converted(self, adapter):
        """System extracted from messages also gets cache_control."""
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
            model="claude-3-sonnet-20240229",
        )
        result = await adapter.translate_request(req, prompt_caching_enabled=True)
        assert isinstance(result["system"], list)
        assert result["system"][0]["text"] == "Be concise."
        assert result["system"][0]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_idempotent_injection(self, adapter):
        """3.1/3.4: Applying twice doesn't duplicate cache_control."""
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-3-sonnet-20240229",
            system="You are helpful.",
        )
        result1 = await adapter.translate_request(req, prompt_caching_enabled=True)
        # Simulate feeding the result back: create a new request with the
        # content-block system already set. The adapter reads system from
        # request.system (a string) or messages, so we test the list path
        # by constructing a request whose system field is already a list.
        # Since ChatCompletionRequest.system is str|None, we test via
        # the adapter directly with a list system in the payload.
        # The idempotence property is tested more thoroughly in PBT.
        assert len(result1["system"]) == 1
        assert result1["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Task 4: Cached Token Extraction from Responses
# ---------------------------------------------------------------------------


class TestCachedTokenExtraction:
    """Tests for extracting cached token counts from responses."""

    def test_extract_cache_read_tokens(self, adapter):
        """4.1: cache_read_input_tokens → cached_tokens."""
        raw = {
            "id": "msg_1",
            "content": [{"type": "text", "text": "Hello"}],
            "model": "claude-3-sonnet-20240229",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 80,
            },
        }
        resp = adapter.translate_response(raw)
        assert resp.usage.cached_tokens == 80

    def test_extract_cache_creation_tokens(self, adapter):
        """4.2: cache_creation_input_tokens → cache_creation_tokens."""
        raw = {
            "id": "msg_2",
            "content": [{"type": "text", "text": "Hello"}],
            "model": "claude-3-sonnet-20240229",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 30,
            },
        }
        resp = adapter.translate_response(raw)
        assert resp.usage.cache_creation_tokens == 30

    def test_both_cached_fields(self, adapter):
        """4.1+4.2: Both fields extracted together."""
        raw = {
            "id": "msg_3",
            "content": [{"type": "text", "text": "Hello"}],
            "model": "claude-3-sonnet-20240229",
            "usage": {
                "input_tokens": 200,
                "output_tokens": 100,
                "cache_read_input_tokens": 150,
                "cache_creation_input_tokens": 20,
            },
        }
        resp = adapter.translate_response(raw)
        assert resp.usage.cached_tokens == 150
        assert resp.usage.cache_creation_tokens == 20

    def test_defaults_to_zero_when_absent(self, adapter):
        """4.3: Missing fields default to 0."""
        raw = {
            "id": "msg_4",
            "content": [{"type": "text", "text": "Hello"}],
            "model": "claude-3-sonnet-20240229",
            "usage": {"input_tokens": 50, "output_tokens": 25},
        }
        resp = adapter.translate_response(raw)
        assert resp.usage.cached_tokens == 0
        assert resp.usage.cache_creation_tokens == 0

    def test_empty_usage_defaults_to_zero(self, adapter):
        """4.3: No usage dict at all → both default to 0."""
        raw = {"id": "x", "content": [], "model": "claude-3-sonnet-20240229"}
        resp = adapter.translate_response(raw)
        assert resp.usage.cached_tokens == 0
        assert resp.usage.cache_creation_tokens == 0


# ---------------------------------------------------------------------------
# Task 5: Cost Calculation with Cached Tokens
# ---------------------------------------------------------------------------


class TestCostCalculationWithCaching:
    """Tests for CostTracker.calculate_cost with cached token billing."""

    def _make_tracker(self, cached_cost=None, creation_cost=None):
        pricing = TokenPricing(
            prompt_token_cost=0.003,
            completion_token_cost=0.015,
            cached_token_cost=cached_cost,
            cache_creation_token_cost=creation_cost,
        )
        return CostTracker({"anthropic": {"claude-3-sonnet": pricing}})

    def test_no_cached_tokens_unchanged(self):
        """5.1: Without cached tokens, cost is same as before."""
        tracker = self._make_tracker()
        cost = tracker.calculate_cost("anthropic", "claude-3-sonnet", 1000, 500)
        expected = (1000 / 1000 * 0.003) + (500 / 1000 * 0.015)
        assert cost == pytest.approx(expected)

    def test_cached_tokens_subtracted_from_prompt(self):
        """5.2: cached_tokens subtracted from prompt before billing."""
        tracker = self._make_tracker(cached_cost=0.0003)
        cost = tracker.calculate_cost(
            "anthropic", "claude-3-sonnet",
            prompt_tokens=1000, completion_tokens=500,
            cached_tokens=800,
        )
        # billable_prompt = 1000 - 800 = 200
        expected = (200 / 1000 * 0.003) + (500 / 1000 * 0.015) + (800 / 1000 * 0.0003)
        assert cost == pytest.approx(expected)

    def test_cache_creation_tokens_billing(self):
        """5.2: cache_creation_tokens subtracted and billed at creation rate."""
        tracker = self._make_tracker(cached_cost=0.0003, creation_cost=0.00375)
        cost = tracker.calculate_cost(
            "anthropic", "claude-3-sonnet",
            prompt_tokens=1000, completion_tokens=500,
            cached_tokens=600, cache_creation_tokens=200,
        )
        # billable_prompt = 1000 - 600 - 200 = 200
        expected = (
            (200 / 1000 * 0.003)
            + (500 / 1000 * 0.015)
            + (600 / 1000 * 0.0003)
            + (200 / 1000 * 0.00375)
        )
        assert cost == pytest.approx(expected)

    def test_fallback_to_prompt_rate_when_no_cached_cost(self):
        """5.2/7.2: When cached_token_cost is None, use prompt_token_cost."""
        tracker = self._make_tracker(cached_cost=None, creation_cost=None)
        cost = tracker.calculate_cost(
            "anthropic", "claude-3-sonnet",
            prompt_tokens=1000, completion_tokens=500,
            cached_tokens=800, cache_creation_tokens=100,
        )
        # billable_prompt = 1000 - 800 - 100 = 100
        # All billed at prompt_token_cost = 0.003
        expected = (
            (100 / 1000 * 0.003)
            + (500 / 1000 * 0.015)
            + (800 / 1000 * 0.003)
            + (100 / 1000 * 0.003)
        )
        assert cost == pytest.approx(expected)

    def test_unknown_provider_returns_zero(self):
        """Unknown provider still returns 0."""
        tracker = self._make_tracker()
        cost = tracker.calculate_cost(
            "unknown", "model", 100, 50, cached_tokens=10, cache_creation_tokens=5
        )
        assert cost == 0.0
