"""Unit tests for GatewayAgent smart routing integration."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gateway.agent import GatewayAgent, _error_response
from src.gateway.cache_manager import CacheManager
from src.gateway.cost_tracker import CostTracker
from src.gateway.guardrail_engine import GuardrailEngine
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Project,
    ProviderModelMapping,
    RateLimitResult,
    RoutingStrategy,
    SmartRoutingDecision,
    TokenPricing,
    TokenUsage,
    VirtualModelConfig,
)
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.router import Router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    content: str = "Hello!",
    model: str = "claude-sonnet",
    provider: str = "anthropic",
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"index": 0, "message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _make_decision(
    selected_model: str = "claude-sonnet",
    task_type: str = "coding",
    confidence: float = 0.85,
    benchmark_score: float = 90.0,
    used_fallback: bool = False,
) -> SmartRoutingDecision:
    return SmartRoutingDecision(
        task_type=task_type,
        confidence=confidence,
        selected_model=selected_model,
        benchmark_score=benchmark_score,
        candidates_considered=[
            {"model": selected_model, "benchmark_score": benchmark_score, "passed": True}
        ],
        used_fallback=used_fallback,
        cost_quality_tradeoff=0.3,
    )


def _base_context() -> dict:
    return {
        "user_id": "user-1",
        "project_id": "proj-1",
        "roles": ["developer"],
        "scopes": ["chat"],
    }


def _smart_routing_context() -> dict:
    ctx = _base_context()
    ctx["smart_routing"] = True
    return ctx


@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock(spec=SlidingWindowRateLimiter)
    rl.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=True, limit=60, remaining=59,
            reset_at=datetime.utcnow(), retry_after_seconds=None,
        )
    )
    return rl


@pytest.fixture
def cost_tracker():
    pricing = {
        "anthropic": {
            "claude-sonnet": TokenPricing(prompt_token_cost=0.003, completion_token_cost=0.015),
        }
    }
    return CostTracker(pricing_config=pricing)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIsSmartRoutingRequest:
    """Tests for _is_smart_routing_request helper."""

    def test_returns_true_when_context_flag_set(self, mock_rate_limiter, cost_tracker):
        """Returns True when context has smart_routing: True."""
        mock_router = MagicMock(spec=Router)
        mock_router._smart_strategy = MagicMock()  # strategy is configured

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
        )

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet",
        )
        context = {"smart_routing": True}

        assert agent._is_smart_routing_request(request, context) is True

    def test_returns_true_when_model_empty(self, mock_rate_limiter, cost_tracker):
        """Returns True when request model is empty."""
        mock_router = MagicMock(spec=Router)
        mock_router._smart_strategy = MagicMock()

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
        )

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="",
        )
        context = {}

        assert agent._is_smart_routing_request(request, context) is True

    def test_returns_false_when_no_smart_strategy(self, mock_rate_limiter, cost_tracker):
        """Returns False when router has no smart strategy configured."""
        mock_router = MagicMock(spec=Router)
        mock_router._smart_strategy = None

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
        )

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="",
        )
        context = {"smart_routing": True}

        assert agent._is_smart_routing_request(request, context) is False

    def test_returns_false_for_normal_request(self, mock_rate_limiter, cost_tracker):
        """Returns False for a normal request with model specified and no flag."""
        mock_router = MagicMock(spec=Router)
        mock_router._smart_strategy = MagicMock()

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
        )

        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet",
        )
        context = {}

        assert agent._is_smart_routing_request(request, context) is False


class TestSmartRoutingIntegration:
    """Tests for smart routing in handle_chat_completion."""

    @pytest.mark.asyncio
    async def test_smart_routing_includes_metadata_in_response(self, mock_rate_limiter, cost_tracker):
        """When smart routing is triggered, response includes smart_routing metadata."""
        mock_router = MagicMock(spec=Router)
        mock_smart_strategy = MagicMock()
        mock_router._smart_strategy = mock_smart_strategy
        mock_router.execute_with_fallback = AsyncMock()

        decision = _make_decision(selected_model="claude-sonnet", task_type="coding", confidence=0.85)
        response = _make_response()
        mock_router.smart_route = AsyncMock(return_value=(response, decision))

        mock_factory = MagicMock()
        mock_factory.create = MagicMock(return_value=AsyncMock())

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
            provider_fn_factory=mock_factory,
            smart_routing_enabled=True,
        )

        request_data = {
            "messages": [{"role": "user", "content": "Write a Python function"}],
            "model": "",
        }

        result = await agent.handle_chat_completion(request_data, _smart_routing_context())

        assert "smart_routing" in result
        sr = result["smart_routing"]
        assert sr["task_type"] == "coding"
        assert sr["confidence"] == 0.85
        assert sr["selected_model"] == "claude-sonnet"
        assert sr["benchmark_score"] == 90.0
        assert sr["used_fallback"] is False
        assert sr["cost_quality_tradeoff"] == 0.3

    @pytest.mark.asyncio
    async def test_non_smart_routing_no_metadata(self, mock_rate_limiter, cost_tracker):
        """Normal requests do not include smart_routing metadata."""
        mock_router = MagicMock(spec=Router)
        mock_router._smart_strategy = None  # No smart strategy
        response = _make_response(model="claude-sonnet", provider="anthropic")
        mock_router.execute_with_fallback = AsyncMock(return_value=response)
        mock_router.model_registry = MagicMock()

        mock_factory = MagicMock()
        mock_factory.create = MagicMock(return_value=AsyncMock(return_value=response))

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
            provider_fn_factory=mock_factory,
        )

        request_data = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "claude-sonnet",
        }

        result = await agent.handle_chat_completion(request_data, _base_context())

        assert "smart_routing" not in result

    @pytest.mark.asyncio
    async def test_smart_routing_extracts_prompt_from_last_message(self, mock_rate_limiter, cost_tracker):
        """Smart routing extracts the prompt from the last user message."""
        mock_router = MagicMock(spec=Router)
        mock_smart_strategy = MagicMock()
        mock_router._smart_strategy = mock_smart_strategy

        decision = _make_decision()
        response = _make_response()
        mock_router.smart_route = AsyncMock(return_value=(response, decision))

        mock_factory = MagicMock()
        mock_factory.create = MagicMock(return_value=AsyncMock())

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
            provider_fn_factory=mock_factory,
        )

        request_data = {
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Write a sorting algorithm"},
            ],
            "model": "",
        }

        await agent.handle_chat_completion(request_data, _base_context())

        # Verify smart_route was called with the last message content as prompt
        mock_router.smart_route.assert_awaited_once()
        call_args = mock_router.smart_route.call_args
        # prompt is the 3rd positional arg (request, factory, prompt)
        prompt_arg = call_args[0][2]
        assert prompt_arg == "Write a sorting algorithm"

    @pytest.mark.asyncio
    async def test_smart_routing_skips_model_access_check(self, mock_rate_limiter, cost_tracker):
        """Smart routing skips model access check since model is selected later."""
        mock_router = MagicMock(spec=Router)
        mock_smart_strategy = MagicMock()
        mock_router._smart_strategy = mock_smart_strategy

        decision = _make_decision(selected_model="claude-sonnet")
        response = _make_response()
        mock_router.smart_route = AsyncMock(return_value=(response, decision))

        mock_factory = MagicMock()
        mock_factory.create = MagicMock(return_value=AsyncMock())

        project = Project(
            project_id="proj-1",
            name="Test",
            allowed_models=["gpt-4"],  # claude-sonnet NOT in allowed list
        )

        agent = GatewayAgent(
            router=mock_router,
            rate_limiter=mock_rate_limiter,
            guardrail_engine=GuardrailEngine(),
            cache_manager=CacheManager(),
            cost_tracker=cost_tracker,
            provider_fn_factory=mock_factory,
            projects={"proj-1": project},
        )

        # Empty model triggers smart routing, which should skip the model access check
        request_data = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "",
        }

        result = await agent.handle_chat_completion(request_data, _base_context())

        # Should NOT get a 403 error — smart routing handles model selection
        assert result.get("status_code") != 403
        mock_router.smart_route.assert_awaited_once()
