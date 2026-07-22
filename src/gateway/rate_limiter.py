"""Sliding window rate limiter for user-level and project-level limits."""

from datetime import datetime, timedelta, timezone

from src.gateway.models import RateLimitConfig, RateLimitResult
from src.gateway.striped_lock import StripedLock


class SlidingWindowRateLimiter:
    """Sliding window rate limiter.

    Prevents burst traffic at window boundaries by using a sliding window
    rather than fixed time buckets. Tracks individual request timestamps
    and counts requests within [now - window_seconds, now].

    Checks both user-level and project-level limits independently.
    """

    def __init__(self, config: RateLimitConfig):
        self.config = config
        # Separate timestamp stores: key -> list of datetime
        self._user_requests: dict[str, list[datetime]] = {}
        self._project_requests: dict[str, list[datetime]] = {}
        # Per-key locks instead of one global lock. A check touches both a user
        # bucket and a project bucket, so we lock BOTH keys (namespaced so a user
        # id can't collide with a project id) via multi() in canonical order —
        # different (user, project) pairs proceed concurrently, deadlock-free.
        self._locks = StripedLock()

    def _cleanup(self, timestamps: list[datetime], cutoff: datetime) -> list[datetime]:
        """Remove timestamps older than the cutoff."""
        return [ts for ts in timestamps if ts > cutoff]

    async def check_rate_limit(
        self, user_id: str, project_id: str
    ) -> RateLimitResult:
        """Check if request is within rate limits.

        Checks both user-level and project-level limits.
        Returns allow/deny with limit, remaining, and reset metadata.
        """
        async with self._locks.multi(f"u:{user_id}", f"p:{project_id}"):
            now = datetime.now(timezone.utc)
            window = timedelta(seconds=self.config.window_seconds)
            cutoff = now - window

            # Clean up and count user requests in the window
            user_ts = self._user_requests.get(user_id, [])
            user_ts = self._cleanup(user_ts, cutoff)
            if user_ts:
                self._user_requests[user_id] = user_ts
            else:
                self._user_requests.pop(user_id, None)
            user_count = len(user_ts)
            user_remaining = max(0, self.config.user_rpm - user_count)

            # Clean up and count project requests in the window
            project_ts = self._project_requests.get(project_id, [])
            project_ts = self._cleanup(project_ts, cutoff)
            if project_ts:
                self._project_requests[project_id] = project_ts
            else:
                self._project_requests.pop(project_id, None)
            project_count = len(project_ts)
            project_remaining = max(0, self.config.project_rpm - project_count)

            # Determine if either limit is exceeded
            user_exceeded = user_count >= self.config.user_rpm
            project_exceeded = project_count >= self.config.project_rpm
            allowed = not user_exceeded and not project_exceeded

            if allowed:
                # Record the request timestamp for both user and project
                self._user_requests.setdefault(user_id, []).append(now)
                self._project_requests.setdefault(project_id, []).append(now)
                # After recording, remaining decreases by 1
                user_remaining = max(0, self.config.user_rpm - (user_count + 1))
                project_remaining = max(0, self.config.project_rpm - (project_count + 1))

            # Pick the more restrictive result
            remaining = min(user_remaining, project_remaining)

            # Determine which limit applies (more restrictive)
            if user_remaining <= project_remaining:
                limit = self.config.user_rpm
            else:
                limit = self.config.project_rpm

            # Calculate reset_at: when the oldest request in the window expires
            oldest_user = user_ts[0] if user_ts else now
            oldest_project = project_ts[0] if project_ts else now
            # Use the earliest expiry among the two
            oldest = min(oldest_user, oldest_project)
            reset_at = oldest + window

            # retry_after_seconds only when denied
            retry_after_seconds = None
            if not allowed:
                # Find the oldest timestamp from the exceeded limit(s)
                candidates = []
                if user_exceeded and user_ts:
                    candidates.append(user_ts[0])
                if project_exceeded and project_ts:
                    candidates.append(project_ts[0])
                if candidates:
                    earliest_expiry = min(candidates) + window
                    retry_after_seconds = max(1, int((earliest_expiry - now).total_seconds() + 0.999))

            return RateLimitResult(
                allowed=allowed,
                limit=limit,
                remaining=remaining,
                reset_at=reset_at,
                retry_after_seconds=retry_after_seconds,
            )
