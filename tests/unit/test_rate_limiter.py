"""Unit tests for SlidingWindowRateLimiter."""

import pytest
from datetime import datetime, timedelta

from src.gateway.models import RateLimitConfig
from src.gateway.rate_limiter import SlidingWindowRateLimiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_limiter(user_rpm=5, project_rpm=10, window_seconds=60):
    config = RateLimitConfig(
        user_rpm=user_rpm,
        project_rpm=project_rpm,
        window_seconds=window_seconds,
    )
    return SlidingWindowRateLimiter(config)


# ---------------------------------------------------------------------------
# Basic allow within limits
# ---------------------------------------------------------------------------

class TestBasicAllow:
    @pytest.mark.asyncio
    async def test_first_request_allowed(self):
        limiter = _make_limiter()
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_multiple_requests_within_limit(self):
        limiter = _make_limiter(user_rpm=5, project_rpm=10)
        for _ in range(5):
            result = await limiter.check_rate_limit("user-1", "proj-1")
        # First 5 should all be allowed (user_rpm=5)
        # The 5th call uses the last slot
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_retry_after_is_none_when_allowed(self):
        limiter = _make_limiter()
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.retry_after_seconds is None


# ---------------------------------------------------------------------------
# Reject when user limit exceeded
# ---------------------------------------------------------------------------

class TestUserLimitExceeded:
    @pytest.mark.asyncio
    async def test_reject_after_user_limit(self):
        limiter = _make_limiter(user_rpm=3, project_rpm=100)
        for _ in range(3):
            result = await limiter.check_rate_limit("user-1", "proj-1")
            assert result.allowed is True
        # 4th request should be denied
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_rejected_request_has_retry_after(self):
        limiter = _make_limiter(user_rpm=1, project_rpm=100)
        await limiter.check_rate_limit("user-1", "proj-1")
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is False
        assert result.retry_after_seconds is not None
        assert result.retry_after_seconds > 0


# ---------------------------------------------------------------------------
# Reject when project limit exceeded
# ---------------------------------------------------------------------------

class TestProjectLimitExceeded:
    @pytest.mark.asyncio
    async def test_reject_after_project_limit(self):
        limiter = _make_limiter(user_rpm=100, project_rpm=3)
        # Use different users so user limit isn't hit
        for i in range(3):
            result = await limiter.check_rate_limit(f"user-{i}", "proj-1")
            assert result.allowed is True
        # 4th request from a new user should be denied (project limit)
        result = await limiter.check_rate_limit("user-99", "proj-1")
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_project_rejected_has_retry_after(self):
        limiter = _make_limiter(user_rpm=100, project_rpm=1)
        await limiter.check_rate_limit("user-1", "proj-1")
        result = await limiter.check_rate_limit("user-2", "proj-1")
        assert result.allowed is False
        assert result.retry_after_seconds is not None
        assert result.retry_after_seconds > 0


# ---------------------------------------------------------------------------
# Independent user and project limits
# ---------------------------------------------------------------------------

class TestIndependentLimits:
    @pytest.mark.asyncio
    async def test_different_users_have_separate_limits(self):
        limiter = _make_limiter(user_rpm=2, project_rpm=100)
        # user-1 uses 2 slots
        await limiter.check_rate_limit("user-1", "proj-1")
        await limiter.check_rate_limit("user-1", "proj-1")
        # user-1 is now at limit
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is False
        # user-2 should still be allowed
        result = await limiter.check_rate_limit("user-2", "proj-1")
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_different_projects_have_separate_limits(self):
        limiter = _make_limiter(user_rpm=100, project_rpm=2)
        # proj-1 uses 2 slots (different users)
        await limiter.check_rate_limit("user-1", "proj-1")
        await limiter.check_rate_limit("user-2", "proj-1")
        # proj-1 is now at limit
        result = await limiter.check_rate_limit("user-3", "proj-1")
        assert result.allowed is False
        # proj-2 should still be allowed
        result = await limiter.check_rate_limit("user-1", "proj-2")
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_user_limit_hit_while_project_has_capacity(self):
        limiter = _make_limiter(user_rpm=1, project_rpm=100)
        await limiter.check_rate_limit("user-1", "proj-1")
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_project_limit_hit_while_user_has_capacity(self):
        limiter = _make_limiter(user_rpm=100, project_rpm=1)
        await limiter.check_rate_limit("user-1", "proj-1")
        # New user, but project is full
        result = await limiter.check_rate_limit("user-2", "proj-1")
        assert result.allowed is False


# ---------------------------------------------------------------------------
# Remaining count accuracy
# ---------------------------------------------------------------------------

class TestRemainingCount:
    @pytest.mark.asyncio
    async def test_remaining_decreases_with_each_request(self):
        limiter = _make_limiter(user_rpm=5, project_rpm=100)
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.remaining == 4  # user_rpm(5) - 1 = 4 (user is more restrictive)

        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.remaining == 3

    @pytest.mark.asyncio
    async def test_remaining_is_zero_when_denied(self):
        limiter = _make_limiter(user_rpm=1, project_rpm=100)
        await limiter.check_rate_limit("user-1", "proj-1")
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is False
        assert result.remaining == 0

    @pytest.mark.asyncio
    async def test_remaining_reflects_more_restrictive_limit(self):
        # user_rpm=10, project_rpm=3 — project is more restrictive
        limiter = _make_limiter(user_rpm=10, project_rpm=3)
        result = await limiter.check_rate_limit("user-1", "proj-1")
        # After 1 request: user_remaining=9, project_remaining=2 → min=2
        assert result.remaining == 2

    @pytest.mark.asyncio
    async def test_limit_field_reflects_more_restrictive(self):
        limiter = _make_limiter(user_rpm=10, project_rpm=3)
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.limit == 3  # project is more restrictive


# ---------------------------------------------------------------------------
# Reset time calculation
# ---------------------------------------------------------------------------

class TestResetTime:
    @pytest.mark.asyncio
    async def test_reset_at_is_in_the_future(self):
        limiter = _make_limiter()
        now = datetime.utcnow()
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.reset_at >= now

    @pytest.mark.asyncio
    async def test_reset_at_within_window(self):
        limiter = _make_limiter(window_seconds=60)
        now = datetime.utcnow()
        result = await limiter.check_rate_limit("user-1", "proj-1")
        # reset_at should be at most window_seconds from now
        assert result.reset_at <= now + timedelta(seconds=61)


# ---------------------------------------------------------------------------
# Retry-after when denied
# ---------------------------------------------------------------------------

class TestRetryAfter:
    @pytest.mark.asyncio
    async def test_retry_after_positive_when_denied(self):
        limiter = _make_limiter(user_rpm=1, project_rpm=100)
        await limiter.check_rate_limit("user-1", "proj-1")
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is False
        assert result.retry_after_seconds is not None
        assert result.retry_after_seconds >= 1

    @pytest.mark.asyncio
    async def test_retry_after_at_most_window_seconds(self):
        limiter = _make_limiter(user_rpm=1, project_rpm=100, window_seconds=60)
        await limiter.check_rate_limit("user-1", "proj-1")
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.retry_after_seconds <= 61

    @pytest.mark.asyncio
    async def test_sliding_window_old_requests_expire(self):
        """Requests outside the window should not count."""
        limiter = _make_limiter(user_rpm=2, project_rpm=100, window_seconds=60)
        # Manually inject old timestamps
        old_time = datetime.utcnow() - timedelta(seconds=120)
        limiter._user_requests["user-1"] = [old_time, old_time]
        limiter._project_requests["proj-1"] = [old_time, old_time]
        # These old requests should be cleaned up, so new request is allowed
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is True
