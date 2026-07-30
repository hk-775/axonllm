"""Unit tests for the Level 2+3 SemanticEfficiencyEngine."""

from datetime import datetime, timedelta

import pytest

from src.gateway.cost_tracker import CostTracker
from src.gateway.models import UsageRecord
from src.gateway.semantic_efficiency import (
    ComplexityTier,
    SemanticEfficiencyEngine,
)
from src.gateway.task_classifier import TaskClassifier


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
    task_type: str = "",
) -> UsageRecord:
    # task_type defaults to "" — the same value a record written before the field
    # existed deserializes to — so every pre-existing test here exercises the
    # unclassified path unless it opts in.
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
        task_type=task_type,
    )


def _build_engine(records: list[UsageRecord] | None = None) -> SemanticEfficiencyEngine:
    tracker = CostTracker(pricing_config={})
    if records:
        tracker._records = records
    classifier = TaskClassifier()
    return SemanticEfficiencyEngine(task_classifier=classifier, cost_tracker=tracker)


# ---------------------------------------------------------------------------
# Tests — prompt analysis
# ---------------------------------------------------------------------------


class TestPromptAnalysis:
    def test_simple_prompt(self):
        engine = _build_engine()
        messages = [
            {"role": "user", "content": "Say hello"},
        ]
        analysis = engine.analyze_prompt(messages, model="claude-opus")

        assert analysis.complexity in (ComplexityTier.TRIVIAL, ComplexityTier.SIMPLE)
        assert analysis.is_overprovisioned is True  # Opus for "Say hello"
        assert analysis.prompt_length_tokens > 0

    def test_complex_coding_prompt(self):
        engine = _build_engine()
        messages = [
            {"role": "system", "content": "You are a senior software engineer."},
            {"role": "user", "content": (
                "Implement a distributed lock manager using Redis with the following requirements:\n"
                "1. Support lock acquisition with timeout\n"
                "2. Implement lock renewal for long-running operations\n"
                "3. Handle network partitions gracefully\n"
                "4. Provide a Python class with async interface\n"
                "```python\nclass DistributedLockManager:\n    pass\n```\n"
                "Compare this approach with Zookeeper-based locking and explain the tradeoffs step by step."
            )},
        ]
        analysis = engine.analyze_prompt(messages, model="claude-opus")

        assert analysis.complexity in (ComplexityTier.COMPLEX, ComplexityTier.EXPERT)
        assert analysis.task_type == "coding"
        assert analysis.is_overprovisioned is False

    def test_system_prompt_ratio(self):
        engine = _build_engine()
        long_system = "You are an assistant. " * 200
        messages = [
            {"role": "system", "content": long_system},
            {"role": "user", "content": "Hello"},
        ]
        analysis = engine.analyze_prompt(messages)

        assert analysis.system_prompt_ratio > 0.5
        assert analysis.compression_opportunity > 0

    def test_long_history_detection(self):
        engine = _build_engine()
        messages = [{"role": "system", "content": "Be helpful."}]
        for i in range(10):
            messages.append({"role": "user", "content": f"Question {i}"})
            messages.append({"role": "assistant", "content": f"Answer {i} with some detail and explanation."})
        messages.append({"role": "user", "content": "Final question"})

        analysis = engine.analyze_prompt(messages)
        assert analysis.history_token_count > 0
        assert analysis.compression_opportunity > 0

    def test_no_model_no_overprovisioning(self):
        engine = _build_engine()
        messages = [{"role": "user", "content": "Hello"}]
        analysis = engine.analyze_prompt(messages, model=None)

        assert analysis.actual_model_tier is None
        assert analysis.is_overprovisioned is False


# ---------------------------------------------------------------------------
# Tests — redundancy detection
# ---------------------------------------------------------------------------


class TestRedundancy:
    def test_multiple_system_prompts(self):
        engine = _build_engine()
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Be concise."},
        ]
        analysis = engine.analyze_prompt(messages)
        assert "multiple_system_prompts" in analysis.redundancy_indicators

    def test_duplicate_user_message(self):
        engine = _build_engine()
        messages = [
            {"role": "user", "content": "What is the weather?"},
            {"role": "assistant", "content": "It's sunny."},
            {"role": "user", "content": "What is the weather?"},
        ]
        analysis = engine.analyze_prompt(messages)
        assert "duplicate_user_message" in analysis.redundancy_indicators

    def test_excessive_conversation(self):
        engine = _build_engine()
        messages = []
        for i in range(25):
            messages.append({"role": "user", "content": f"Message {i}"})
            messages.append({"role": "assistant", "content": f"Reply {i}"})

        analysis = engine.analyze_prompt(messages)
        assert "excessive_conversation_length" in analysis.redundancy_indicators


# ---------------------------------------------------------------------------
# Tests — output utilization analysis
# ---------------------------------------------------------------------------


class TestOutputAnalysis:
    def test_short_responses_detected(self):
        records = [
            _make_record(completion_tokens=20) for _ in range(10)
        ]
        engine = _build_engine(records)
        analysis = engine.analyze_output_utilization(records)

        assert analysis.avg_completion_tokens == pytest.approx(20.0)
        assert analysis.recommendation is not None
        assert "under 50 tokens" in analysis.recommendation

    def test_healthy_output(self):
        records = [
            _make_record(completion_tokens=300 + i * 50) for i in range(10)
        ]
        engine = _build_engine(records)
        analysis = engine.analyze_output_utilization(records)

        assert analysis.recommendation is None

    def test_empty_records(self):
        engine = _build_engine()
        analysis = engine.analyze_output_utilization([])

        assert analysis.avg_completion_tokens == 0.0


# ---------------------------------------------------------------------------
# Tests — user profile
# ---------------------------------------------------------------------------


class TestUserProfile:
    def test_builds_profile(self):
        records = [
            _make_record(user_id="alice", model="claude-opus", prompt_tokens=200, completion_tokens=50, cost=0.10)
            for _ in range(10)
        ]
        engine = _build_engine(records)
        profile = engine.build_user_profile("alice")

        assert profile.user_id == "alice"
        assert profile.typical_model == "claude-opus"
        assert profile.estimated_monthly_savings > 0
        assert "short_responses_from_expensive_models" in profile.patterns

    def test_empty_user_profile(self):
        engine = _build_engine()
        profile = engine.build_user_profile("nobody")

        assert profile.user_id == "nobody"
        assert profile.dominant_task_type == "unknown"
        assert profile.estimated_monthly_savings == 0.0

    def test_profile_cached(self):
        records = [_make_record(user_id="alice")]
        engine = _build_engine(records)

        engine.build_user_profile("alice")
        cached = engine.get_user_profile("alice")
        assert cached is not None
        assert cached.user_id == "alice"


class TestDominantTaskType:
    """``dominant_task_type`` was a hardcoded "general" until it was derived here.

    The regression these tests exist to prevent is not a crash — it is the field
    quietly reporting a constant again, which reads as a plausible answer.
    """

    def test_reports_the_most_common_task_type(self):
        records = (
            [_make_record(user_id="alice", task_type="math") for _ in range(7)]
            + [_make_record(user_id="alice", task_type="coding") for _ in range(3)]
        )
        engine = _build_engine(records)

        assert engine.build_user_profile("alice").dominant_task_type == "math"

    def test_a_math_only_user_does_not_report_general(self):
        """The literal's most visible symptom: everyone looked like "general"."""
        records = [_make_record(user_id="alice", task_type="math") for _ in range(5)]
        engine = _build_engine(records)

        profile = engine.build_user_profile("alice")
        assert profile.dominant_task_type == "math"
        assert profile.dominant_task_type != "general"

    def test_general_is_reported_when_it_is_the_real_answer(self):
        """"general" must stay reachable — it is a task type, not just a default."""
        records = [_make_record(user_id="alice", task_type="general") for _ in range(4)]
        engine = _build_engine(records)

        assert engine.build_user_profile("alice").dominant_task_type == "general"

    def test_unclassified_records_report_unknown_not_general(self):
        """Rows written before ``task_type`` existed deserialize to "".

        Bucketing those as "general" would recreate the original bug for exactly
        the population most likely to hit it: historical data.
        """
        records = [_make_record(user_id="alice") for _ in range(10)]
        engine = _build_engine(records)

        assert engine.build_user_profile("alice").dominant_task_type == "unknown"

    def test_unclassified_records_do_not_outvote_classified_ones(self):
        """The mode is over *classified* records only.

        With 9 unclassified rows and 1 math row, counting "" as a bucket would
        make "" the winner and the profile would report an empty task type.
        """
        records = [_make_record(user_id="alice") for _ in range(9)]
        records.append(_make_record(user_id="alice", task_type="math"))
        engine = _build_engine(records)

        assert engine.build_user_profile("alice").dominant_task_type == "math"

    def test_other_users_records_are_not_counted(self):
        records = (
            [_make_record(user_id="alice", task_type="math") for _ in range(2)]
            + [_make_record(user_id="bob", task_type="coding") for _ in range(20)]
        )
        engine = _build_engine(records)

        assert engine.build_user_profile("alice").dominant_task_type == "math"
        assert engine.build_user_profile("bob").dominant_task_type == "coding"


# ---------------------------------------------------------------------------
# Tests — semantic recommendations
# ---------------------------------------------------------------------------


class TestSemanticRecommendations:
    def test_recommends_downgrade_for_simple_prompts(self):
        records = [
            _make_record(model="claude-opus", prompt_tokens=100, completion_tokens=50, cost=0.10)
            for _ in range(10)
        ]
        engine = _build_engine(records)
        report = engine.generate_report(user_id="alice")

        assert len(report.model_recommendations) > 0
        rec = report.model_recommendations[0]
        assert rec.current_model == "claude-opus"
        assert rec.estimated_savings_pct > 0

    def test_no_recommendation_for_cheap_model(self):
        records = [
            _make_record(model="nova-micro", prompt_tokens=100, completion_tokens=50, cost=0.001)
            for _ in range(10)
        ]
        engine = _build_engine(records)
        report = engine.generate_report(user_id="alice")

        assert len(report.model_recommendations) == 0


# ---------------------------------------------------------------------------
# Tests — waste summary
# ---------------------------------------------------------------------------


class TestWasteSummary:
    def test_waste_detected(self):
        records = [
            _make_record(model="claude-opus", prompt_tokens=100, completion_tokens=50, cost=0.10)
            for _ in range(10)
        ]
        engine = _build_engine(records)
        report = engine.generate_report(user_id="alice")

        assert report.waste_summary["estimated_wasted_cost"] > 0
        assert report.waste_summary["waste_pct"] > 0
        assert "model_overprovisioning" in report.waste_summary["waste_categories"]

    def test_no_waste_for_cheap_model(self):
        records = [
            _make_record(model="nova-micro", prompt_tokens=100, completion_tokens=50, cost=0.001)
            for _ in range(10)
        ]
        engine = _build_engine(records)
        report = engine.generate_report(user_id="alice")

        assert report.waste_summary["estimated_wasted_cost"] == 0

    def test_empty_waste_summary(self):
        engine = _build_engine()
        report = engine.generate_report(user_id="nobody")

        assert report.waste_summary["total_cost"] == 0.0
        assert report.waste_summary["waste_pct"] == 0.0


# ---------------------------------------------------------------------------
# Tests — full report generation
# ---------------------------------------------------------------------------


class TestFullReport:
    def test_report_with_user(self):
        records = [
            _make_record(user_id="alice", model="claude-opus", cost=0.10),
            _make_record(user_id="alice", model="claude-sonnet", cost=0.01),
        ]
        engine = _build_engine(records)
        report = engine.generate_report(user_id="alice")

        assert report.user_profile is not None
        assert report.output_analysis is not None
        assert report.waste_summary is not None

    def test_report_with_project(self):
        records = [
            _make_record(user_id="alice", project_id="proj-1", cost=0.10),
            _make_record(user_id="bob", project_id="proj-1", cost=0.01),
        ]
        engine = _build_engine(records)
        report = engine.generate_report(project_id="proj-1")

        assert report.user_profile is None  # No user_id specified
        assert report.output_analysis is not None
        assert report.waste_summary is not None

    def test_report_empty(self):
        engine = _build_engine()
        report = engine.generate_report(user_id="nobody")

        assert report.user_profile is not None
        assert report.waste_summary["total_cost"] == 0.0


# ---------------------------------------------------------------------------
# Tests — complexity assessment
# ---------------------------------------------------------------------------


class TestComplexity:
    def test_trivial_question(self):
        engine = _build_engine()
        messages = [{"role": "user", "content": "Hi"}]
        analysis = engine.analyze_prompt(messages)
        assert analysis.complexity in (ComplexityTier.TRIVIAL, ComplexityTier.SIMPLE)

    def test_complex_reasoning(self):
        engine = _build_engine()
        messages = [{"role": "user", "content": (
            "Analyze why the Byzantine Generals Problem is fundamentally unsolvable "
            "with fewer than 3f+1 generals when f are traitorous. Provide a formal "
            "proof by contradiction and compare step by step with the Two Generals Problem. "
            "Then explain how practical systems like PBFT work around these theoretical limits."
        )}]
        analysis = engine.analyze_prompt(messages)
        assert analysis.complexity in (ComplexityTier.COMPLEX, ComplexityTier.EXPERT)
