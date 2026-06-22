"""Quota enforcer — bridges policy hierarchy limits into the request pipeline.

Takes a ResolvedPolicy (from the hierarchy walk) and enforces:
- rate_limit_rpm: dynamic per-project RPM from org/BU/project/env chain
- budget_limit: blocks requests when projected spend exceeds hierarchy budget
- max_tokens_per_request: caps the max_tokens parameter
- allowed_models: rejects models not in the intersection
- allowed_providers: rejects providers not in the intersection
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gateway.models import ResolvedPolicy


@dataclass
class QuotaDecision:
    """Result of a quota enforcement check."""

    allowed: bool = True
    reason: str = ""
    limit_type: str = ""
    limit_value: float | int | None = None
    current_value: float | int | None = None


BUDGET_ALERT_THRESHOLDS = [0.8, 0.9, 1.0]


class QuotaEnforcer:
    """Enforces resolved policy limits on incoming requests.

    Maintains its own sliding window counters keyed by project_id,
    separate from the global rate limiter. This allows hierarchy-derived
    limits to override static defaults.
    """

    def __init__(self) -> None:
        self._request_windows: dict[str, list[datetime]] = {}
        self._spend_tracker: dict[str, float] = {}
        self._alerted_thresholds: dict[str, set[float]] = {}
        self._alert_callbacks: list = []

    def on_budget_alert(self, callback) -> None:
        """Register a callback for budget threshold alerts.

        Callback signature: callback(project_id, threshold_pct, current_spend, budget_limit)
        """
        self._alert_callbacks.append(callback)

    def check_rate_limit(
        self, project_id: str, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if request is within the policy's rate_limit_rpm."""
        if policy.rate_limit_rpm is None:
            return QuotaDecision(allowed=True)

        now = datetime.utcnow()
        window = timedelta(seconds=60)
        cutoff = now - window

        timestamps = self._request_windows.get(project_id, [])
        timestamps = [ts for ts in timestamps if ts > cutoff]

        if len(timestamps) >= policy.rate_limit_rpm:
            return QuotaDecision(
                allowed=False,
                reason=f"Policy rate limit exceeded: {policy.rate_limit_rpm} RPM",
                limit_type="rate_limit_rpm",
                limit_value=policy.rate_limit_rpm,
                current_value=len(timestamps),
            )

        timestamps.append(now)
        self._request_windows[project_id] = timestamps
        return QuotaDecision(allowed=True)

    def check_budget(
        self, project_id: str, estimated_cost: float, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if request would exceed the policy's budget_limit."""
        if policy.budget_limit is None:
            return QuotaDecision(allowed=True)

        current_spend = self._spend_tracker.get(project_id, 0.0)
        projected = current_spend + estimated_cost

        if projected > policy.budget_limit:
            return QuotaDecision(
                allowed=False,
                reason=f"Budget limit exceeded: ${projected:.4f} > ${policy.budget_limit:.2f}",
                limit_type="budget_limit",
                limit_value=policy.budget_limit,
                current_value=current_spend,
            )

        return QuotaDecision(allowed=True)

    def check_max_tokens(
        self, requested_max_tokens: int | None, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if requested max_tokens exceeds policy limit."""
        if policy.max_tokens_per_request is None:
            return QuotaDecision(allowed=True)

        if requested_max_tokens is None:
            return QuotaDecision(allowed=True)

        if requested_max_tokens > policy.max_tokens_per_request:
            return QuotaDecision(
                allowed=False,
                reason=f"max_tokens {requested_max_tokens} exceeds policy limit {policy.max_tokens_per_request}",
                limit_type="max_tokens_per_request",
                limit_value=policy.max_tokens_per_request,
                current_value=requested_max_tokens,
            )

        return QuotaDecision(allowed=True)

    def check_model_allowed(
        self, model: str, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if the requested model is allowed by the policy hierarchy."""
        if policy.allowed_models is None:
            return QuotaDecision(allowed=True)

        if model not in policy.allowed_models:
            return QuotaDecision(
                allowed=False,
                reason=f"Model '{model}' not in allowed models: {policy.allowed_models}",
                limit_type="allowed_models",
            )

        return QuotaDecision(allowed=True)

    def check_provider_allowed(
        self, provider: str, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if the requested provider is allowed by the policy hierarchy."""
        if policy.allowed_providers is None:
            return QuotaDecision(allowed=True)

        if provider not in policy.allowed_providers:
            return QuotaDecision(
                allowed=False,
                reason=f"Provider '{provider}' not in allowed providers: {policy.allowed_providers}",
                limit_type="allowed_providers",
            )

        return QuotaDecision(allowed=True)

    def enforce_all(
        self,
        project_id: str,
        model: str,
        provider: str | None,
        max_tokens: int | None,
        estimated_cost: float,
        policy: ResolvedPolicy,
    ) -> QuotaDecision:
        """Run all quota checks. Returns first failure or allowed."""
        checks = [
            self.check_rate_limit(project_id, policy),
            self.check_budget(project_id, estimated_cost, policy),
            self.check_max_tokens(max_tokens, policy),
            self.check_model_allowed(model, policy),
        ]
        if provider:
            checks.append(self.check_provider_allowed(provider, policy))

        for decision in checks:
            if not decision.allowed:
                return decision

        return QuotaDecision(allowed=True)

    def record_spend(self, project_id: str, cost: float, budget_limit: float | None = None) -> None:
        """Record spend for budget tracking. Fires alerts at threshold crossings."""
        prev = self._spend_tracker.get(project_id, 0.0)
        new_spend = prev + cost
        self._spend_tracker[project_id] = new_spend

        if budget_limit and budget_limit > 0 and self._alert_callbacks:
            alerted = self._alerted_thresholds.get(project_id, set())
            for threshold in BUDGET_ALERT_THRESHOLDS:
                if threshold in alerted:
                    continue
                trigger_at = budget_limit * threshold
                if prev < trigger_at <= new_spend:
                    alerted.add(threshold)
                    for cb in self._alert_callbacks:
                        cb(project_id, threshold, new_spend, budget_limit)
            self._alerted_thresholds[project_id] = alerted

    def get_spend(self, project_id: str) -> float:
        """Get current tracked spend for a project."""
        return self._spend_tracker.get(project_id, 0.0)

    def reset_spend(self, project_id: str) -> None:
        """Reset spend tracking for a project (e.g., at billing cycle reset)."""
        self._spend_tracker.pop(project_id, None)
        self._alerted_thresholds.pop(project_id, None)

    def cap_max_tokens(self, requested: int | None, policy: ResolvedPolicy) -> int | None:
        """Return the effective max_tokens — capped by policy if needed."""
        if policy.max_tokens_per_request is None:
            return requested
        if requested is None:
            return policy.max_tokens_per_request
        return min(requested, policy.max_tokens_per_request)
