"""Unit tests for ProviderHealthTracker."""

import time

from src.gateway.health_tracker import ProviderHealthTracker


class TestMarkUnhealthyAndIsHealthy:
    def test_unknown_provider_is_healthy(self):
        tracker = ProviderHealthTracker()
        assert tracker.is_healthy("openai") is True

    def test_marked_unhealthy_during_cooldown(self, monkeypatch):
        tracker = ProviderHealthTracker()
        now = 1000.0
        monkeypatch.setattr(time, "time", lambda: now)

        tracker.mark_unhealthy("openai", cooldown_seconds=60)
        # Still within cooldown
        monkeypatch.setattr(time, "time", lambda: now + 30)
        assert tracker.is_healthy("openai") is False

    def test_healthy_after_cooldown_expires(self, monkeypatch):
        tracker = ProviderHealthTracker()
        now = 1000.0
        monkeypatch.setattr(time, "time", lambda: now)

        tracker.mark_unhealthy("openai", cooldown_seconds=60)
        # Cooldown expired
        monkeypatch.setattr(time, "time", lambda: now + 60)
        assert tracker.is_healthy("openai") is True

    def test_multiple_providers_independent(self, monkeypatch):
        tracker = ProviderHealthTracker()
        now = 1000.0
        monkeypatch.setattr(time, "time", lambda: now)

        tracker.mark_unhealthy("openai", cooldown_seconds=30)
        tracker.mark_unhealthy("anthropic", cooldown_seconds=90)

        monkeypatch.setattr(time, "time", lambda: now + 50)
        assert tracker.is_healthy("openai") is True
        assert tracker.is_healthy("anthropic") is False

    def test_re_mark_unhealthy_resets_cooldown(self, monkeypatch):
        tracker = ProviderHealthTracker()
        now = 1000.0
        monkeypatch.setattr(time, "time", lambda: now)

        tracker.mark_unhealthy("openai", cooldown_seconds=30)
        # Re-mark with longer cooldown
        monkeypatch.setattr(time, "time", lambda: now + 10)
        tracker.mark_unhealthy("openai", cooldown_seconds=60)

        # Original cooldown would have expired, but new one hasn't
        monkeypatch.setattr(time, "time", lambda: now + 40)
        assert tracker.is_healthy("openai") is False


class TestRecordAndGetAverageLatency:
    def test_no_records_returns_inf(self):
        tracker = ProviderHealthTracker()
        assert tracker.get_average_latency("openai", window_seconds=60) == float("inf")

    def test_single_record(self, monkeypatch):
        tracker = ProviderHealthTracker()
        monkeypatch.setattr(time, "time", lambda: 1000.0)

        tracker.record_latency("openai", 150.0)
        assert tracker.get_average_latency("openai", window_seconds=60) == 150.0

    def test_average_of_multiple_records(self, monkeypatch):
        tracker = ProviderHealthTracker()
        monkeypatch.setattr(time, "time", lambda: 1000.0)

        tracker.record_latency("openai", 100.0)
        tracker.record_latency("openai", 200.0)
        tracker.record_latency("openai", 300.0)

        assert tracker.get_average_latency("openai", window_seconds=60) == 200.0

    def test_sliding_window_excludes_old_records(self, monkeypatch):
        tracker = ProviderHealthTracker()

        # Record at t=1000
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        tracker.record_latency("openai", 500.0)

        # Record at t=1050
        monkeypatch.setattr(time, "time", lambda: 1050.0)
        tracker.record_latency("openai", 100.0)

        # Query at t=1050 with 30s window — only the second record is in window
        avg = tracker.get_average_latency("openai", window_seconds=30)
        assert avg == 100.0

    def test_all_records_outside_window_returns_inf(self, monkeypatch):
        tracker = ProviderHealthTracker()

        monkeypatch.setattr(time, "time", lambda: 1000.0)
        tracker.record_latency("openai", 200.0)

        monkeypatch.setattr(time, "time", lambda: 1200.0)
        assert tracker.get_average_latency("openai", window_seconds=60) == float("inf")

    def test_latencies_independent_per_provider(self, monkeypatch):
        tracker = ProviderHealthTracker()
        monkeypatch.setattr(time, "time", lambda: 1000.0)

        tracker.record_latency("openai", 100.0)
        tracker.record_latency("anthropic", 300.0)

        assert tracker.get_average_latency("openai", window_seconds=60) == 100.0
        assert tracker.get_average_latency("anthropic", window_seconds=60) == 300.0
