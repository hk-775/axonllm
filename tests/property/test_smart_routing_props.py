"""Property-based tests for smart routing components."""

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.task_classifier import TaskClassifier
from src.gateway.models import ClassificationResult


class TestTaskClassifierProperties:
    """Property 1: Classification Output Validity.

    **Validates: Requirements 1.1, 1.4, 1.5**

    For any string prompt, TaskClassifier.classify(prompt) returns a
    ClassificationResult where task_type is one of the supported types
    and confidence is in [0.0, 1.0].
    """

    @given(prompt=st.text(min_size=0, max_size=4000))
    @settings(max_examples=200)
    def test_classify_returns_valid_task_type_and_confidence(self, prompt: str):
        """For any string, classify returns valid task_type and confidence in [0.0, 1.0]."""
        classifier = TaskClassifier()
        result = classifier.classify(prompt)

        # Result is a ClassificationResult
        assert isinstance(result, ClassificationResult)

        # task_type is one of the valid types
        assert result.task_type in TaskClassifier.VALID_TASK_TYPES

        # confidence is in [0.0, 1.0]
        assert 0.0 <= result.confidence <= 1.0

        # matched_keywords is a list
        assert isinstance(result.matched_keywords, list)


from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.models import ModelScore


class TestModelLeaderboardProperties:
    """Property 2: Leaderboard Rankings Are Sorted Descending.

    **Validates: Requirements 2.5**

    For any valid leaderboard configuration and any task type,
    ModelLeaderboard.get_rankings(task_type) returns a list sorted
    by score in descending order.
    """

    @given(
        scores=st.lists(
            st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            min_size=0,
            max_size=20,
        ),
        task_type=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_"),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(max_examples=200)
    def test_rankings_always_sorted_descending(self, scores: list[float], task_type: str):
        """For any valid leaderboard config, get_rankings returns list sorted descending by score."""
        # Build a YAML string with the given scores
        models_yaml = "\n".join(
            f"      - name: model-{i}\n        score: {s}"
            for i, s in enumerate(scores)
        )
        yaml_str = f"""\
task_types:
  {task_type}:
    models:
{models_yaml}
"""
        lb = ModelLeaderboard.from_yaml(yaml_str, valid_models=None)
        rankings = lb.get_rankings(task_type)

        # Rankings must be sorted descending by score
        for i in range(len(rankings) - 1):
            assert rankings[i].score >= rankings[i + 1].score


from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ProviderModelMapping, TokenPricing, VirtualModelConfig, RoutingStrategy
from src.gateway.smart_routing import NoCandidateModelsError, SmartRoutingStrategy


# --- Shared helpers for property tests ---

def _build_registry_and_leaderboard(model_names, context_windows=None, costs=None):
    """Build a ModelRegistry and ModelLeaderboard from model names."""
    registry = ModelRegistry()
    for i, name in enumerate(model_names):
        pricing = None
        if costs and i < len(costs):
            pricing = TokenPricing(
                prompt_token_cost=costs[i],
                completion_token_cost=costs[i],
            )
        else:
            pricing = TokenPricing(
                prompt_token_cost=0.001 * (i + 1),
                completion_token_cost=0.002 * (i + 1),
            )

        max_ctx = None
        if context_windows and i < len(context_windows):
            max_ctx = context_windows[i]

        registry.models[name] = VirtualModelConfig(
            name=name,
            description=f"Model {name}",
            providers=[
                ProviderModelMapping(
                    provider=f"provider-{name}",
                    model_id=f"{name}-id",
                    weight=1.0,
                    fallback_order=0,
                    pricing=pricing,
                )
            ],
            routing_strategy=RoutingStrategy.ROUND_ROBIN,
            max_context_tokens=max_ctx,
        )

    # Build leaderboard YAML
    models_yaml = "\n".join(
        f"      - name: {name}\n        score: {90 - i * 5}"
        for i, name in enumerate(model_names)
    )
    yaml_str = f"""\
task_types:
  coding:
    models:
{models_yaml}
  reasoning:
    models:
{models_yaml}
  creative_writing:
    models:
{models_yaml}
  summarization:
    models:
{models_yaml}
  math:
    models:
{models_yaml}
  general:
    models:
{models_yaml}
"""
    lb = ModelLeaderboard.from_yaml(yaml_str, valid_models=set(model_names))
    return registry, lb


# Strategy for generating model name lists
model_names_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        min_size=3,
        max_size=15,
    ),
    min_size=2,
    max_size=6,
    unique=True,
)


class TestSmartSelectionRespectsAllowedModels:
    """Property 3: Smart Selection Respects Allowed Models.

    **Validates: Requirements 3.3, 3.7**

    For any non-empty allowed_models set, the model selected by
    SmartRoutingStrategy.select_model() is always a member of the
    allowed_models set (or an error is raised if no candidates remain).
    """

    @given(
        model_names=model_names_strategy,
        allowed_subset_indices=st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=5),
        prompt=st.sampled_from([
            "Implement a function and debug the code and refactor the class",
            "Calculate the integral of x^2 and solve the equation",
            "Write a story about a dragon with creative narrative",
            "Summarize this article and give me a brief overview",
            "Explain why the sky is blue and analyze the logic",
        ]),
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_selected_model_in_allowed_models(
        self, model_names, allowed_subset_indices, prompt
    ):
        """Selected model is always in allowed_models set."""
        # Ensure unique model names
        assume(len(set(model_names)) == len(model_names))

        # Build allowed_models as a subset
        allowed_indices = [i % len(model_names) for i in allowed_subset_indices]
        allowed_models = {model_names[i] for i in allowed_indices}
        assume(len(allowed_models) > 0)

        registry, lb = _build_registry_and_leaderboard(model_names)
        health_tracker = ProviderHealthTracker()
        cost_tracker = CostTracker(pricing_config={})
        feedback_tracker = FeedbackTracker()

        strategy = SmartRoutingStrategy(
            classifier=TaskClassifier(),
            leaderboard=lb,
            model_registry=registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=0.1,  # Low threshold to avoid fallback
            cost_quality_tradeoff=0.3,
            default_model=model_names[0],
        )

        try:
            decision = await strategy.select_model(prompt, allowed_models=allowed_models)
            assert decision.selected_model in allowed_models
        except NoCandidateModelsError:
            pass  # Valid outcome when no candidates remain


class TestSmartSelectionRespectsHealth:
    """Property 4: Smart Selection Respects Health.

    **Validates: Requirements 3.4**

    The model selected by SmartRoutingStrategy.select_model() always has
    at least one healthy provider according to the health tracker.
    """

    @given(
        model_names=model_names_strategy,
        unhealthy_indices=st.lists(st.integers(min_value=0, max_value=5), min_size=0, max_size=3),
        prompt=st.sampled_from([
            "Implement a function and debug the code and refactor the class",
            "Calculate the integral of x^2 and solve the equation",
            "Write a story about a dragon with creative narrative",
            "Summarize this article and give me a brief overview",
            "Explain why the sky is blue and analyze the logic",
        ]),
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_selected_model_has_healthy_provider(
        self, model_names, unhealthy_indices, prompt
    ):
        """Selected model always has at least one healthy provider."""
        assume(len(set(model_names)) == len(model_names))

        registry, lb = _build_registry_and_leaderboard(model_names)
        health_tracker = ProviderHealthTracker()
        cost_tracker = CostTracker(pricing_config={})
        feedback_tracker = FeedbackTracker()

        # Mark some providers as unhealthy
        unhealthy_providers = set()
        for idx in unhealthy_indices:
            i = idx % len(model_names)
            provider_name = f"provider-{model_names[i]}"
            health_tracker.mark_unhealthy(provider_name, 60)
            unhealthy_providers.add(provider_name)

        strategy = SmartRoutingStrategy(
            classifier=TaskClassifier(),
            leaderboard=lb,
            model_registry=registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=0.1,
            cost_quality_tradeoff=0.3,
            default_model=model_names[0],
        )

        try:
            decision = await strategy.select_model(prompt)
            # Verify the selected model has at least one healthy provider
            selected_config = registry.models[decision.selected_model]
            has_healthy = any(
                health_tracker.is_healthy(p.provider)
                for p in selected_config.providers
            )
            assert has_healthy or decision.used_fallback
        except NoCandidateModelsError:
            pass  # Valid outcome when all providers are unhealthy


class TestCompositeScoreFormula:
    """Property 5: Composite Score Formula Correctness.

    **Validates: Requirements 4.4, 4.5**

    For any cost_quality_tradeoff in [0.0, 1.0], any benchmark_score in [0, 100],
    and any cost_per_token >= 0, the composite score is in [0.0, 1.0].
    """

    @given(
        tradeoff=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        benchmark_score=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
        cost_per_token=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        max_benchmark=st.floats(min_value=0.1, max_value=100.0, allow_nan=False),
        max_cost=st.floats(min_value=0.001, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=500)
    def test_composite_score_in_valid_range(
        self, tradeoff, benchmark_score, cost_per_token, max_benchmark, max_cost
    ):
        """Composite score is always in [0.0, 1.0]."""
        assume(benchmark_score <= max_benchmark)
        assume(cost_per_token <= max_cost)

        # Create a minimal strategy just to test the formula
        registry = ModelRegistry()
        lb = ModelLeaderboard()
        health_tracker = ProviderHealthTracker()
        cost_tracker = CostTracker(pricing_config={})
        feedback_tracker = FeedbackTracker()

        strategy = SmartRoutingStrategy(
            classifier=TaskClassifier(),
            leaderboard=lb,
            model_registry=registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=0.3,
            cost_quality_tradeoff=tradeoff,
            default_model="default",
        )

        score = strategy._compute_composite_score(
            benchmark_score, cost_per_token, max_benchmark, max_cost
        )

        assert 0.0 <= score <= 1.0


class TestContextWindowFiltering:
    """Property 6: Context Window Filtering.

    **Validates: Requirements 5.2**

    For any prompt and set of candidate models, SmartRoutingStrategy.select_model()
    never selects a model whose max_context_tokens is less than the estimated
    token count of the prompt.
    """

    @given(
        model_names=model_names_strategy,
        prompt_length=st.integers(min_value=10, max_value=2000),
        context_multipliers=st.lists(
            st.floats(min_value=0.5, max_value=10.0, allow_nan=False, allow_infinity=False),
            min_size=2,
            max_size=6,
        ),
    )
    @settings(max_examples=100)
    @pytest.mark.asyncio
    async def test_selected_model_context_window_sufficient(
        self, model_names, prompt_length, context_multipliers
    ):
        """Selected model's context window >= estimated prompt tokens."""
        assume(len(set(model_names)) == len(model_names))
        assume(len(context_multipliers) >= len(model_names))

        prompt = "x" * prompt_length
        estimated_tokens = max(1, prompt_length // 4)

        # Set context windows relative to estimated tokens
        context_windows = [
            int(estimated_tokens * context_multipliers[i])
            for i in range(len(model_names))
        ]

        # Ensure at least one model has sufficient context window
        has_sufficient = any(cw >= estimated_tokens for cw in context_windows)
        assume(has_sufficient)

        registry, lb = _build_registry_and_leaderboard(
            model_names, context_windows=context_windows
        )
        health_tracker = ProviderHealthTracker()
        cost_tracker = CostTracker(pricing_config={})
        feedback_tracker = FeedbackTracker()

        strategy = SmartRoutingStrategy(
            classifier=TaskClassifier(),
            leaderboard=lb,
            model_registry=registry,
            health_tracker=health_tracker,
            cost_tracker=cost_tracker,
            feedback_tracker=feedback_tracker,
            confidence_threshold=0.0,  # Never fallback for this test
            cost_quality_tradeoff=0.3,
            default_model=model_names[0],
        )

        # Use a prompt that triggers a known task type
        coding_prompt = "implement function debug code " + prompt

        try:
            decision = await strategy.select_model(coding_prompt)
            if not decision.used_fallback:
                selected_config = registry.models[decision.selected_model]
                if selected_config.max_context_tokens is not None:
                    actual_estimated = strategy._estimate_token_count(coding_prompt)
                    assert selected_config.max_context_tokens >= actual_estimated
        except NoCandidateModelsError:
            pass  # Valid when no models have sufficient context
