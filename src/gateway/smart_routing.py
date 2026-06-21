"""Smart routing strategy — selects models based on prompt classification."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from src.gateway.cost_tracker import CostTracker
from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import (
    FeedbackRecord,
    ProviderModelMapping,
    SmartRoutingDecision,
)
from src.gateway.routing import NoHealthyProviderError, RoutingStrategyBase
from src.gateway.task_classifier import TaskClassifier

logger = logging.getLogger(__name__)


class NoCandidateModelsError(Exception):
    """Raised when no candidate models remain after filtering."""


class SmartRoutingStrategy(RoutingStrategyBase):
    """Routing strategy that selects models based on prompt classification.

    When used as a per-model strategy (via select()), it acts as a simple
    health-aware provider selector. The full smart routing pipeline is
    accessed via select_model().
    """

    def __init__(
        self,
        classifier: TaskClassifier,
        leaderboard: ModelLeaderboard,
        model_registry: ModelRegistry,
        health_tracker: ProviderHealthTracker,
        cost_tracker: CostTracker,
        feedback_tracker: FeedbackTracker,
        confidence_threshold: float = 0.3,
        cost_quality_tradeoff: float = 0.3,
        default_model: str = "claude-sonnet",
    ) -> None:
        self.classifier = classifier
        self.leaderboard = leaderboard
        self.model_registry = model_registry
        self.health_tracker = health_tracker
        self.cost_tracker = cost_tracker
        self.feedback_tracker = feedback_tracker
        self.confidence_threshold = confidence_threshold
        self.cost_quality_tradeoff = cost_quality_tradeoff
        self.default_model = default_model

    def select(
        self,
        providers: list[ProviderModelMapping],
        health_tracker: ProviderHealthTracker,
    ) -> ProviderModelMapping:
        """RoutingStrategyBase interface — selects among providers for an already-chosen model.

        Picks the first healthy provider (round-robin among healthy).
        """
        healthy = self._healthy_providers(providers, health_tracker)
        if not healthy:
            raise NoHealthyProviderError("No healthy providers available")
        return healthy[0]

    async def select_model(
        self,
        prompt: str,
        allowed_models: set[str] | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> SmartRoutingDecision:
        """Full smart routing: classify prompt, score models, select best.

        Steps:
        1. Classify prompt
        2. Check confidence threshold
        3. Get leaderboard rankings for task type
        4. Filter by allowed_models
        5. Filter by health (at least one healthy provider)
        6. Filter by context window (estimate prompt tokens)
        7. Filter by budget
        8. Apply cost-quality tradeoff scoring
        9. Select top candidate
        10. Record feedback
        11. Return decision
        """
        # Step 1: Classify prompt
        classification = self.classifier.classify(prompt)
        task_type = classification.task_type
        confidence = classification.confidence

        candidates_considered: list[dict] = []

        # Step 2: Check confidence threshold
        if confidence < self.confidence_threshold:
            # Fallback to default model
            decision = SmartRoutingDecision(
                task_type=task_type,
                confidence=confidence,
                selected_model=self.default_model,
                benchmark_score=0.0,
                candidates_considered=candidates_considered,
                used_fallback=True,
                cost_quality_tradeoff=self.cost_quality_tradeoff,
            )
            await self._record_feedback(decision)
            return decision

        # Step 3: Get leaderboard rankings
        rankings = self.leaderboard.get_rankings(task_type)
        if not rankings:
            # No rankings for this task type — fallback
            decision = SmartRoutingDecision(
                task_type=task_type,
                confidence=confidence,
                selected_model=self.default_model,
                benchmark_score=0.0,
                candidates_considered=candidates_considered,
                used_fallback=True,
                cost_quality_tradeoff=self.cost_quality_tradeoff,
            )
            await self._record_feedback(decision)
            return decision

        # Build candidate list from rankings
        candidates = []
        for model_score in rankings:
            model_name = model_score.model_name
            entry = {"model": model_name, "benchmark_score": model_score.score}

            # Step 4: Filter by allowed_models
            if allowed_models is not None and model_name not in allowed_models:
                entry["filtered_reason"] = "not_in_allowed_models"
                candidates_considered.append(entry)
                continue

            # Check model exists in registry
            if model_name not in self.model_registry.models:
                entry["filtered_reason"] = "not_in_registry"
                candidates_considered.append(entry)
                continue

            # Step 5: Filter by health
            model_config = self.model_registry.models[model_name]
            providers = model_config.providers
            has_healthy = any(
                self.health_tracker.is_healthy(p.provider) for p in providers
            )
            if not has_healthy:
                entry["filtered_reason"] = "all_providers_unhealthy"
                candidates_considered.append(entry)
                continue

            # Step 6: Filter by context window
            estimated_tokens = self._estimate_token_count(prompt)
            max_context = model_config.max_context_tokens
            if max_context is not None and max_context < estimated_tokens:
                entry["filtered_reason"] = "context_window_too_small"
                candidates_considered.append(entry)
                continue

            # Step 7: Filter by budget
            if project_id is not None:
                budget_status = await self.cost_tracker.check_budget(project_id)
                if budget_status.is_over_budget:
                    entry["filtered_reason"] = "over_budget"
                    candidates_considered.append(entry)
                    continue

            if user_id is not None:
                user_budget = await self.cost_tracker.check_user_budget(user_id)
                if user_budget.is_over_budget:
                    entry["filtered_reason"] = "over_budget"
                    candidates_considered.append(entry)
                    continue

            # Model passed all filters
            entry["passed"] = True
            candidates.append(entry)
            candidates_considered.append(entry)

        # Step 8: Score candidates with composite score
        if not candidates:
            raise NoCandidateModelsError(
                "No candidate models remain after filtering"
            )

        # Compute cost per token for each candidate
        for candidate in candidates:
            model_name = candidate["model"]
            model_config = self.model_registry.models[model_name]
            cost = self._get_model_cost(model_config)
            candidate["cost_per_token"] = cost

        max_benchmark = max(c["benchmark_score"] for c in candidates)
        max_cost = max(c["cost_per_token"] for c in candidates) if candidates else 1.0
        # Avoid division by zero if all costs are 0
        if max_cost == 0:
            max_cost = 1.0

        for candidate in candidates:
            candidate["composite_score"] = self._compute_composite_score(
                candidate["benchmark_score"],
                candidate["cost_per_token"],
                max_benchmark,
                max_cost,
            )

        # Step 9: Select top candidate
        best = max(candidates, key=lambda c: c["composite_score"])
        selected_model = best["model"]
        benchmark_score = best["benchmark_score"]

        decision = SmartRoutingDecision(
            task_type=task_type,
            confidence=confidence,
            selected_model=selected_model,
            benchmark_score=benchmark_score,
            candidates_considered=candidates_considered,
            used_fallback=False,
            cost_quality_tradeoff=self.cost_quality_tradeoff,
        )

        # Step 10: Record feedback
        await self._record_feedback(decision)

        return decision

    def _estimate_token_count(self, prompt: str) -> int:
        """Rough token estimation: ~4 chars per token for English text."""
        return max(1, len(prompt) // 4)

    def _compute_composite_score(
        self,
        benchmark_score: float,
        cost_per_token: float,
        max_benchmark: float,
        max_cost: float,
    ) -> float:
        """Compute composite score using cost-quality tradeoff formula.

        composite = (1 - tradeoff) * normalized_benchmark + tradeoff * (1 - normalized_cost)
        """
        norm_benchmark = benchmark_score / max_benchmark if max_benchmark > 0 else 0.0
        norm_cost = cost_per_token / max_cost if max_cost > 0 else 0.0
        return (1 - self.cost_quality_tradeoff) * norm_benchmark + self.cost_quality_tradeoff * (1 - norm_cost)

    def _get_model_cost(self, model_config) -> float:
        """Get average cost per token for a model across its providers."""
        costs = []
        for provider in model_config.providers:
            if provider.pricing is not None:
                avg = (provider.pricing.prompt_token_cost + provider.pricing.completion_token_cost) / 2
                costs.append(avg)
        if costs:
            return sum(costs) / len(costs)
        return 0.0

    async def _record_feedback(self, decision: SmartRoutingDecision) -> None:
        """Record a feedback entry for the routing decision."""
        feedback = FeedbackRecord(
            request_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            task_type=decision.task_type,
            confidence=decision.confidence,
            selected_model=decision.selected_model,
            benchmark_score=decision.benchmark_score,
        )
        await self.feedback_tracker.record_async(feedback)
