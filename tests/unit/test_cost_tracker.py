"""Unit tests for CostTracker."""

import pytest
from datetime import datetime, timedelta

from src.gateway.cost_tracker import CostTracker
from src.gateway.models import (
    BudgetStatus,
    TokenPricing,
    UsageFilters,
    UsageRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _pricing_config():
    return {
        "openai": {
            "gpt-4": TokenPricing(prompt_token_cost=0.03, completion_token_cost=0.06),
            "gpt-3.5-turbo": TokenPricing(prompt_token_cost=0.001, completion_token_cost=0.002),
        },
        "anthropic": {
            "claude-3-sonnet": TokenPricing(prompt_token_cost=0.003, completion_token_cost=0.015),
        },
    }


def _make_record(
    *,
    request_id="req-1",
    project_id="proj-1",
    user_id="user-1",
    provider="openai",
    model="gpt-4",
    prompt_tokens=100,
    completion_tokens=50,
    cost=0.006,
    timestamp=None,
    tenant_id=None,
):
    return UsageRecord(
        request_id=request_id,
        project_id=project_id,
        user_id=user_id,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
        timestamp=timestamp or datetime.utcnow(),
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# calculate_cost
# ---------------------------------------------------------------------------

class TestCalculateCost:
    def test_basic_cost_calculation(self):
        tracker = CostTracker(_pricing_config())
        # (100/1000 * 0.03) + (50/1000 * 0.06) = 0.003 + 0.003 = 0.006
        cost = tracker.calculate_cost("openai", "gpt-4", 100, 50)
        assert cost == pytest.approx(0.006)

    def test_zero_tokens(self):
        tracker = CostTracker(_pricing_config())
        assert tracker.calculate_cost("openai", "gpt-4", 0, 0) == 0.0

    def test_unknown_provider_returns_zero(self):
        tracker = CostTracker(_pricing_config())
        assert tracker.calculate_cost("unknown", "gpt-4", 100, 50) == 0.0

    def test_unknown_model_returns_zero(self):
        tracker = CostTracker(_pricing_config())
        assert tracker.calculate_cost("openai", "unknown-model", 100, 50) == 0.0

    def test_zero_placeholder_is_not_usable_pricing(self):
        tracker = CostTracker(
            {"openai": {"placeholder": TokenPricing(0.0, 0.0)}}
        )

        assert tracker.has_pricing("openai", "placeholder") is False
        assert tracker.calculate_cost(
            "openai",
            "placeholder",
            100,
            50,
        ) == 0.0

    def test_real_rate_is_usable_pricing(self):
        tracker = CostTracker(_pricing_config())

        assert tracker.has_pricing("openai", "gpt-4") is True

    def test_different_provider_model(self):
        tracker = CostTracker(_pricing_config())
        # (200/1000 * 0.003) + (100/1000 * 0.015) = 0.0006 + 0.0015 = 0.0021
        cost = tracker.calculate_cost("anthropic", "claude-3-sonnet", 200, 100)
        assert cost == pytest.approx(0.0021)


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------

class TestRecordUsage:
    @pytest.mark.asyncio
    async def test_record_persists(self):
        tracker = CostTracker(_pricing_config())
        record = _make_record()
        await tracker.record_usage(record)
        assert len(tracker._records) == 1
        assert tracker._records[0] is record

    @pytest.mark.asyncio
    async def test_multiple_records(self):
        tracker = CostTracker(_pricing_config())
        await tracker.record_usage(_make_record(request_id="r1"))
        await tracker.record_usage(_make_record(request_id="r2"))
        assert len(tracker._records) == 2


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    @pytest.mark.asyncio
    async def test_estimate_returns_positive_int(self):
        tracker = CostTracker(_pricing_config())
        count = await tracker.estimate_tokens("Hello, world!", "gpt-4")
        assert isinstance(count, int)
        assert count > 0

    @pytest.mark.asyncio
    async def test_empty_string_returns_zero(self):
        tracker = CostTracker(_pricing_config())
        count = await tracker.estimate_tokens("", "gpt-4")
        assert count == 0

    @pytest.mark.asyncio
    async def test_unknown_model_falls_back(self):
        tracker = CostTracker(_pricing_config())
        # Should not raise, falls back to cl100k_base
        count = await tracker.estimate_tokens("Hello", "totally-unknown-model")
        assert isinstance(count, int)
        assert count > 0


# ---------------------------------------------------------------------------
# check_budget
# ---------------------------------------------------------------------------

class TestCheckBudget:
    @pytest.mark.asyncio
    async def test_under_budget(self):
        tracker = CostTracker(_pricing_config())
        tracker.register_project("proj-1", budget_limit=100.0, alert_threshold=80.0)
        await tracker.record_usage(_make_record(cost=10.0))
        status = await tracker.check_budget("proj-1")
        assert status.current_spend == pytest.approx(10.0)
        assert status.is_over_budget is False
        assert status.is_alert_triggered is False

    @pytest.mark.asyncio
    async def test_alert_triggered(self):
        tracker = CostTracker(_pricing_config())
        tracker.register_project("proj-1", budget_limit=100.0, alert_threshold=50.0)
        await tracker.record_usage(_make_record(cost=60.0))
        status = await tracker.check_budget("proj-1")
        assert status.is_alert_triggered is True
        assert status.is_over_budget is False

    @pytest.mark.asyncio
    async def test_over_budget(self):
        tracker = CostTracker(_pricing_config())
        tracker.register_project("proj-1", budget_limit=100.0, alert_threshold=80.0)
        await tracker.record_usage(_make_record(cost=100.0))
        status = await tracker.check_budget("proj-1")
        assert status.is_over_budget is True
        assert status.is_alert_triggered is True

    @pytest.mark.asyncio
    async def test_no_budget_configured(self):
        tracker = CostTracker(_pricing_config())
        status = await tracker.check_budget("unknown-project")
        assert status.budget_limit is None
        assert status.alert_threshold is None
        assert status.is_over_budget is False
        assert status.is_alert_triggered is False
        assert status.current_spend == 0.0

    @pytest.mark.asyncio
    async def test_only_counts_project_records(self):
        tracker = CostTracker(_pricing_config())
        tracker.register_project("proj-1", budget_limit=100.0, alert_threshold=50.0)
        await tracker.record_usage(_make_record(project_id="proj-1", cost=30.0))
        await tracker.record_usage(_make_record(project_id="proj-2", cost=999.0))
        status = await tracker.check_budget("proj-1")
        assert status.current_spend == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_identical_project_and_user_ids_are_isolated_by_tenant(self):
        tracker = CostTracker(_pricing_config())
        for tenant_id in ("tenant-a", "tenant-b"):
            tracker.register_project(
                "shared-project",
                budget_limit=100.0,
                tenant_id=tenant_id,
            )
            tracker.register_user(
                "shared-user",
                budget_limit=100.0,
                tenant_id=tenant_id,
            )

        await tracker.record_usage(
            _make_record(
                project_id="shared-project",
                user_id="shared-user",
                tenant_id="tenant-a",
                cost=75.0,
            )
        )
        await tracker.record_usage(
            _make_record(
                request_id="req-2",
                project_id="shared-project",
                user_id="shared-user",
                tenant_id="tenant-b",
                cost=5.0,
            )
        )

        project_a = await tracker.check_budget(
            "shared-project",
            tenant_id="tenant-a",
        )
        project_b = await tracker.check_budget(
            "shared-project",
            tenant_id="tenant-b",
        )
        user_a = await tracker.check_user_budget(
            "shared-user",
            tenant_id="tenant-a",
        )
        user_b = await tracker.check_user_budget(
            "shared-user",
            tenant_id="tenant-b",
        )

        assert project_a.current_spend == pytest.approx(75.0)
        assert project_b.current_spend == pytest.approx(5.0)
        assert user_a.current_spend == pytest.approx(75.0)
        assert user_b.current_spend == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# get_aggregated_usage
# ---------------------------------------------------------------------------

class TestGetAggregatedUsage:
    @pytest.mark.asyncio
    async def test_no_records(self):
        tracker = CostTracker(_pricing_config())
        report = await tracker.get_aggregated_usage(UsageFilters())
        assert report.total_requests == 0
        assert report.total_tokens == 0
        assert report.total_cost == 0.0
        assert report.breakdown == []

    @pytest.mark.asyncio
    async def test_filter_by_provider(self):
        tracker = CostTracker(_pricing_config())
        await tracker.record_usage(_make_record(provider="openai", cost=1.0))
        await tracker.record_usage(_make_record(provider="anthropic", cost=2.0))
        report = await tracker.get_aggregated_usage(UsageFilters(provider="openai"))
        assert report.total_requests == 1
        assert report.total_cost == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_filter_by_time_range(self):
        tracker = CostTracker(_pricing_config())
        now = datetime.utcnow()
        old = now - timedelta(days=10)
        await tracker.record_usage(_make_record(timestamp=old, cost=1.0))
        await tracker.record_usage(_make_record(timestamp=now, cost=2.0))
        report = await tracker.get_aggregated_usage(
            UsageFilters(start_time=now - timedelta(days=1))
        )
        assert report.total_requests == 1
        assert report.total_cost == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_filter_by_project_and_user(self):
        tracker = CostTracker(_pricing_config())
        await tracker.record_usage(_make_record(project_id="p1", user_id="u1", cost=1.0))
        await tracker.record_usage(_make_record(project_id="p1", user_id="u2", cost=2.0))
        await tracker.record_usage(_make_record(project_id="p2", user_id="u1", cost=3.0))
        report = await tracker.get_aggregated_usage(
            UsageFilters(project_id="p1", user_id="u1")
        )
        assert report.total_requests == 1
        assert report.total_cost == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_breakdown_includes_all_dimensions(self):
        tracker = CostTracker(_pricing_config())
        await tracker.record_usage(_make_record())
        report = await tracker.get_aggregated_usage(UsageFilters())
        group_bys = {b.group_by for b in report.breakdown}
        assert group_bys == {"provider", "model", "project", "user"}

    @pytest.mark.asyncio
    async def test_filter_by_model(self):
        tracker = CostTracker(_pricing_config())
        await tracker.record_usage(_make_record(model="gpt-4", cost=5.0))
        await tracker.record_usage(_make_record(model="gpt-3.5-turbo", cost=1.0))
        report = await tracker.get_aggregated_usage(UsageFilters(model="gpt-4"))
        assert report.total_requests == 1
        assert report.total_cost == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Regression (task #7) — time-window filters must tolerate mixed naive/aware
# timestamps. Production now writes aware timestamps, but persisted/legacy
# records may be naive and filter bounds may be either; comparing the two used
# to raise TypeError in _apply_filters.
# ---------------------------------------------------------------------------

from datetime import timezone as _tz


class TestMixedTimezoneFilters:
    def _tracker_with_mixed_records(self):
        tracker = CostTracker(_pricing_config())
        tracker._records = [
            _make_record(request_id="naive", timestamp=datetime.utcnow()),           # naive
            _make_record(request_id="aware", timestamp=datetime.now(_tz.utc)),        # aware
        ]
        return tracker

    def test_aware_start_bound_over_mixed_records(self):
        tracker = self._tracker_with_mixed_records()
        out = tracker._apply_filters(
            UsageFilters(start_time=datetime(2020, 1, 1, tzinfo=_tz.utc))
        )
        assert len(out) == 2  # no TypeError, both pass the ancient lower bound

    def test_naive_start_bound_over_mixed_records(self):
        tracker = self._tracker_with_mixed_records()
        out = tracker._apply_filters(UsageFilters(start_time=datetime(2020, 1, 1)))
        assert len(out) == 2

    def test_end_bound_over_mixed_records(self):
        tracker = self._tracker_with_mixed_records()
        out = tracker._apply_filters(
            UsageFilters(end_time=datetime(2100, 1, 1, tzinfo=_tz.utc))
        )
        assert len(out) == 2
