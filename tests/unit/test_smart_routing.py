"""Unit tests for SmartRoutingStrategy."""

import pytest

from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    ProviderModelMapping,
    SmartRoutingDecision,
    TokenPricing,
)
from src.gateway.routing import NoHealthyProviderError
from src.gateway.smart_routing import NoCandidateModelsError, SmartRoutingStrategy
from src.gateway.task_classifier import TaskClassifier


LEADERBOARD_YAML = """\
task_types:
  coding:
    models:
      - name: claude-opus
        score: 95
      - name: gpt-4o
        score: 90
      - name: claude-sonnet
        score: 85
  reasoning:
    models:
      - name: claude-opus
        score: 97
      - name: deepseek-r1
        score: 90
  general:
    models:
      - name: claude-sonnet
        score: 90
      - name: gpt-4o
        score: 88
      - name: nova-pro
        score: 82
smart_routing:
  confidence_threshold: 0.3
  cost_quality_tradeoff: 0.3
  default_model: claude-sonnet
"""

MODELS_YAML = """\
models:
  - name: claude-opus
    description: Claude Opus
    routing_strategy: round-robin
    providers:
      - provider: anthropic
        model_id: claude-opus-4
        weight: 1.0
        fallback_order: 0
        pricing:
          prompt_token_cost: 0.015
          completion_token_cost: 0.075
  - name: gpt-4o
    description: GPT-4o
    routing_strategy: round-robin
    providers:
      - provider: openai
        model_id: gpt-4o
        weight: 1.0
        fallback_order: 0
        pricing:
          prompt_token_cost: 0.0025
          completion_token_cost: 0.01
  - name: claude-sonnet
    description: Claude Sonnet
    routing_strategy: round-robin
    providers:
      - provider: anthropic
        model_id: claude-sonnet-4
        weight: 1.0
        fallback_order: 0
        pricing:
          prompt_token_cost: 0.003
          completion_token_cost: 0.015
  - name: deepseek-r1
    description: DeepSeek R1
    routing_strategy: round-robin
    providers:
      - provider: bedrock
        model_id: deepseek-r1
        weight: 1.0
        fallback_order: 0
        pricing:
          prompt_token_cost: 0.00135
          completion_token_cost: 0.0054
  - name: nova-pro
    description: Nova Pro
    routing_strategy: round-robin
    providers:
      - provider: bedrock
        model_id: nova-pro
        weight: 1.0
        fallback_order: 0
        pricing:
          prompt_token_cost: 0.0008
          completion_token_cost: 0.0032
"""


@pytest.fixture
def health_tracker():
    return ProviderHealthTracker()


@pytest.fixture
def model_registry():
    return ModelRegistry.from_yaml(MODELS_YAML)


@pytest.fixture
def leaderboard():
    valid_models = {"claude-opus", "gpt-4o", "claude-sonnet", "deepseek-r1", "nova-pro"}
    return ModelLeaderboard.from_yaml(LEADERBOARD_YAML, valid_models)


@pytest.fixture
def cost_tracker():
    return CostTracker(pricing_config={})


@pytest.fixture
def feedback_tracker():
    return FeedbackTracker()


@pytest.fixture
def strategy(health_tracker, model_registry, leaderboard, cost_tracker, feedback_tracker):
    return SmartRoutingStrategy(
        classifier=TaskClassifier(),
        leaderboard=leaderboard,
        model_registry=model_registry,
        health_tracker=health_tracker,
        cost_tracker=cost_tracker,
        feedback_tracker=feedback_tracker,
        confidence_threshold=0.3,
        cost_quality_tradeoff=0.3,
        default_model="claude-sonnet",
    )


class TestSelect:
    """Tests for the RoutingStrategyBase.select() interface."""

    def test_select_returns_healthy_provider(self, strategy, health_tracker):
        providers = [
            ProviderModelMapping(provider="anthropic", model_id="claude-opus-4"),
            ProviderModelMapping(provider="openai", model_id="gpt-4o"),
        ]
        result = strategy.select(providers, health_tracker)
        assert result.provider == "anthropic"

    def test_select_skips_unhealthy_provider(self, strategy, health_tracker):
        health_tracker.mark_unhealthy("anthropic", 60)
        providers = [
            ProviderModelMapping(provider="anthropic", model_id="claude-opus-4"),
            ProviderModelMapping(provider="openai", model_id="gpt-4o"),
        ]
        result = strategy.select(providers, health_tracker)
        assert result.provider == "openai"

    def test_select_raises_when_all_unhealthy(self, strategy, health_tracker):
        health_tracker.mark_unhealthy("anthropic", 60)
        health_tracker.mark_unhealthy("openai", 60)
        providers = [
            ProviderModelMapping(provider="anthropic", model_id="claude-opus-4"),
            ProviderModelMapping(provider="openai", model_id="gpt-4o"),
        ]
        with pytest.raises(NoHealthyProviderError):
            strategy.select(providers, health_tracker)


class TestSelectModel:
    """Tests for the full smart routing pipeline."""

    @pytest.mark.asyncio
    async def test_high_confidence_uses_leaderboard(self, strategy):
        # "Implement a function and debug the code" → coding with high confidence
        decision = await strategy.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        assert decision.task_type == "coding"
        assert decision.used_fallback is False
        assert decision.selected_model in {"claude-opus", "gpt-4o", "claude-sonnet"}

    @pytest.mark.asyncio
    async def test_low_confidence_uses_fallback(self, strategy):
        # Generic prompt with no strong keywords → low confidence
        decision = await strategy.select_model("Hello there")
        assert decision.used_fallback is True
        assert decision.selected_model == "claude-sonnet"

    @pytest.mark.asyncio
    async def test_allowed_models_filter(self, strategy):
        decision = await strategy.select_model(
            "Implement a function and debug the code and refactor the class method",
            allowed_models={"gpt-4o", "claude-sonnet"},
        )
        assert decision.selected_model in {"gpt-4o", "claude-sonnet"}

    @pytest.mark.asyncio
    async def test_health_filter_excludes_unhealthy(self, strategy, health_tracker):
        # Mark anthropic unhealthy — claude-opus and claude-sonnet only have anthropic
        health_tracker.mark_unhealthy("anthropic", 60)
        decision = await strategy.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        # Should not select models with only anthropic provider
        assert decision.selected_model in {"gpt-4o", "deepseek-r1"}

    @pytest.mark.asyncio
    async def test_all_providers_unhealthy_raises(self, strategy, health_tracker):
        health_tracker.mark_unhealthy("anthropic", 60)
        health_tracker.mark_unhealthy("openai", 60)
        health_tracker.mark_unhealthy("bedrock", 60)
        with pytest.raises(NoCandidateModelsError):
            await strategy.select_model(
                "Implement a function and debug the code and refactor the class method"
            )

    @pytest.mark.asyncio
    async def test_context_window_filter(self, strategy, model_registry):
        # Set a very small context window on claude-opus
        model_registry.models["claude-opus"].max_context_tokens = 10
        # Prompt that estimates to more than 10 tokens
        decision = await strategy.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        assert decision.selected_model != "claude-opus"

    @pytest.mark.asyncio
    async def test_no_context_window_means_no_limit(self, strategy, model_registry):
        # Ensure models without max_context_tokens are not filtered
        model_registry.models["claude-opus"].max_context_tokens = None
        decision = await strategy.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        # claude-opus should still be a candidate
        assert decision.selected_model in {"claude-opus", "gpt-4o", "claude-sonnet"}

    @pytest.mark.asyncio
    async def test_budget_filter(self, strategy, cost_tracker):
        # Register project with exceeded budget
        cost_tracker.register_project("proj-1", budget_limit=0.0)
        with pytest.raises(NoCandidateModelsError):
            await strategy.select_model(
                "Implement a function and debug the code and refactor the class method",
                project_id="proj-1",
            )

    @pytest.mark.asyncio
    async def test_cost_quality_tradeoff_zero_picks_highest_benchmark(
        self, health_tracker, model_registry, leaderboard, cost_tracker, feedback_tracker
    ):
        strat = SmartRoutingStrategy(
            classifier=TaskClassifier(),
            leaderboard=leaderboard,
            model_registry=model_registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=0.3,
            cost_quality_tradeoff=0.0,
            default_model="claude-sonnet",
        )
        decision = await strat.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        # With tradeoff=0, should pick highest benchmark (claude-opus=95)
        assert decision.selected_model == "claude-opus"

    @pytest.mark.asyncio
    async def test_cost_quality_tradeoff_one_picks_cheapest(
        self, health_tracker, model_registry, leaderboard, cost_tracker, feedback_tracker
    ):
        strat = SmartRoutingStrategy(
            classifier=TaskClassifier(),
            leaderboard=leaderboard,
            model_registry=model_registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=0.3,
            cost_quality_tradeoff=1.0,
            default_model="claude-sonnet",
        )
        decision = await strat.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        # With tradeoff=1.0, should pick cheapest model among coding candidates
        # gpt-4o cost: (0.0025+0.01)/2 = 0.00625
        # claude-sonnet cost: (0.003+0.015)/2 = 0.009
        # claude-opus cost: (0.015+0.075)/2 = 0.045
        assert decision.selected_model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_feedback_recorded(self, strategy, feedback_tracker):
        await strategy.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        records = feedback_tracker.get_records()
        assert len(records) == 1
        assert records[0].task_type == "coding"

    @pytest.mark.asyncio
    async def test_candidates_considered_populated(self, strategy):
        decision = await strategy.select_model(
            "Implement a function and debug the code and refactor the class method"
        )
        assert len(decision.candidates_considered) > 0


class TestEstimateTokenCount:
    def test_basic_estimation(self, strategy):
        assert strategy._estimate_token_count("hello world") == max(1, len("hello world") // 4)

    def test_empty_string(self, strategy):
        assert strategy._estimate_token_count("") == 1

    def test_long_string(self, strategy):
        text = "a" * 1000
        assert strategy._estimate_token_count(text) == 250


class TestCompositeScore:
    def test_pure_quality(self, strategy):
        strategy.cost_quality_tradeoff = 0.0
        score = strategy._compute_composite_score(95, 0.01, 100, 0.05)
        assert score == pytest.approx(0.95)

    def test_pure_cost(self, strategy):
        strategy.cost_quality_tradeoff = 1.0
        # cost=0.01, max_cost=0.05 → norm_cost=0.2 → (1-0.2) = 0.8
        score = strategy._compute_composite_score(95, 0.01, 100, 0.05)
        assert score == pytest.approx(0.8)

    def test_balanced(self, strategy):
        strategy.cost_quality_tradeoff = 0.5
        # norm_benchmark = 90/100 = 0.9
        # norm_cost = 0.02/0.05 = 0.4
        # composite = 0.5 * 0.9 + 0.5 * (1 - 0.4) = 0.45 + 0.3 = 0.75
        score = strategy._compute_composite_score(90, 0.02, 100, 0.05)
        assert score == pytest.approx(0.75)

    def test_zero_max_benchmark(self, strategy):
        score = strategy._compute_composite_score(0, 0.01, 0, 0.05)
        # norm_benchmark = 0, norm_cost = 0.2
        # composite = 0.7 * 0 + 0.3 * (1 - 0.2) = 0.24
        assert score == pytest.approx(0.24)

    def test_zero_max_cost(self, strategy):
        score = strategy._compute_composite_score(90, 0, 100, 0)
        # norm_cost = 0 (max_cost=0 → 0)
        # composite = 0.7 * 0.9 + 0.3 * (1 - 0) = 0.63 + 0.3 = 0.93
        assert score == pytest.approx(0.93)
