"""Unit tests for Router.smart_route method."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    RoutingStrategy,
    SmartRoutingDecision,
    TokenPricing,
    TokenUsage,
    ModelConfig,
)
from src.gateway.router import Router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(model: str = "") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "Write a Python function"}],
        model=model,
    )


def _make_response(provider: str = "openai", model: str = "claude-sonnet") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": "def hello(): pass"}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider=provider,
    )


def _build_registry() -> ModelRegistry:
    """Build a ModelRegistry with models for smart routing tests."""
    registry = ModelRegistry()
    registry.models["claude-sonnet"] = ModelConfig(
        name="claude-sonnet",
        description="Claude Sonnet",
        providers=[
            ProviderModelMapping(
                provider="anthropic", model_id="claude-3-sonnet",
                fallback_order=1, pricing=TokenPricing(0.003, 0.015),
            ),
        ],
        routing_strategy=RoutingStrategy.ROUND_ROBIN,
    )
    registry.models["gpt-4o"] = ModelConfig(
        name="gpt-4o",
        description="GPT-4o",
        providers=[
            ProviderModelMapping(
                provider="openai", model_id="gpt-4o",
                fallback_order=1, pricing=TokenPricing(0.005, 0.015),
            ),
        ],
        routing_strategy=RoutingStrategy.ROUND_ROBIN,
    )
    return registry


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSmartRoute:
    @pytest.mark.asyncio
    async def test_smart_route_selects_model_and_delegates(self):
        """smart_route calls select_model, updates request.model, and delegates to execute_with_fallback."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()

        mock_smart_strategy = MagicMock()
        mock_smart_strategy.select_model = AsyncMock(
            return_value=_make_decision(selected_model="claude-sonnet")
        )

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            max_retries=0,
            base_delay=0.0,
            smart_strategy=mock_smart_strategy,
        )

        request = _make_request(model="")

        mock_factory = MagicMock()
        mock_provider_fn = AsyncMock(return_value=_make_response(provider="anthropic", model="claude-sonnet"))
        mock_factory.create = MagicMock(return_value=mock_provider_fn)

        response, decision = await router.smart_route(
            request, mock_factory, prompt="Write a Python function",
        )

        # Verify select_model was called with the prompt
        mock_smart_strategy.select_model.assert_awaited_once_with(
            "Write a Python function",
            {"claude-sonnet", "gpt-4o"},
            None,
            None,
        )
        # Verify request.model was updated
        assert request.model == "claude-sonnet"
        # Verify factory.create was called with the updated request (spoke=None
        # in the single-region default).
        mock_factory.create.assert_called_once_with(request, spoke=None)
        # Verify response is correct
        assert response.provider == "anthropic"
        assert decision.task_type == "coding"
        assert decision.selected_model == "claude-sonnet"

    @pytest.mark.asyncio
    async def test_smart_route_passes_allowed_models(self):
        """smart_route passes allowed_models to both select_model and execute_with_fallback."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()

        mock_smart_strategy = MagicMock()
        mock_smart_strategy.select_model = AsyncMock(
            return_value=_make_decision(selected_model="gpt-4o")
        )

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            max_retries=0,
            base_delay=0.0,
            smart_strategy=mock_smart_strategy,
        )

        request = _make_request(model="")
        allowed = {"gpt-4o", "claude-sonnet"}

        mock_factory = MagicMock()
        mock_provider_fn = AsyncMock(return_value=_make_response(provider="openai", model="gpt-4o"))
        mock_factory.create = MagicMock(return_value=mock_provider_fn)

        response, decision = await router.smart_route(
            request, mock_factory, prompt="Hello",
            allowed_models=allowed,
        )

        mock_smart_strategy.select_model.assert_awaited_once_with(
            "Hello", allowed, None, None,
        )
        assert decision.selected_model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_smart_route_passes_project_and_user_ids(self):
        """smart_route passes project_id and user_id to select_model."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()

        mock_smart_strategy = MagicMock()
        mock_smart_strategy.select_model = AsyncMock(
            return_value=_make_decision(selected_model="claude-sonnet")
        )

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            max_retries=0,
            base_delay=0.0,
            smart_strategy=mock_smart_strategy,
        )

        request = _make_request(model="")

        mock_factory = MagicMock()
        mock_provider_fn = AsyncMock(return_value=_make_response())
        mock_factory.create = MagicMock(return_value=mock_provider_fn)

        await router.smart_route(
            request, mock_factory, prompt="Test",
            project_id="proj-1", user_id="user-1",
        )

        mock_smart_strategy.select_model.assert_awaited_once_with(
            "Test",
            {"claude-sonnet", "gpt-4o"},
            "proj-1",
            "user-1",
        )

    @pytest.mark.asyncio
    async def test_smart_route_without_strategy_raises(self):
        """smart_route raises RuntimeError when no smart strategy is configured."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            max_retries=0,
            base_delay=0.0,
        )

        request = _make_request(model="")
        mock_factory = MagicMock()

        with pytest.raises(RuntimeError, match="Smart routing strategy not configured"):
            await router.smart_route(request, mock_factory, prompt="Test")

    @pytest.mark.asyncio
    async def test_smart_route_with_fallback_decision(self):
        """smart_route works correctly when the decision uses fallback."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()

        mock_smart_strategy = MagicMock()
        mock_smart_strategy.select_model = AsyncMock(
            return_value=_make_decision(
                selected_model="claude-sonnet",
                task_type="general",
                confidence=0.1,
                used_fallback=True,
            )
        )

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            max_retries=0,
            base_delay=0.0,
            smart_strategy=mock_smart_strategy,
        )

        request = _make_request(model="")

        mock_factory = MagicMock()
        mock_provider_fn = AsyncMock(return_value=_make_response())
        mock_factory.create = MagicMock(return_value=mock_provider_fn)

        response, decision = await router.smart_route(
            request, mock_factory, prompt="hi",
        )

        assert decision.used_fallback is True
        assert decision.task_type == "general"
        assert request.model == "claude-sonnet"

    def test_smart_strategy_registered_in_strategy_map(self):
        """When smart_strategy is provided, it's registered in the strategy map."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()
        mock_smart_strategy = MagicMock()

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
            smart_strategy=mock_smart_strategy,
        )

        assert RoutingStrategy.SMART in router._strategies
        assert router._strategies[RoutingStrategy.SMART] is mock_smart_strategy

    def test_no_smart_strategy_not_in_map(self):
        """When no smart_strategy is provided, SMART is not in the strategy map."""
        registry = _build_registry()
        health_tracker = ProviderHealthTracker()

        router = Router(
            model_registry=registry,
            health_tracker=health_tracker,
        )

        assert RoutingStrategy.SMART not in router._strategies
        assert router._smart_strategy is None
