"""Unit tests for FeedbackTracker."""

from datetime import datetime, timedelta, timezone

import pytest

from src.gateway.feedback_tracker import FeedbackTracker
from src.gateway.models import FeedbackRecord


def _make_record(
    task_type: str = "coding",
    model: str = "claude-opus",
    timestamp: datetime | None = None,
) -> FeedbackRecord:
    return FeedbackRecord(
        request_id="req-1",
        timestamp=timestamp or datetime.now(timezone.utc),
        task_type=task_type,
        confidence=0.85,
        selected_model=model,
        benchmark_score=95.0,
    )


class TestRecord:
    def test_record_stores_entry(self):
        tracker = FeedbackTracker()
        record = _make_record()
        tracker.record(record)
        assert tracker.get_records() == [record]

    def test_record_multiple_entries(self):
        tracker = FeedbackTracker()
        r1 = _make_record(task_type="coding")
        r2 = _make_record(task_type="math")
        tracker.record(r1)
        tracker.record(r2)
        assert len(tracker.get_records()) == 2


class TestGetRecords:
    def test_filter_by_task_type(self):
        tracker = FeedbackTracker()
        tracker.record(_make_record(task_type="coding"))
        tracker.record(_make_record(task_type="math"))
        tracker.record(_make_record(task_type="coding"))

        results = tracker.get_records(task_type="coding")
        assert len(results) == 2
        assert all(r.task_type == "coding" for r in results)

    def test_filter_by_model_name(self):
        tracker = FeedbackTracker()
        tracker.record(_make_record(model="claude-opus"))
        tracker.record(_make_record(model="gpt-4o"))
        tracker.record(_make_record(model="claude-opus"))

        results = tracker.get_records(model_name="claude-opus")
        assert len(results) == 2
        assert all(r.selected_model == "claude-opus" for r in results)

    def test_filter_by_both(self):
        tracker = FeedbackTracker()
        tracker.record(_make_record(task_type="coding", model="claude-opus"))
        tracker.record(_make_record(task_type="coding", model="gpt-4o"))
        tracker.record(_make_record(task_type="math", model="claude-opus"))

        results = tracker.get_records(task_type="coding", model_name="claude-opus")
        assert len(results) == 1

    def test_limit_parameter(self):
        tracker = FeedbackTracker()
        for i in range(10):
            tracker.record(_make_record())

        results = tracker.get_records(limit=5)
        assert len(results) == 5

    def test_limit_returns_most_recent(self):
        tracker = FeedbackTracker()
        for i in range(5):
            tracker.record(
                FeedbackRecord(
                    request_id=f"req-{i}",
                    timestamp=datetime.utcnow(),
                    task_type="coding",
                    confidence=0.85,
                    selected_model="claude-opus",
                    benchmark_score=95.0,
                )
            )

        results = tracker.get_records(limit=2)
        assert len(results) == 2
        assert results[0].request_id == "req-3"
        assert results[1].request_id == "req-4"

    def test_no_filters_returns_all(self):
        tracker = FeedbackTracker()
        tracker.record(_make_record())
        tracker.record(_make_record())
        assert len(tracker.get_records()) == 2


class TestPrune:
    def test_prune_removes_old_records(self):
        tracker = FeedbackTracker(retention_hours=1)
        old_time = datetime.utcnow() - timedelta(hours=2)
        tracker.record(_make_record(timestamp=old_time))
        tracker.record(_make_record())

        # After recording the second, prune should have removed the old one
        assert len(tracker.get_records()) == 1

    def test_prune_enforces_max_records(self):
        tracker = FeedbackTracker(max_records=3)
        for i in range(5):
            tracker.record(_make_record())

        assert len(tracker.get_records()) <= 3

    def test_prune_keeps_recent_records(self):
        tracker = FeedbackTracker(retention_hours=24)
        recent = _make_record(timestamp=datetime.utcnow())
        tracker.record(recent)
        assert len(tracker.get_records()) == 1

    def test_max_records_keeps_most_recent(self):
        tracker = FeedbackTracker(max_records=2)
        for i in range(5):
            tracker.record(
                FeedbackRecord(
                    request_id=f"req-{i}",
                    timestamp=datetime.utcnow(),
                    task_type="coding",
                    confidence=0.85,
                    selected_model="claude-opus",
                    benchmark_score=95.0,
                )
            )

        records = tracker.get_records()
        assert len(records) == 2
        # Should keep the most recent
        assert records[-1].request_id == "req-4"
