"""Atomic in-memory backend for distributed enforcement tests."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

from src.gateway.models import RateLimitResult


class SharedEnforcementBackend:
    """Model the persistence contracts shared by multiple gateway instances."""

    enabled = True

    def __init__(self) -> None:
        self.rate_counts: dict[tuple, int] = {}
        self.spend_totals: dict[tuple[str, str], float] = {}
        self.rate_calls: list[dict] = []
        self.rate_available = True
        self.rate_error: Exception | None = None
        self._lock = asyncio.Lock()

    async def consume_rate_limit_window(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        user_id: str | None,
        project_id: str,
        user_limit: int | None,
        project_limit: int,
        window_seconds: int,
        now: datetime,
    ) -> RateLimitResult | None:
        self.rate_calls.append({
            "namespace": namespace,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id,
            "user_limit": user_limit,
            "project_limit": project_limit,
            "window_seconds": window_seconds,
            "now": now,
        })
        if self.rate_error is not None:
            raise self.rate_error
        if not self.rate_available:
            return None

        window_start = int(now.timestamp()) // window_seconds * window_seconds
        reset_at = datetime.fromtimestamp(
            window_start + window_seconds,
            tz=timezone.utc,
        )
        counters: list[tuple[tuple, int]] = []
        if user_id is not None and user_limit is not None:
            counters.append((
                (
                    namespace,
                    tenant_id,
                    "user",
                    user_id,
                    window_start,
                ),
                user_limit,
            ))
        counters.append((
            (
                namespace,
                tenant_id,
                "project",
                project_id,
                window_start,
            ),
            project_limit,
        ))

        async with self._lock:
            allowed = all(
                self.rate_counts.get(key, 0) < limit
                for key, limit in counters
            )
            if allowed:
                for key, _ in counters:
                    self.rate_counts[key] = self.rate_counts.get(key, 0) + 1

            states = [
                (
                    limit,
                    max(0, limit - self.rate_counts.get(key, 0)),
                )
                for key, limit in counters
            ]

        limit, remaining = min(states, key=lambda state: (state[1], state[0]))
        retry_after = None
        if not allowed:
            retry_after = max(
                1,
                math.ceil((reset_at - now).total_seconds()),
            )
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            retry_after_seconds=retry_after,
        )

    async def add_spend(
        self,
        scope: str,
        ident: str,
        cost: float,
    ) -> float:
        async with self._lock:
            key = (scope, ident)
            total = self.spend_totals.get(key, 0.0) + cost
            self.spend_totals[key] = total
            return total

    async def get_spend(
        self,
        scope: str,
        ident: str,
    ) -> float | None:
        return self.spend_totals.get((scope, ident))

    async def reset_spend(
        self,
        scope: str,
        ident: str,
    ) -> bool:
        self.spend_totals.pop((scope, ident), None)
        return True
