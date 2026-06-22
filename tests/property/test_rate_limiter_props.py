# Feature: litellm-service, Properties 23-25: SlidingWindowRateLimiter property tests
"""Property-based tests for the SlidingWindowRateLimiter component.

Properties covered:
  23 – Rate limiter rejects over-limit requests with correct metadata
  24 – Rate limits enforce independently at user and project levels
  25 – Sliding window prevents burst at boundaries
"""

import asyncio
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.rate_limiter import SlidingWindowRateLimiter
from src.gateway.models import RateLimitConfig


# ---------------------------------------------------------------------------
# Shared helpers and strategies
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# RPM values kept small so we can exceed them within a single test run
rpm_strategy = st.integers(min_value=1, max_value=20)
window_strategy = st.integers(min_value=10, max_value=120)

user_id_strategy = st.sampled_from(["user-a", "user-b", "user-c", "user-d"])
project_id_strategy = st.sampled_from(["proj-1", "proj-2", "proj-3", "proj-4"])


# ===========================================================================
# Property 23: Rate limiter rejects over-limit requests with correct metadata
# Feature: litellm-service, Property 23: Rate limit rejection metadata
# ===========================================================================


@given(
    user_rpm=rpm_strategy,
    project_rpm=rpm_strategy,
    window_seconds=window_strategy,
    extra_requests=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_rate_limit_rejection_metadata(user_rpm, project_rpm, window_seconds, extra_requests):
    """Property 23: Rate limiter rejects over-limit requests with correct metadata.

    For any rate limit config and any sequence of requests exceeding RPM,
    excess requests are rejected with retry_after, limit, remaining=0, and reset_at.

    **Validates: Requirements 9.1, 9.3**
    """
    config = RateLimitConfig(
        user_rpm=user_rpm,
        project_rpm=project_rpm,
        window_seconds=window_seconds,
    )
    limiter = SlidingWindowRateLimiter(config)

    effective_limit = min(user_rpm, project_rpm)

    # Send exactly effective_limit requests — all should be allowed
    for i in range(effective_limit):
        result = _run(limiter.check_rate_limit("user-x", "proj-x"))
        assert result.allowed is True, (
            f"Request {i+1}/{effective_limit} should be allowed"
        )

    # Send extra_requests beyond the limit — all should be rejected
    for i in range(extra_requests):
        result = _run(limiter.check_rate_limit("user-x", "proj-x"))

        # Must be rejected
        assert result.allowed is False, (
            f"Over-limit request {i+1} should be rejected"
        )

        # remaining must be 0 when denied
        assert result.remaining == 0, (
            f"Remaining should be 0 when denied, got {result.remaining}"
        )

        # retry_after_seconds must be set and positive
        assert result.retry_after_seconds is not None, (
            "retry_after_seconds must be set when denied"
        )
        assert result.retry_after_seconds > 0, (
            f"retry_after_seconds must be positive, got {result.retry_after_seconds}"
        )

        # limit field must reflect the effective limit
        assert result.limit == effective_limit, (
            f"limit should be {effective_limit}, got {result.limit}"
        )

        # reset_at must be in the future (or very close to now)
        assert result.reset_at is not None, "reset_at must be set"


# ===========================================================================
# Property 24: Rate limits enforce independently at user and project levels
# Feature: litellm-service, Property 24: Independent user and project limits
# ===========================================================================


@given(
    user_rpm=st.integers(min_value=1, max_value=10),
    project_rpm=st.integers(min_value=1, max_value=10),
    window_seconds=window_strategy,
)
@settings(max_examples=100)
def test_independent_user_and_project_limits(user_rpm, project_rpm, window_seconds):
    """Property 24: Rate limits enforce independently at user and project levels.

    For any user-level and project-level config, exceeding either limit
    independently triggers rejection even if the other has capacity.

    **Validates: Requirements 9.2**
    """
    # Ensure limits differ so we can test independence
    assume(user_rpm != project_rpm)

    config = RateLimitConfig(
        user_rpm=user_rpm,
        project_rpm=project_rpm,
        window_seconds=window_seconds,
    )

    # --- Test 1: User limit hit while project has capacity ---
    if user_rpm < project_rpm:
        limiter = SlidingWindowRateLimiter(config)

        # Exhaust user limit (each request from same user, same project)
        for _ in range(user_rpm):
            result = _run(limiter.check_rate_limit("user-solo", "proj-big"))
            assert result.allowed is True

        # Next request from same user should be rejected (user limit hit)
        result = _run(limiter.check_rate_limit("user-solo", "proj-big"))
        assert result.allowed is False, (
            "User limit exceeded — request should be rejected even though project has capacity"
        )

        # But a DIFFERENT user on the same project should still be allowed
        result = _run(limiter.check_rate_limit("user-other", "proj-big"))
        assert result.allowed is True, (
            "Different user should be allowed — user limits are independent"
        )

    # --- Test 2: Project limit hit while user has capacity ---
    if project_rpm < user_rpm:
        limiter = SlidingWindowRateLimiter(config)

        # Exhaust project limit using different users (so no single user hits their limit)
        users = [f"user-{i}" for i in range(project_rpm)]
        for u in users:
            result = _run(limiter.check_rate_limit(u, "proj-small"))
            assert result.allowed is True

        # Next request on same project should be rejected (project limit hit)
        # Use a fresh user who hasn't made any requests
        result = _run(limiter.check_rate_limit("user-fresh", "proj-small"))
        assert result.allowed is False, (
            "Project limit exceeded — request should be rejected even though user has capacity"
        )

        # But the same user on a DIFFERENT project should still be allowed
        result = _run(limiter.check_rate_limit("user-fresh", "proj-other"))
        assert result.allowed is True, (
            "Different project should be allowed — project limits are independent"
        )


# ===========================================================================
# Property 25: Sliding window prevents burst at boundaries
# Feature: litellm-service, Property 25: Sliding window boundary behavior
# ===========================================================================


@given(
    rpm=st.integers(min_value=3, max_value=15),
    window_seconds=st.integers(min_value=20, max_value=120),
    first_batch_fraction=st.floats(min_value=0.3, max_value=0.7, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_sliding_window_boundary_behavior(rpm, window_seconds, first_batch_fraction):
    """Property 25: Sliding window prevents burst at boundaries.

    Requests spanning two adjacent fixed windows but exceeding the sliding
    window limit are correctly rejected. A fixed-window algorithm would
    reset at the boundary and allow all, but the sliding window counts
    requests across the boundary.

    **Validates: Requirements 9.4**
    """
    config = RateLimitConfig(
        user_rpm=rpm,
        project_rpm=rpm * 10,  # high project limit so only user limit matters
        window_seconds=window_seconds,
    )
    limiter = SlidingWindowRateLimiter(config)

    # Split requests: first_batch in "window 1", second_batch in "window 2"
    first_batch = max(1, int(rpm * first_batch_fraction))
    second_batch = rpm - first_batch + 1  # total > rpm across the boundary
    assume(first_batch >= 1)
    assume(second_batch >= 1)
    assume(first_batch + second_batch > rpm)

    now = datetime.now(timezone.utc)

    # Place first_batch requests near the end of "window 1"
    # These are within [now - window_seconds, now], placed at now - small_offset
    offset_first = window_seconds * 0.8  # 80% into the window from the past
    for i in range(first_batch):
        ts = now - timedelta(seconds=offset_first - i * 0.1)
        limiter._user_requests.setdefault("user-boundary", []).append(ts)
        limiter._project_requests.setdefault("proj-boundary", []).append(ts)

    # Place second_batch - 1 requests near the start of "window 2"
    # These are recent, close to now
    offset_second = 2.0  # 2 seconds ago
    for i in range(second_batch - 1):
        ts = now - timedelta(seconds=offset_second - i * 0.1)
        limiter._user_requests.setdefault("user-boundary", []).append(ts)
        limiter._project_requests.setdefault("proj-boundary", []).append(ts)

    # At this point we have first_batch + (second_batch - 1) = rpm timestamps
    # All within the sliding window. The next request should be rejected.
    total_injected = first_batch + (second_batch - 1)
    assert total_injected == rpm, (
        f"Expected {rpm} injected timestamps, got {total_injected}"
    )

    # The sliding window should see all rpm requests and reject the next one
    result = _run(limiter.check_rate_limit("user-boundary", "proj-boundary"))
    assert result.allowed is False, (
        f"Sliding window should reject: {total_injected} requests already in window "
        f"(limit={rpm}). A fixed-window algorithm might incorrectly allow this."
    )
    assert result.remaining == 0
    assert result.retry_after_seconds is not None
    assert result.retry_after_seconds > 0
