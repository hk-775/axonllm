"""Region and spoke configuration for hub-and-spoke topology.

Supports single-region, active-passive failover, and active-active multi-region
with the same data model — just change the spoke list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SpokeRole(Enum):
    PRIMARY = "primary"
    FAILOVER = "failover"
    ACTIVE = "active"


class SpokeStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    DRAINING = "draining"


@dataclass
class SpokeConfig:
    """Configuration for a single spoke (regional deployment)."""

    region: str
    role: SpokeRole = SpokeRole.PRIMARY
    weight: int = 100
    status: SpokeStatus = SpokeStatus.HEALTHY
    endpoint: str = ""
    providers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    data_residency_zones: list[str] = field(default_factory=list)
    health_check_url: str = ""
    max_latency_ms: int = 5000
    failover_priority: int = 0


@dataclass
class HubConfig:
    """Hub configuration — the control plane that routes to spokes."""

    hub_region: str
    spokes: list[SpokeConfig] = field(default_factory=list)
    health_check_interval_seconds: int = 30
    failover_threshold_consecutive: int = 3
    failover_cooldown_seconds: int = 60
    data_residency_strict: bool = False

    @property
    def active_spokes(self) -> list[SpokeConfig]:
        return [s for s in self.spokes if s.status == SpokeStatus.HEALTHY]

    @property
    def is_single_region(self) -> bool:
        return len(self.spokes) <= 1

    def get_spoke(self, region: str) -> SpokeConfig | None:
        return next((s for s in self.spokes if s.region == region), None)

    def get_primary(self) -> SpokeConfig | None:
        return next((s for s in self.spokes if s.role == SpokeRole.PRIMARY), None)

    def get_failover_candidates(self) -> list[SpokeConfig]:
        """Return spokes ordered by failover priority (lower = higher priority)."""
        candidates = [
            s for s in self.spokes
            if s.role in (SpokeRole.FAILOVER, SpokeRole.ACTIVE)
            and s.status == SpokeStatus.HEALTHY
        ]
        return sorted(candidates, key=lambda s: s.failover_priority)


def default_single_region(region: str = "us-east-1") -> HubConfig:
    """Sensible default: single-region deployment."""
    return HubConfig(
        hub_region=region,
        spokes=[
            SpokeConfig(
                region=region,
                role=SpokeRole.PRIMARY,
                weight=100,
            )
        ],
    )


def active_passive(primary: str, failover: str) -> HubConfig:
    """Active-passive failover between two regions."""
    return HubConfig(
        hub_region=primary,
        spokes=[
            SpokeConfig(region=primary, role=SpokeRole.PRIMARY, weight=100),
            SpokeConfig(region=failover, role=SpokeRole.FAILOVER, weight=0, failover_priority=1),
        ],
    )


def active_active(regions: list[tuple[str, int]]) -> HubConfig:
    """Active-active across N regions with weights."""
    spokes = []
    for i, (region, weight) in enumerate(regions):
        role = SpokeRole.PRIMARY if i == 0 else SpokeRole.ACTIVE
        spokes.append(SpokeConfig(region=region, role=role, weight=weight))
    return HubConfig(hub_region=regions[0][0], spokes=spokes)
