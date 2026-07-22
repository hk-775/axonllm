"""Tests for reliability hardening (#15): cost-tracker counters (a),
sharded hot-path locks (e), and jittered retry backoff (c)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.gateway.cost_tracker import CostTracker
from src.gateway.models import RateLimitConfig, TokenPricing, UsageRecord
from src.gateway.quota_enforcer import QuotaEnforcer
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.striped_lock import StripedLock


def _rec(project="p1", user="u1", cost=1.0, rid="r") -> UsageRecord:
    return UsageRecord(
        request_id=rid, project_id=project, user_id=user, provider="anthropic",
        model="claude-sonnet", prompt_tokens=1, completion_tokens=1, total_tokens=2,
        cost=cost, timestamp=datetime.now(timezone.utc))


# --- (a) cost-tracker running spend counters ---


class TestSpendCounters:
    async def test_budget_reflects_recorded_spend(self):
        ct = CostTracker(pricing_config={})
        ct.register_project("p1", budget_limit=10.0, alert_threshold=5.0)
        for i in range(3):
            await ct.record_usage(_rec(cost=2.0, rid=f"r{i}"))
        status = await ct.check_budget("p1")
        assert status.current_spend == pytest.approx(6.0)
        assert status.is_alert_triggered is True     # 6 >= 5
        assert status.is_over_budget is False         # 6 < 10

    async def test_user_budget_counter(self):
        ct = CostTracker(pricing_config={})
        ct.register_user("u1", budget_limit=3.0)
        await ct.record_usage(_rec(user="u1", cost=2.5, rid="a"))
        await ct.record_usage(_rec(user="u2", cost=9.0, rid="b"))   # different user
        status = await ct.check_user_budget("u1")
        assert status.current_spend == pytest.approx(2.5)   # not polluted by u2
        assert status.is_over_budget is False

    async def test_trim_does_not_undercount_budget(self):
        """The old code trimmed the oldest 50k records and summed the rest,
        silently dropping their spend. Counters must survive trimming."""
        ct = CostTracker(pricing_config={})
        ct.MAX_RECORDS = 10                          # tiny cap to force trimming
        ct.register_project("p1", budget_limit=1000.0)
        for i in range(25):                          # exceeds cap → trims twice
            await ct.record_usage(_rec(cost=1.0, rid=f"r{i}"))
        assert len(ct._records) <= ct.MAX_RECORDS    # record list was trimmed
        status = await ct.check_budget("p1")
        assert status.current_spend == pytest.approx(25.0)  # ALL spend counted

    async def test_load_records_seeds_counters(self):
        ct = CostTracker(pricing_config={})
        ct.register_project("p1", budget_limit=100.0)
        ct.load_records([_rec(cost=4.0, rid="x"), _rec(cost=6.0, rid="y")])
        assert (await ct.check_budget("p1")).current_spend == pytest.approx(10.0)

    async def test_load_records_dedups(self):
        ct = CostTracker(pricing_config={})
        ct.register_project("p1", budget_limit=100.0)
        ct.load_records([_rec(cost=4.0, rid="dup")])
        ct.load_records([_rec(cost=4.0, rid="dup")])   # same request_id
        assert (await ct.check_budget("p1")).current_spend == pytest.approx(4.0)


# --- (e) striped locks ---


class TestStripedLock:
    async def test_different_keys_run_concurrently(self):
        sl = StripedLock()
        order: list[str] = []

        async def worker(key: str, hold: float):
            async with sl.acquire(key):
                order.append(f"{key}-in")
                await asyncio.sleep(hold)
                order.append(f"{key}-out")

        # If keys serialized, "a-out" would precede "b-in". With per-key locks
        # they interleave.
        await asyncio.gather(worker("a", 0.05), worker("b", 0.05))
        assert order.index("b-in") < order.index("a-out")

    async def test_same_key_serializes(self):
        sl = StripedLock()
        events: list[str] = []

        async def worker(tag: str):
            async with sl.acquire("shared"):
                events.append(f"{tag}-in")
                await asyncio.sleep(0.02)
                events.append(f"{tag}-out")

        await asyncio.gather(worker("x"), worker("y"))
        # Each critical section completes before the next starts.
        assert events[0].endswith("-in") and events[1].endswith("-out")
        assert events[2].endswith("-in") and events[3].endswith("-out")

    async def test_multi_is_deadlock_free_under_reverse_order(self):
        sl = StripedLock()

        async def ab():
            async with sl.multi("a", "b"):
                await asyncio.sleep(0.01)

        async def ba():
            async with sl.multi("b", "a"):   # reverse request order
                await asyncio.sleep(0.01)

        # multi() sorts keys, so these can't deadlock.
        await asyncio.wait_for(asyncio.gather(ab(), ba()), timeout=2.0)

    async def test_multi_collapses_duplicate_keys(self):
        sl = StripedLock()
        async with sl.multi("k", "k"):        # must not self-deadlock
            pass


class TestRateLimiterConcurrency:
    async def test_concurrent_same_project_counts_all(self):
        # Under one shared project, concurrent requests must all be counted
        # (no lost updates from racing on the project bucket).
        rl = SlidingWindowRateLimiter(RateLimitConfig(
            user_rpm=1000, project_rpm=1000, window_seconds=60))
        results = await asyncio.gather(*[
            rl.check_rate_limit(f"user{i}", "shared-proj") for i in range(50)])
        assert all(r.allowed for r in results)
        assert len(rl._project_requests["shared-proj"]) == 50   # no lost updates

    async def test_enforces_limit(self):
        rl = SlidingWindowRateLimiter(RateLimitConfig(
            user_rpm=5, project_rpm=1000, window_seconds=60))
        results = await asyncio.gather(*[
            rl.check_rate_limit("u1", "p1") for _ in range(8)])
        allowed = [r for r in results if r.allowed]
        assert len(allowed) == 5                                # user_rpm cap holds


class TestQuotaEnforcerConcurrency:
    async def test_concurrent_spend_no_lost_updates(self):
        qe = QuotaEnforcer()
        await asyncio.gather(*[
            qe.record_spend("p1", 1.0) for _ in range(100)])
        assert qe.get_spend("p1") == pytest.approx(100.0)

    async def test_different_projects_independent(self):
        qe = QuotaEnforcer()
        await asyncio.gather(
            *[qe.record_spend("p1", 1.0) for _ in range(10)],
            *[qe.record_spend("p2", 2.0) for _ in range(10)])
        assert qe.get_spend("p1") == pytest.approx(10.0)
        assert qe.get_spend("p2") == pytest.approx(20.0)


# --- (c) jittered backoff ---


class TestBackoffJitter:
    def _router(self, jitter: float):
        from unittest.mock import MagicMock

        from src.gateway.config import RetryConfig
        from src.gateway.router import Router

        r = Router.__new__(Router)      # bypass full wiring; set only what we need
        r.base_delay = 1.0
        r._jitter = jitter
        return r

    def test_no_jitter_is_deterministic_exponential(self):
        r = self._router(0.0)
        assert r._backoff_delay(0) == 1.0
        assert r._backoff_delay(1) == 2.0
        assert r._backoff_delay(2) == 4.0

    def test_jitter_bounded_and_varies(self):
        r = self._router(0.5)
        samples = [r._backoff_delay(2) for _ in range(50)]   # full = 4.0
        assert all(2.0 <= s <= 4.0 for s in samples)         # [(1-j)*full, full]
        assert len(set(samples)) > 1                          # actually randomized

    def test_jitter_grows_with_attempt(self):
        r = self._router(0.5)
        lo0 = min(r._backoff_delay(0) for _ in range(50))
        hi3 = max(r._backoff_delay(3) for _ in range(50))
        assert hi3 > lo0
