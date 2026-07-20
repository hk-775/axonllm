"""Cost tracking, budget management, and usage aggregation for the LLM-Router."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import tiktoken

from src.gateway.config import DEFAULT_CONFIG
from src.gateway.models import (
    BudgetStatus,
    TokenPricing,
    UsageBreakdown,
    UsageFilters,
    UsageRecord,
    UsageReport,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


class CostTracker:
    """Records usage, calculates costs, checks budgets, and aggregates usage data.

    Args:
        pricing_config: Nested dict mapping provider -> model -> TokenPricing.
        budgets: Dict mapping project_id -> {"budget_limit": float, "alert_threshold": float}.
    """

    MAX_RECORDS = 100_000

    def __init__(
        self,
        pricing_config: dict[str, dict[str, TokenPricing]],
        budgets: dict[str, dict] | None = None,
        persistence: DynamoPersistence | None = None,
    ):
        self.pricing_config = pricing_config
        self._records: list[UsageRecord] = []
        self._budgets: dict[str, dict] = budgets or {}
        self._user_budgets: dict[str, dict] = {}
        self._persistence = persistence

    # ------------------------------------------------------------------
    # Budget / project registration
    # ------------------------------------------------------------------

    def register_project(
        self,
        project_id: str,
        budget_limit: float | None = None,
        alert_threshold: float | None = None,
    ) -> None:
        """Register a project with optional budget limit and alert threshold."""
        self._budgets[project_id] = {
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
        }

    def register_user(
        self,
        user_id: str,
        budget_limit: float | None = None,
        alert_threshold: float | None = None,
    ) -> None:
        """Register a user with optional budget limit and alert threshold."""
        self._user_budgets[user_id] = {
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
        }

    def get_user_budget(self, user_id: str) -> dict:
        """Return budget info for a user, or empty defaults."""
        return self._user_budgets.get(user_id, {"budget_limit": None, "alert_threshold": None})

    async def check_user_budget(self, user_id: str) -> BudgetStatus:
        """Check whether a user is within their budget limits."""
        budget_info = self._user_budgets.get(user_id, {})
        budget_limit: float | None = budget_info.get("budget_limit")
        alert_threshold: float | None = budget_info.get("alert_threshold")

        current_spend = sum(
            r.cost for r in self._records if r.user_id == user_id
        )

        is_over_budget = (
            budget_limit is not None and current_spend >= budget_limit
        )
        is_alert_triggered = (
            alert_threshold is not None and current_spend >= alert_threshold
        )

        return BudgetStatus(
            project_id=user_id,
            current_spend=current_spend,
            budget_limit=budget_limit,
            alert_threshold=alert_threshold,
            is_over_budget=is_over_budget,
            is_alert_triggered=is_alert_triggered,
        )


    # ------------------------------------------------------------------
    # Cost calculation
    # ------------------------------------------------------------------

    def calculate_cost(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        image_tokens: int = 0,
        reasoning_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float:
        """Calculate cost using per-token pricing config.

        Formula:
          ((prompt_tokens - cached_tokens - cache_creation_tokens) / 1000 * prompt_token_cost)
        + (completion_tokens / 1000 * completion_token_cost)
        + (cached_tokens / 1000 * cached_rate)              [cached_rate = cached_token_cost or prompt_token_cost]
        + (cache_creation_tokens / 1000 * creation_rate)     [creation_rate = cache_creation_token_cost or prompt_token_cost]
        + (image_tokens / 1000 * image_token_cost)           [if configured]
        + (reasoning_tokens / 1000 * reasoning_token_cost)   [if configured]
        + per_request_cost                                   [flat fee per call]

        Returns 0.0 if no pricing is configured for the provider/model.
        """
        provider_pricing = self.pricing_config.get(provider, {})
        pricing: TokenPricing | None = provider_pricing.get(model)
        if pricing is None:
            return 0.0

        # Determine effective rates with fallback to prompt_token_cost
        cached_rate = pricing.cached_token_cost if pricing.cached_token_cost is not None else pricing.prompt_token_cost
        creation_rate = pricing.cache_creation_token_cost if pricing.cache_creation_token_cost is not None else pricing.prompt_token_cost

        # Subtract cached + creation from prompt to avoid double-billing
        billable_prompt = max(0, prompt_tokens - cached_tokens - cache_creation_tokens)

        cost = (billable_prompt / 1000 * pricing.prompt_token_cost) + (
            completion_tokens / 1000 * pricing.completion_token_cost
        )

        if cached_tokens > 0:
            cost += cached_tokens / 1000 * cached_rate

        if cache_creation_tokens > 0:
            cost += cache_creation_tokens / 1000 * creation_rate

        if image_tokens > 0 and pricing.image_token_cost is not None:
            cost += image_tokens / 1000 * pricing.image_token_cost

        if reasoning_tokens > 0 and pricing.reasoning_token_cost is not None:
            cost += reasoning_tokens / 1000 * pricing.reasoning_token_cost

        cost += pricing.per_request_cost

        return cost

    # ------------------------------------------------------------------
    # Usage recording
    # ------------------------------------------------------------------

    async def record_usage(self, usage: UsageRecord) -> None:
        """Persist a usage record to the in-memory store."""
        self._records.append(usage)
        if len(self._records) > self.MAX_RECORDS:
            self._records = self._records[-(self.MAX_RECORDS // 2):]

        # Fire-and-forget DynamoDB write
        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_usage_record(usage)
            except Exception:
                logger.warning(
                    "Failed to persist usage record %s to DynamoDB",
                    usage.request_id,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    async def estimate_tokens(self, text: str, model: str) -> int:
        """Estimate token count using tiktoken when the provider doesn't return usage.

        Falls back to cl100k_base encoding if the model is not recognised by tiktoken.
        """
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding(DEFAULT_CONFIG.token_estimation.fallback_encoding)
        return len(encoding.encode(text))

    # ------------------------------------------------------------------
    # Budget checking
    # ------------------------------------------------------------------

    async def check_budget(self, project_id: str) -> BudgetStatus:
        """Check whether a project is within its budget limits.

        Returns a BudgetStatus with alert/exceeded flags set appropriately.
        If the project has no registered budget, limits are None and flags are False.
        """
        budget_info = self._budgets.get(project_id, {})
        budget_limit: float | None = budget_info.get("budget_limit")
        alert_threshold: float | None = budget_info.get("alert_threshold")

        current_spend = sum(
            r.cost for r in self._records if r.project_id == project_id
        )

        is_over_budget = (
            budget_limit is not None and current_spend >= budget_limit
        )
        is_alert_triggered = (
            alert_threshold is not None and current_spend >= alert_threshold
        )

        return BudgetStatus(
            project_id=project_id,
            current_spend=current_spend,
            budget_limit=budget_limit,
            alert_threshold=alert_threshold,
            is_over_budget=is_over_budget,
            is_alert_triggered=is_alert_triggered,
        )

    # ------------------------------------------------------------------
    # Aggregated usage
    # ------------------------------------------------------------------

    async def get_aggregated_usage(self, filters: UsageFilters) -> UsageReport:
        """Query aggregated usage data with optional filters.

        Filters: time range, provider, model, project_id, user_id.
        Returns a UsageReport with totals and per-provider breakdown.
        """
        filtered = self._apply_filters(filters)

        total_requests = len(filtered)
        total_tokens = sum(r.total_tokens for r in filtered)
        total_cost = sum(r.cost for r in filtered)

        breakdown = self._build_breakdown(filtered)

        return UsageReport(
            total_requests=total_requests,
            total_tokens=total_tokens,
            total_cost=total_cost,
            breakdown=breakdown,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_aware(ts: datetime) -> datetime:
        """Coerce a timestamp to tz-aware UTC so time-window filters never mix
        naive and aware datetimes (which raises TypeError). Records may arrive
        naive from older callers or persisted rows; filter bounds may be either.
        """
        if ts is None:
            return ts
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    def _apply_filters(self, filters: UsageFilters) -> list[UsageRecord]:
        """Return records matching all non-None filter criteria."""
        result = self._records
        if filters.start_time is not None:
            start = self._as_aware(filters.start_time)
            result = [r for r in result if self._as_aware(r.timestamp) >= start]
        if filters.end_time is not None:
            end = self._as_aware(filters.end_time)
            result = [r for r in result if self._as_aware(r.timestamp) <= end]
        if filters.provider is not None:
            result = [r for r in result if r.provider == filters.provider]
        if filters.model is not None:
            result = [r for r in result if r.model == filters.model]
        if filters.project_id is not None:
            result = [r for r in result if r.project_id == filters.project_id]
        if filters.user_id is not None:
            result = [r for r in result if r.user_id == filters.user_id]
        return result

    def _build_breakdown(self, records: list[UsageRecord]) -> list[UsageBreakdown]:
        """Build usage breakdowns grouped by provider, model, project, and user."""
        breakdowns: list[UsageBreakdown] = []

        for group_by, key_fn in [
            ("provider", lambda r: r.provider),
            ("model", lambda r: r.model),
            ("project", lambda r: r.project_id),
            ("user", lambda r: r.user_id),
        ]:
            groups: dict[str, list[UsageRecord]] = defaultdict(list)
            for r in records:
                groups[key_fn(r)].append(r)
            for group_key, group_records in groups.items():
                breakdowns.append(
                    UsageBreakdown(
                        group_key=group_key,
                        group_by=group_by,
                        requests=len(group_records),
                        tokens=sum(r.total_tokens for r in group_records),
                        cost=sum(r.cost for r in group_records),
                    )
                )

        return breakdowns
