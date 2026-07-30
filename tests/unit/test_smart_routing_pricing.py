"""Cost resolution and the unknown-cost policy in smart routing.

``_get_model_cost`` read ``ProviderModelMapping.pricing``, which is populated
only from an inline ``pricing:`` block in models.yaml — and the shipped
models.yaml has none, so every model costed 0.0 and the cost half of
``cost_quality_tradeoff`` became a constant added to every candidate. Ranking
collapsed to pure benchmark order.

These tests cover both halves of the fix: resolving pricing from the shared
table CostTracker bills from, and treating a missing price as *unknown* rather
than as free.
"""

import pytest

from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import ProviderModelMapping, TokenPricing
from src.gateway.smart_routing import SmartRoutingStrategy
from src.gateway.task_classifier import TaskClassifier

# cheap-model has the LOWEST benchmark and NO pricing anywhere. It is the trap:
# scored as free it wins every task type despite being the worst model.
LEADERBOARD_YAML = """\
task_types:
  coding:
    models:
      - name: dear-model
        score: 95
      - name: mid-model
        score: 90
      - name: cheap-model
        score: 70
smart_routing:
  confidence_threshold: 0.3
  cost_quality_tradeoff: 0.3
  default_model: mid-model
"""

# No inline pricing on any entry — matching the shipped config/models.yaml,
# where 0 of 48 provider entries carry a pricing block.
MODELS_YAML = """\
models:
  - name: dear-model
    description: Expensive
    routing_strategy: round-robin
    providers:
      - provider: anthropic
        model_id: dear-1
        fallback_order: 0
  - name: mid-model
    description: Mid-priced
    routing_strategy: round-robin
    providers:
      - provider: openai
        model_id: mid-1
        fallback_order: 0
  - name: cheap-model
    description: Unpriced
    routing_strategy: round-robin
    providers:
      - provider: bedrock
        model_id: unpriced-1
        fallback_order: 0
"""

# Keyed by provider then *provider-side* model id, exactly as
# config/pricing.yaml is and as CostTracker looks it up.
PRICING = {
    "anthropic": {
        "dear-1": TokenPricing(prompt_token_cost=0.015, completion_token_cost=0.075)
    },
    "openai": {
        "mid-1": TokenPricing(prompt_token_cost=0.0025, completion_token_cost=0.01)
    },
    # "unpriced-1" is deliberately absent.
}

MODEL_NAMES = {"dear-model", "mid-model", "cheap-model"}


def build_strategy(pricing_config=None, cost_tracker=None, tradeoff=0.3):
    return SmartRoutingStrategy(
        classifier=TaskClassifier(),
        leaderboard=ModelLeaderboard.from_yaml(LEADERBOARD_YAML, MODEL_NAMES),
        model_registry=ModelRegistry.from_yaml(MODELS_YAML),
        health_tracker=ProviderHealthTracker(),
        cost_tracker=cost_tracker or CostTracker(pricing_config={}),
        feedback_tracker=FeedbackTracker(),
        cost_quality_tradeoff=tradeoff,
        default_model="mid-model",
        pricing_config=pricing_config,
    )


class TestPricingResolution:
    """Costs come from the shared table when models.yaml has no inline block."""

    def test_cost_resolved_from_pricing_table(self):
        strategy = build_strategy(PRICING)
        registry = strategy.model_registry
        # (0.015 + 0.075) / 2
        assert strategy._get_model_cost(registry.models["dear-model"]) == pytest.approx(0.045)
        # (0.0025 + 0.01) / 2
        assert strategy._get_model_cost(registry.models["mid-model"]) == pytest.approx(0.00625)

    def test_without_pricing_every_cost_is_unknown(self):
        # The regression itself: no pricing source, so nothing is priced. This is
        # the state the shipped config was in.
        strategy = build_strategy(None)
        registry = strategy.model_registry
        for name in MODEL_NAMES:
            assert strategy._get_model_cost(registry.models[name]) is None, name

    def test_inline_pricing_wins_over_the_table(self):
        # An explicit block in models.yaml is the more specific declaration.
        strategy = build_strategy(PRICING)
        mapping = ProviderModelMapping(
            provider="anthropic",
            model_id="dear-1",
            pricing=TokenPricing(prompt_token_cost=1.0, completion_token_cost=3.0),
        )
        resolved = strategy._resolve_pricing(mapping)
        assert resolved.prompt_token_cost == 1.0

    def test_pricing_defaults_to_the_cost_tracker_table(self):
        # A caller that already wired pricing into CostTracker gets cost-aware
        # scoring without passing it twice.
        strategy = build_strategy(None, cost_tracker=CostTracker(pricing_config=PRICING))
        assert strategy.pricing_config == PRICING
        cost = strategy._get_model_cost(strategy.model_registry.models["dear-model"])
        assert cost == pytest.approx(0.045)

    def test_partially_priced_model_averages_only_priced_providers(self):
        strategy = build_strategy(PRICING)
        two_providers = ModelRegistry.from_yaml(
            """\
models:
  - name: split-model
    description: One priced provider, one not
    routing_strategy: round-robin
    providers:
      - provider: anthropic
        model_id: dear-1
        fallback_order: 0
      - provider: bedrock
        model_id: unpriced-1
        fallback_order: 1
"""
        )
        cost = strategy._get_model_cost(two_providers.models["split-model"])
        assert cost == pytest.approx(0.045)


class TestUnknownCostIsNotFree:
    """A missing price must not make a model the cheapest candidate.

    This is the property that keeps the fix from being a regression: 13 of the
    48 provider entries in the shipped config are unpriced — their providers
    publish no rate for the pinned id — so "missing means 0.0" would let the
    worst model win for being unmeasured. The count falls as prices are filled
    in but is unlikely to reach zero, since some of those ids no longer exist.
    """

    @pytest.mark.asyncio
    async def test_unpriced_model_does_not_win(self):
        strategy = build_strategy(PRICING)
        decision = await strategy.select_model("write a python function to sort a list")
        assert decision.used_fallback is False
        # cheap-model is unpriced AND has the worst benchmark (70). Scored as
        # free it would win outright.
        assert decision.selected_model != "cheap-model"

    @pytest.mark.asyncio
    async def test_unpriced_candidate_is_flagged(self):
        strategy = build_strategy(PRICING)
        decision = await strategy.select_model("write a python function to sort a list")
        by_name = {c["model"]: c for c in decision.candidates_considered if c.get("passed")}
        assert by_name["cheap-model"]["cost_per_token"] is None
        assert by_name["cheap-model"]["cost_estimated"] is True
        # A priced candidate is not flagged.
        assert "cost_estimated" not in by_name["dear-model"]

    @pytest.mark.asyncio
    async def test_unpriced_scores_at_the_mean_of_known_costs(self):
        strategy = build_strategy(PRICING)
        decision = await strategy.select_model("write a python function to sort a list")
        by_name = {c["model"]: c for c in decision.candidates_considered if c.get("passed")}
        mean_cost = (0.045 + 0.00625) / 2
        expected = strategy._compute_composite_score(70, mean_cost, 95, 0.045)
        assert by_name["cheap-model"]["composite_score"] == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_max_cost_excludes_unknown_candidates(self):
        # Normalizing against an unpriced candidate treated as 0.0 would deflate
        # every other model's normalized cost.
        strategy = build_strategy(PRICING)
        decision = await strategy.select_model("write a python function to sort a list")
        by_name = {c["model"]: c for c in decision.candidates_considered if c.get("passed")}
        # dear-model is the most expensive priced candidate, so norm_cost == 1
        # and its cost term contributes nothing.
        assert by_name["dear-model"]["composite_score"] == pytest.approx(0.7 * 1.0)


class TestCostAffectsSelection:
    """The tradeoff weight must actually change the outcome."""

    @pytest.mark.asyncio
    async def test_cost_can_outrank_benchmark(self):
        # dear-model leads on benchmark (95 vs 90) but costs 7.2x more, so at a
        # 0.3 tradeoff mid-model wins. Before the fix all costs were 0.0 and the
        # top-benchmark model always won.
        strategy = build_strategy(PRICING, tradeoff=0.3)
        decision = await strategy.select_model("write a python function to sort a list")
        assert decision.selected_model == "mid-model"

    @pytest.mark.asyncio
    async def test_zero_tradeoff_is_pure_benchmark(self):
        strategy = build_strategy(PRICING, tradeoff=0.0)
        decision = await strategy.select_model("write a python function to sort a list")
        assert decision.selected_model == "dear-model"

    @pytest.mark.asyncio
    async def test_no_pricing_falls_back_to_benchmark_order(self):
        # With nothing priced the cost term is uniform, so this reproduces the
        # old behaviour — the fix must degrade to it rather than misrank.
        strategy = build_strategy(None, tradeoff=0.3)
        decision = await strategy.select_model("write a python function to sort a list")
        assert decision.selected_model == "dear-model"

    @pytest.mark.asyncio
    async def test_all_candidates_still_scored_when_nothing_priced(self):
        strategy = build_strategy(None, tradeoff=0.3)
        decision = await strategy.select_model("write a python function to sort a list")
        passed = [c for c in decision.candidates_considered if c.get("passed")]
        assert len(passed) == 3
        # No division-by-zero and no NaN when max_cost would be 0.
        for c in passed:
            assert 0.0 <= c["composite_score"] <= 1.0
