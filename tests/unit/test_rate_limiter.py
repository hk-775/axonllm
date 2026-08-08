"""Unit tests for SlidingWindowRateLimiter."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.gateway.models import Project, RateLimitConfig
from src.gateway.rate_limiter import SlidingWindowRateLimiter
from tests.unit.shared_enforcement_backend import SharedEnforcementBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_limiter(
    user_rpm=5,
    project_rpm=10,
    window_seconds=60,
    persistence=None,
):
    config = RateLimitConfig(
        user_rpm=user_rpm,
        project_rpm=project_rpm,
        window_seconds=window_seconds,
    )
    return SlidingWindowRateLimiter(config, persistence=persistence)


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
        now = datetime.now(timezone.utc)
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.reset_at >= now

    @pytest.mark.asyncio
    async def test_reset_at_within_window(self):
        limiter = _make_limiter(window_seconds=60)
        now = datetime.now(timezone.utc)
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
        old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        limiter._user_requests["user-1"] = [old_time, old_time]
        limiter._project_requests["proj-1"] = [old_time, old_time]
        # These old requests should be cleaned up, so new request is allowed
        result = await limiter.check_rate_limit("user-1", "proj-1")
        assert result.allowed is True


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_same_user_and_project_ids_do_not_share_local_capacity(self):
        limiter = _make_limiter(user_rpm=1, project_rpm=1)

        first = await limiter.check_rate_limit(
            "same-user",
            "same-project",
            tenant_id="tenant-a",
        )
        blocked = await limiter.check_rate_limit(
            "same-user",
            "same-project",
            tenant_id="tenant-a",
        )
        other_tenant = await limiter.check_rate_limit(
            "same-user",
            "same-project",
            tenant_id="tenant-b",
        )

        assert first.allowed is True
        assert blocked.allowed is False
        assert other_tenant.allowed is True

    @pytest.mark.asyncio
    async def test_empty_tenant_id_cannot_fall_back_to_legacy_state(self):
        limiter = _make_limiter()

        with pytest.raises(ValueError, match="tenant_id"):
            await limiter.check_rate_limit("user", "project", tenant_id="")

    @pytest.mark.asyncio
    async def test_canonical_project_tenant_must_match_request_scope(self):
        limiter = _make_limiter()
        project = Project(
            project_id="project",
            name="Project",
            tenant_id="tenant-a",
        )

        with pytest.raises(ValueError, match="tenant_id"):
            await limiter.check_rate_limit(
                "user",
                "project",
                tenant_id="tenant-b",
                project=project,
            )


class TestCanonicalProjectRateLimit:
    @pytest.mark.asyncio
    async def test_project_rpm_replaces_static_project_default(self):
        backend = SharedEnforcementBackend()
        limiter = _make_limiter(
            user_rpm=100,
            project_rpm=100,
            persistence=backend,
        )
        project = Project(
            project_id="project",
            name="Project",
            tenant_id="tenant-a",
            rate_limit_rpm=2,
        )

        assert (
            await limiter.check_rate_limit("u1", "project", project=project)
        ).allowed
        assert (
            await limiter.check_rate_limit("u2", "project", project=project)
        ).allowed
        denied = await limiter.check_rate_limit(
            "u3",
            "project",
            project=project,
        )

        assert denied.allowed is False
        assert denied.limit == 2
        assert backend.rate_calls[-1]["tenant_id"] == "tenant-a"
        assert backend.rate_calls[-1]["project_limit"] == 2


class TestSharedFixedWindow:
    @pytest.mark.asyncio
    async def test_multiple_instances_enforce_one_project_limit(self):
        backend = SharedEnforcementBackend()
        first = _make_limiter(
            user_rpm=100,
            project_rpm=5,
            persistence=backend,
        )
        second = _make_limiter(
            user_rpm=100,
            project_rpm=5,
            persistence=backend,
        )

        results = await asyncio.gather(*(
            (first if index % 2 else second).check_rate_limit(
                f"user-{index}",
                "project",
                tenant_id="tenant-a",
            )
            for index in range(20)
        ))

        assert sum(result.allowed for result in results) == 5

    @pytest.mark.asyncio
    async def test_shared_capacity_is_isolated_for_identical_tenant_ids(self):
        backend = SharedEnforcementBackend()
        first = _make_limiter(
            user_rpm=10,
            project_rpm=1,
            persistence=backend,
        )
        second = _make_limiter(
            user_rpm=10,
            project_rpm=1,
            persistence=backend,
        )

        assert (
            await first.check_rate_limit(
                "user",
                "project",
                tenant_id="tenant-a",
            )
        ).allowed
        assert not (
            await second.check_rate_limit(
                "other-user",
                "project",
                tenant_id="tenant-a",
            )
        ).allowed
        assert (
            await second.check_rate_limit(
                "user",
                "project",
                tenant_id="tenant-b",
            )
        ).allowed

    @pytest.mark.asyncio
    async def test_user_limit_is_shared_across_instances_and_projects(self):
        backend = SharedEnforcementBackend()
        first = _make_limiter(
            user_rpm=2,
            project_rpm=100,
            persistence=backend,
        )
        second = _make_limiter(
            user_rpm=2,
            project_rpm=100,
            persistence=backend,
        )

        assert (
            await first.check_rate_limit(
                "same-user",
                "project-a",
                tenant_id="tenant-a",
            )
        ).allowed
        assert (
            await second.check_rate_limit(
                "same-user",
                "project-b",
                tenant_id="tenant-a",
            )
        ).allowed
        assert not (
            await first.check_rate_limit(
                "same-user",
                "project-c",
                tenant_id="tenant-a",
            )
        ).allowed

    @pytest.mark.asyncio
    async def test_denied_project_does_not_partially_consume_user_capacity(self):
        backend = SharedEnforcementBackend()
        limiter = _make_limiter(
            user_rpm=1,
            project_rpm=1,
            persistence=backend,
        )

        assert (
            await limiter.check_rate_limit(
                "first-user",
                "full-project",
                tenant_id="tenant-a",
            )
        ).allowed
        assert not (
            await limiter.check_rate_limit(
                "fresh-user",
                "full-project",
                tenant_id="tenant-a",
            )
        ).allowed
        assert (
            await limiter.check_rate_limit(
                "fresh-user",
                "fresh-project",
                tenant_id="tenant-a",
            )
        ).allowed

    @pytest.mark.asyncio
    async def test_unavailable_shared_backend_fails_closed(self):
        backend = SharedEnforcementBackend()
        backend.rate_available = False
        limiter = _make_limiter(persistence=backend)

        result = await limiter.check_rate_limit(
            "user",
            "project",
            tenant_id="tenant-a",
        )

        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after_seconds is not None

    @pytest.mark.asyncio
    async def test_shared_backend_exception_fails_closed(self):
        backend = SharedEnforcementBackend()
        backend.rate_error = RuntimeError("table unavailable")
        limiter = _make_limiter(persistence=backend)

        result = await limiter.check_rate_limit("user", "project")

        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_malformed_shared_decision_fails_closed(self):
        class MalformedBackend:
            enabled = True

            async def consume_rate_limit_window(self, **kwargs):
                return {"allowed": True}

        limiter = _make_limiter(persistence=MalformedBackend())

        result = await limiter.check_rate_limit("user", "project")

        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_missing_shared_method_fails_closed(self):
        class MissingSharedMethod:
            enabled = True

        limiter = _make_limiter(persistence=MissingSharedMethod())

        result = await limiter.check_rate_limit("user", "project")

        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_disabled_persistence_keeps_explicit_local_mode(self):
        class DisabledPersistence:
            enabled = False

        limiter = _make_limiter(
            user_rpm=1,
            persistence=DisabledPersistence(),
        )

        assert (await limiter.check_rate_limit("user", "project")).allowed
        assert not (await limiter.check_rate_limit("user", "project")).allowed
