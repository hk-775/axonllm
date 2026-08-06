"""Quota enforcer — bridges policy hierarchy limits into the request pipeline.

Takes a ResolvedPolicy (from the hierarchy walk) and enforces:
- rate_limit_rpm: dynamic per-project RPM from org/BU/project/env chain
- budget_limit: blocks requests when projected spend exceeds hierarchy budget
- max_tokens_per_request: caps the max_tokens parameter
- allowed_models: rejects models not in the intersection
- allowed_providers: rejects providers not in the intersection
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING

from src.gateway.striped_lock import StripedLock

if TYPE_CHECKING:
    from src.gateway.models import ResolvedPolicy
    from src.gateway.persistence import DynamoPersistence


@dataclass
class QuotaDecision:
    """Result of a quota enforcement check."""

    allowed: bool = True
    reason: str = ""
    limit_type: str = ""
    limit_value: float | int | None = None
    current_value: float | int | None = None


BUDGET_ALERT_THRESHOLDS = [0.8, 0.9, 1.0]


# Scope name for this enforcer's shared spend counters. Deliberately distinct
# from CostTracker's "project" scope: both record the same cost on the same
# request, so pointing them at one key would double every charge. They stay two
# counters that happen to agree, which is what they already were per-process.
SPEND_SCOPE = "quota"

# How stale the spend figure `check_budget` reads may be. `record_spend` already
# adopts the fleet total for free from its own write, so this only matters for
# spend an instance did not serve itself: without a refresh, an instance that has
# not billed a project since starting reads its own $0 and admits a request
# against an exhausted budget — once per instance, and again after every deploy.
#
# The window bounds the overshoot instead of eliminating it. At worst a project
# overspends by whatever the fleet can bill in two seconds, rather than by a
# whole budget per instance. Eliminating it entirely would mean a consistent read
# on every request, which puts DynamoDB on the hot path of every call the gateway
# proxies — the cost the cache elsewhere in this codebase exists to avoid.
SPEND_REFRESH_SECONDS = 2.0


class QuotaEnforcer:
    """Enforces resolved policy limits on incoming requests.

    Maintains its own sliding window counters keyed by project_id,
    separate from the global rate limiter. This allows hierarchy-derived
    limits to override static defaults.

    Spend is tracked in DynamoDB when persistence is supplied, because this is
    the class that actually blocks requests: ``check_budget`` is what returns
    ``allowed=False``, and reading a per-process counter there turned a $100
    budget into $100 *per instance* — roughly $200 at the shipped
    ``desired_count=2`` and up to $1000 fully scaled out. Rate-limit windows are
    left per-process; a shared RPM window would need a read on every request,
    and the fleet limiter (``rate_limiter``) already covers that dimension.
    """

    def __init__(self, persistence: DynamoPersistence | None = None) -> None:
        self._request_windows: dict[str, list[datetime]] = {}
        self._spend_tracker: dict[str, float] = {}
        self._alerted_thresholds: dict[str, set[float]] = {}
        self._alert_callbacks: list = []
        # Per-project locks: rate-limit and spend state are keyed by project_id,
        # so a single global lock needlessly serialized unrelated projects.
        self._locks = StripedLock()
        self._persistence = persistence
        # When each project's shared counter was last read. Only consulted for
        # projects this instance has not billed recently; a write refreshes the
        # figure at no cost, so it stamps this too.
        self._spend_read_at: dict[str, float] = {}

    @property
    def _shares_spend(self) -> bool:
        return self._persistence is not None and self._persistence.enabled

    def on_budget_alert(self, callback) -> None:
        """Register a callback for budget threshold alerts.

        Callback signature: callback(project_id, threshold_pct, current_spend, budget_limit)
        """
        self._alert_callbacks.append(callback)

    async def check_rate_limit(
        self, project_id: str, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if request is within the policy's rate_limit_rpm."""
        if policy.rate_limit_rpm is None:
            return QuotaDecision(allowed=True)

        async with self._locks.acquire(project_id):
            now = datetime.now(timezone.utc)
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

    async def refresh_spend(self, project_id: str) -> None:
        """Pull the shared counter if this instance's figure may be stale.

        Cheap and idempotent: a no-op without persistence, within
        ``SPEND_REFRESH_SECONDS`` of the last read, or if the read fails. Called
        from ``enforce_all`` before ``check_budget`` so the gate does not decide
        against a number that predates other instances' spending.

        The counter is only ever moved forward. A read that returns less than the
        local figure is stale — this instance's own last write is newer than what
        the read observed — and adopting it would hand back budget already spent.
        """
        if not self._shares_spend:
            return
        now = monotonic()
        if now - self._spend_read_at.get(project_id, 0.0) < SPEND_REFRESH_SECONDS:
            return
        self._spend_read_at[project_id] = now
        total = await self._persistence.get_spend(SPEND_SCOPE, project_id)
        if total is None:
            return
        if total > self._spend_tracker.get(project_id, 0.0):
            self._spend_tracker[project_id] = total

    def check_budget(
        self, project_id: str, estimated_cost: float, policy: ResolvedPolicy
    ) -> QuotaDecision:
        """Check if request would exceed the policy's budget_limit.

        Reads the local figure, which ``record_spend`` and ``refresh_spend`` keep
        aligned with the fleet-wide counter. Stays synchronous because it is also
        called directly (``POST /admin/quotas/simulate``); callers on the request
        path go through ``enforce_all``, which refreshes first.
        """
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

    async def enforce_all(
        self,
        project_id: str,
        model: str,
        provider: str | None,
        max_tokens: int | None,
        estimated_cost: float,
        policy: ResolvedPolicy,
    ) -> QuotaDecision:
        """Run all quota checks. Returns first failure or allowed."""
        rate_decision = await self.check_rate_limit(project_id, policy)
        if not rate_decision.allowed:
            return rate_decision

        # Align with the fleet before deciding. Skipped entirely when the policy
        # sets no budget, so gateways that do not use hierarchy budgets never pay
        # for it.
        if policy.budget_limit is not None:
            await self.refresh_spend(project_id)

        checks = [
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

    async def record_spend(
        self,
        project_id: str,
        cost: float,
        budget_limit: float | None = None,
        *,
        share: bool = True,
    ) -> None:
        """Record spend for budget tracking. Fires alerts at threshold crossings.

        With persistence configured the cost goes into a shared counter whose
        atomic ``ADD`` returns the fleet-wide total, so the number
        ``check_budget`` reads next includes every other instance's spend. One
        write and no extra read on the request path.

        The shared write happens *outside* the per-project lock. Holding a lock
        across a network round trip would cap a single project at one request per
        round trip — around 100/s — for no benefit: ``ADD`` is already atomic, and
        because it returns a value that includes exactly this caller's cost, every
        concurrent caller gets a distinct ``(total - cost, total]`` interval. Two
        requests billing $2 and $3 against $8 see [8, 10] and [10, 13] in some
        order: no gap and no overlap, so a threshold still falls inside exactly
        one of them and fires exactly once. The lock is only taken for the local
        dict and the alerted-threshold set, which are read-modify-write.

        ``share=False`` skips the shared counter for spend that every instance
        fabricates identically at startup (the demo seed); see
        ``CostTracker.record_usage``.
        """
        total: float | None = None
        if share and self._shares_spend:
            total = await self._persistence.add_spend(SPEND_SCOPE, project_id, cost)

        async with self._locks.acquire(project_id):
            if total is not None:
                # Both figures move to the fleet view together. Leaving `prev`
                # local while `new_spend` jumped to the fleet total would make the
                # threshold comparison below span an interval this instance never
                # actually crossed, firing every alert at once on its first
                # request.
                prev = total - cost
                new_spend = total
                # Never let the local counter go backwards: responses can arrive
                # out of order, and a lower total would re-admit requests the
                # fleet has already spent past.
                self._spend_tracker[project_id] = max(
                    new_spend, self._spend_tracker.get(project_id, 0.0)
                )
                # Deliberately does NOT stamp `_spend_read_at`. Treating a write
                # as a read looks like a free optimization — the ADD just returned
                # the total — but it makes the refresh interval unreachable for
                # any project under continuous traffic: every request would push
                # the stamp forward, so `refresh_spend` would only ever fire for
                # idle projects, which is exactly backwards. A busy project is the
                # one whose fleet total moves between requests. The saving was at
                # most one read per project per interval; the cost was the fix not
                # working where it matters.
            else:
                # No shared counter, or the write failed — fall back to
                # accumulating locally, i.e. the old per-instance behaviour.
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
        """Get this instance's tracked spend for a project.

        Fleet-accurate once the instance has served a request for the project
        since starting, because ``record_spend`` adopts the shared total. Use
        ``current_spend`` where the answer must be right regardless.
        """
        return self._spend_tracker.get(project_id, 0.0)

    async def current_spend(self, project_id: str) -> float:
        """Fleet-wide spend for a project, read through to the shared counter.

        For admin reads, which happen once per operator request rather than per
        API call, so the extra read is affordable — and where a stale figure is
        the whole problem: an instance that has not served this project since
        starting reports 0 from ``get_spend`` while another is already blocking
        requests against it.

        Falls back to the local figure if there is no shared counter or it cannot
        be read.
        """
        if self._shares_spend:
            total = await self._persistence.get_spend(SPEND_SCOPE, project_id)
            if total is not None:
                self._spend_tracker[project_id] = total
                return total
        return self.get_spend(project_id)

    async def reset_spend(self, project_id: str) -> bool:
        """Reset spend tracking for a project (e.g., at billing cycle reset).

        Returns whether the reset is fleet-wide. False means the shared counter
        still holds the old value and only this instance was cleared, so the
        project stays blocked on every other instance — the caller must not
        report an unqualified success.

        Local state is cleared either way: a partial reset is still better than
        none, and the shared counter is re-read on the next admin request.
        """
        self._spend_tracker.pop(project_id, None)
        self._alerted_thresholds.pop(project_id, None)
        self._spend_read_at.pop(project_id, None)
        if not self._shares_spend:
            return True
        return await self._persistence.reset_spend(SPEND_SCOPE, project_id)

    async def adopt_fleet_spend(self, project_ids) -> None:
        """Seed the local counters from the shared ones at startup.

        Without this, a restarted or newly scaled-out instance believes every
        project has spent nothing until it happens to serve a request for it —
        so the first request after a deploy is admitted against a budget the
        fleet had already exhausted, and ``GET /admin/quotas`` reports $0 spend
        for a project that is over its limit.
        """
        if not self._shares_spend:
            return
        for project_id in project_ids:
            total = await self._persistence.get_spend(SPEND_SCOPE, project_id)
            if total is not None:
                self._spend_tracker[project_id] = total

    def cap_max_tokens(self, requested: int | None, policy: ResolvedPolicy) -> int | None:
        """Return the effective max_tokens — capped by policy if needed."""
        if policy.max_tokens_per_request is None:
            return requested
        if requested is None:
            return policy.max_tokens_per_request
        return min(requested, policy.max_tokens_per_request)
