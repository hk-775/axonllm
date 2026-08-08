"""Unit tests for the Level 1 EfficiencyAnalyzer."""

from datetime import datetime, timedelta

import pytest

from src.gateway.cost_tracker import CostTracker
from src.gateway.efficiency_analyzer import EfficiencyAnalyzer, EXPENSIVE_MODELS
from src.gateway.models import (
    EfficiencyGrade,
    TokenPricing,
    UsageRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(
    user_id: str = "alice",
    project_id: str = "proj-1",
    model: str = "claude-sonnet",
    provider: str = "bedrock",
    prompt_tokens: int = 500,
    completion_tokens: int = 200,
    cost: float = 0.01,
    cached_tokens: int = 0,
    timestamp: datetime | None = None,
    tenant_id: str | None = None,
) -> UsageRecord:
    return UsageRecord(
        request_id=f"req-{id(object())}",
        project_id=project_id,
        user_id=user_id,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
        timestamp=timestamp or datetime.utcnow(),
        cached_tokens=cached_tokens,
        tenant_id=tenant_id,
    )


def _build_tracker(records: list[UsageRecord]) -> CostTracker:
    tracker = CostTracker(pricing_config={})
    tracker._records = records
    return tracker


# ---------------------------------------------------------------------------
# Tests — metric computation
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_basic_metrics(self):
        records = [
            _make_record(prompt_tokens=1000, completion_tokens=200, cost=0.05, cached_tokens=100),
            _make_record(prompt_tokens=800, completion_tokens=300, cost=0.03, cached_tokens=200),
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        m = report.metrics
        assert m.entity_id == "alice"
        assert m.entity_type == "user"
        assert m.total_requests == 2
        assert m.total_cost == pytest.approx(0.08)
        assert m.avg_cost_per_request == pytest.approx(0.04)
        # Completion/prompt = 500/1800
        assert m.completion_prompt_ratio == pytest.approx(500 / 1800, abs=0.01)
        # Cache utilization = 300/1800
        assert m.cache_utilization_rate == pytest.approx(300 / 1800, abs=0.01)

    def test_empty_records(self):
        tracker = _build_tracker([])
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("nobody")

        assert report.metrics.total_requests == 0
        assert report.metrics.score == 100.0
        assert report.metrics.grade == EfficiencyGrade.GOOD
        assert report.alerts == []
        assert report.recommendations == []

    def test_expensive_model_ratio(self):
        records = [
            _make_record(model="claude-opus", cost=0.10),
            _make_record(model="claude-opus", cost=0.10),
            _make_record(model="claude-sonnet", cost=0.01),
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert report.metrics.expensive_model_ratio == pytest.approx(2 / 3, abs=0.01)


class TestGrading:
    def test_excellent_grade(self):
        records = [
            _make_record(
                prompt_tokens=500,
                completion_tokens=300,
                cost=0.01,
                cached_tokens=200,
                model="claude-sonnet",
            )
            for _ in range(10)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert report.metrics.grade in (EfficiencyGrade.EXCELLENT, EfficiencyGrade.GOOD)
        assert report.metrics.score >= 70

    def test_wasteful_grade(self):
        now = datetime.utcnow()
        records = [
            _make_record(
                prompt_tokens=5000,
                completion_tokens=20,
                cost=0.50,
                model="claude-opus",
                timestamp=now + timedelta(seconds=i),
            )
            for i in range(10)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert report.metrics.grade in (EfficiencyGrade.POOR, EfficiencyGrade.WASTEFUL)
        assert report.metrics.score < 50


# ---------------------------------------------------------------------------
# Tests — alert generation
# ---------------------------------------------------------------------------


class TestAlerts:
    def test_low_completion_prompt_ratio_alert(self):
        records = [
            _make_record(prompt_tokens=5000, completion_tokens=10, cost=0.05)
            for _ in range(5)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        alert_types = [a.alert_type for a in report.alerts]
        assert "low_completion_prompt_ratio" in alert_types

    def test_low_cache_utilization_alert(self):
        records = [
            _make_record(prompt_tokens=1000, completion_tokens=200, cached_tokens=0, cost=0.01)
            for _ in range(10)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        alert_types = [a.alert_type for a in report.alerts]
        assert "low_cache_utilization" in alert_types

    def test_high_expensive_model_alert(self):
        records = [
            _make_record(model="claude-opus", cost=0.10)
            for _ in range(10)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        alert_types = [a.alert_type for a in report.alerts]
        assert "high_expensive_model_usage" in alert_types

    def test_bloated_prompts_alert(self):
        records = [
            _make_record(prompt_tokens=6000, completion_tokens=500, cost=0.10)
            for _ in range(5)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        alert_types = [a.alert_type for a in report.alerts]
        assert "bloated_prompts" in alert_types

    def test_duplicate_requests_alert(self):
        now = datetime.utcnow()
        records = [
            _make_record(
                prompt_tokens=500,
                completion_tokens=200,
                cost=0.01,
                model="claude-sonnet",
                timestamp=now + timedelta(seconds=i * 10),
            )
            for i in range(20)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        alert_types = [a.alert_type for a in report.alerts]
        assert "high_duplicate_requests" in alert_types

    def test_no_alerts_for_efficient_user(self):
        now = datetime.utcnow()
        records = [
            _make_record(
                prompt_tokens=400 + i * 100,
                completion_tokens=200 + i * 50,
                cost=0.01,
                cached_tokens=200,
                model="claude-sonnet",
                timestamp=now + timedelta(hours=i),
            )
            for i in range(5)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        critical_alerts = [a for a in report.alerts if a.severity == "critical"]
        assert len(critical_alerts) == 0


# ---------------------------------------------------------------------------
# Tests — recommendations
# ---------------------------------------------------------------------------


class TestRecommendations:
    def test_recommends_cheaper_model_for_short_responses(self):
        records = [
            _make_record(
                model="claude-opus",
                prompt_tokens=200,
                completion_tokens=50,
                cost=0.10,
            )
            for _ in range(10)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert len(report.recommendations) > 0
        rec = report.recommendations[0]
        assert rec.current_model == "claude-opus"
        assert rec.estimated_savings_pct > 0

    def test_no_recommendation_for_cheap_model(self):
        records = [
            _make_record(
                model="nova-micro",
                prompt_tokens=200,
                completion_tokens=50,
                cost=0.001,
            )
            for _ in range(10)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert len(report.recommendations) == 0


# ---------------------------------------------------------------------------
# Tests — peer comparison
# ---------------------------------------------------------------------------


class TestPeerComparison:
    def test_peer_comparison_with_peers(self):
        records = [
            _make_record(user_id="alice", project_id="proj-1", cost=0.10),
            _make_record(user_id="alice", project_id="proj-1", cost=0.10),
            _make_record(user_id="bob", project_id="proj-1", cost=0.01),
            _make_record(user_id="bob", project_id="proj-1", cost=0.01),
            _make_record(user_id="carol", project_id="proj-1", cost=0.02),
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        pc = report.peer_comparison
        assert pc["peers_found"] == 2
        assert pc["user_avg_cost_per_request"] == pytest.approx(0.10)
        assert pc["vs_avg_pct"] > 0  # Alice is above average

    def test_peer_comparison_no_peers(self):
        records = [
            _make_record(user_id="alice", project_id="proj-1", cost=0.10),
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        pc = report.peer_comparison
        assert pc["peers_found"] == 0


# ---------------------------------------------------------------------------
# Tests — project analysis
# ---------------------------------------------------------------------------


class TestProjectAnalysis:
    def test_project_analysis(self):
        records = [
            _make_record(user_id="alice", project_id="proj-1", cost=0.10),
            _make_record(user_id="bob", project_id="proj-1", cost=0.01),
            _make_record(user_id="carol", project_id="proj-1", cost=0.01),
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_project("proj-1")

        assert report.metrics.entity_type == "project"
        assert report.metrics.total_requests == 3

        # Alice is an outlier (10x more expensive than average)
        assert "alice" in report.peer_comparison.get("outlier_users", [])

    def test_all_user_metrics(self):
        records = [
            _make_record(user_id="alice", cost=0.10),
            _make_record(user_id="bob", cost=0.01),
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        all_metrics = analyzer.get_all_user_metrics()

        assert len(all_metrics) == 2
        user_ids = {m.entity_id for m in all_metrics}
        assert user_ids == {"alice", "bob"}

    def test_colliding_user_and_project_ids_are_tenant_isolated(self):
        records = [
            _make_record(
                tenant_id="tenant-a",
                user_id="shared-user",
                project_id="shared-project",
                cost=0.10,
            ),
            _make_record(
                tenant_id="tenant-b",
                user_id="shared-user",
                project_id="shared-project",
                cost=9.00,
            ),
            _make_record(
                tenant_id="tenant-b",
                user_id="other-user",
                project_id="shared-project",
                cost=8.00,
            ),
        ]
        analyzer = EfficiencyAnalyzer(_build_tracker(records))

        user = analyzer.analyze_user(
            "shared-user",
            tenant_id="tenant-a",
        )
        project = analyzer.analyze_project(
            "shared-project",
            tenant_id="tenant-a",
        )
        users = analyzer.get_all_user_metrics(tenant_id="tenant-a")

        assert user.metrics.total_requests == 1
        assert user.metrics.total_cost == pytest.approx(0.10)
        assert user.peer_comparison["peers_found"] == 0
        assert project.metrics.total_requests == 1
        assert project.metrics.total_cost == pytest.approx(0.10)
        assert [metric.entity_id for metric in users] == ["shared-user"]


# ---------------------------------------------------------------------------
# Tests — token velocity
# ---------------------------------------------------------------------------


class TestTokenVelocity:
    def test_high_velocity(self):
        now = datetime.utcnow()
        records = [
            _make_record(
                prompt_tokens=5000,
                completion_tokens=5000,
                cost=0.10,
                timestamp=now + timedelta(seconds=i),
            )
            for i in range(100)
        ]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert report.metrics.token_velocity_per_hour > 10000

    def test_single_record_velocity(self):
        records = [_make_record()]
        tracker = _build_tracker(records)
        analyzer = EfficiencyAnalyzer(tracker)
        report = analyzer.analyze_user("alice")

        assert report.metrics.token_velocity_per_hour == 0.0


# ---------------------------------------------------------------------------
# Regression — mixed naive/aware timestamps must not crash
# (usage records reach the analyzer from sources that write naive
# datetime.utcnow() and tz-aware datetime.now(timezone.utc); comparing them
# raised TypeError and 500'd /admin/efficiency — "Failed to load efficiency data")
# ---------------------------------------------------------------------------


class TestMixedTimezoneTimestamps:
    def test_get_all_user_metrics_with_mixed_tz(self):
        from datetime import timezone

        records = [
            _make_record(user_id="alice", timestamp=datetime.utcnow()),               # naive
            _make_record(user_id="alice", timestamp=datetime.now(timezone.utc)),      # aware
        ]
        analyzer = EfficiencyAnalyzer(_build_tracker(records))
        metrics = analyzer.get_all_user_metrics()  # must not raise
        assert len(metrics) == 1
        assert metrics[0].entity_id == "alice"

    def test_analyze_user_velocity_and_duplicates_with_mixed_tz(self):
        from datetime import timezone

        naive = datetime.utcnow()
        aware = datetime.now(timezone.utc)
        records = [
            _make_record(user_id="bob", timestamp=naive),
            _make_record(user_id="bob", timestamp=aware),
        ]
        analyzer = EfficiencyAnalyzer(_build_tracker(records))
        report = analyzer.analyze_user("bob")  # exercises velocity + duplicate helpers
        assert report.metrics.total_requests == 2
        assert report.metrics.token_velocity_per_hour >= 0.0
