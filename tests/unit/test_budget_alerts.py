"""Tests for budget threshold alerting."""

import asyncio

import pytest

from src.gateway.quota_enforcer import QuotaEnforcer


def _run(coro):
    return asyncio.run(coro)


class TestBudgetAlerts:
    def test_fires_at_80_percent(self):
        enforcer = QuotaEnforcer()
        alerts = []
        enforcer.on_budget_alert(lambda pid, thr, spend, limit: alerts.append((pid, thr)))

        _run(enforcer.record_spend("proj-1", 79.0, budget_limit=100.0))
        assert len(alerts) == 0

        _run(enforcer.record_spend("proj-1", 2.0, budget_limit=100.0))
        assert (("proj-1", 0.8)) in alerts

    def test_fires_at_90_and_100(self):
        enforcer = QuotaEnforcer()
        alerts = []
        enforcer.on_budget_alert(lambda pid, thr, spend, limit: alerts.append(thr))

        _run(enforcer.record_spend("proj-1", 89.0, budget_limit=100.0))
        _run(enforcer.record_spend("proj-1", 2.0, budget_limit=100.0))
        assert 0.9 in alerts
        assert 0.8 in alerts

        _run(enforcer.record_spend("proj-1", 10.0, budget_limit=100.0))
        assert 1.0 in alerts

    def test_no_duplicate_alerts(self):
        enforcer = QuotaEnforcer()
        alerts = []
        enforcer.on_budget_alert(lambda pid, thr, spend, limit: alerts.append(thr))

        _run(enforcer.record_spend("proj-1", 85.0, budget_limit=100.0))
        _run(enforcer.record_spend("proj-1", 5.0, budget_limit=100.0))
        count_80 = alerts.count(0.8)
        assert count_80 == 1

    def test_reset_clears_thresholds(self):
        enforcer = QuotaEnforcer()
        alerts = []
        enforcer.on_budget_alert(lambda pid, thr, spend, limit: alerts.append(thr))

        _run(enforcer.record_spend("proj-1", 85.0, budget_limit=100.0))
        assert 0.8 in alerts

        alerts.clear()
        enforcer.reset_spend("proj-1")
        _run(enforcer.record_spend("proj-1", 85.0, budget_limit=100.0))
        assert 0.8 in alerts

    def test_no_alert_without_budget_limit(self):
        enforcer = QuotaEnforcer()
        alerts = []
        enforcer.on_budget_alert(lambda pid, thr, spend, limit: alerts.append(thr))

        _run(enforcer.record_spend("proj-1", 1000.0))
        assert len(alerts) == 0

    def test_no_alert_without_callbacks(self):
        enforcer = QuotaEnforcer()
        _run(enforcer.record_spend("proj-1", 100.0, budget_limit=50.0))
