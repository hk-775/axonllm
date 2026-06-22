"""Admin API routes for multi-region hub-and-spoke management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.multi_region.region_config import SpokeRole, SpokeStatus

if TYPE_CHECKING:
    from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
    from src.gateway.multi_region.region_router import RegionRouter


class RegionAPI:
    """Admin API for multi-region topology management."""

    def __init__(self, router: RegionRouter, monitor: SpokeHealthMonitor) -> None:
        self.router = router
        self.monitor = monitor

    async def get_topology(self, request: Request) -> JSONResponse:
        """GET /admin/regions — full topology view."""
        config = self.router.config
        return JSONResponse(content={
            "hub_region": config.hub_region,
            "mode": self._detect_mode(config),
            "total_spokes": len(config.spokes),
            "healthy_spokes": len(config.active_spokes),
            "spokes": [
                {
                    "region": s.region,
                    "role": s.role.value,
                    "status": s.status.value,
                    "weight": s.weight,
                    "providers": s.providers,
                    "models": s.models,
                    "data_residency_zones": s.data_residency_zones,
                    "failover_priority": s.failover_priority,
                }
                for s in config.spokes
            ],
        })

    async def get_health(self, request: Request) -> JSONResponse:
        """GET /admin/regions/health — health status of all spokes."""
        return JSONResponse(content=self.monitor.get_status_summary())

    async def check_health_now(self, request: Request) -> JSONResponse:
        """POST /admin/regions/health/check — trigger immediate health check."""
        results = await self.monitor.check_all()
        return JSONResponse(content={
            "checked": len(results),
            "results": [
                {
                    "region": r.region,
                    "healthy": r.healthy,
                    "latency_ms": round(r.latency_ms, 1),
                    "status_code": r.status_code,
                    "error": r.error,
                }
                for r in results
            ],
        })

    async def route_test(self, request: Request) -> JSONResponse:
        """POST /admin/regions/route — test routing decision for given params."""
        body = await request.json() if await request.body() else {}
        model = body.get("model")
        zone = body.get("data_residency_zone")
        preferred = body.get("preferred_region")

        decision = self.router.route(
            model=model,
            data_residency_zone=zone,
            preferred_region=preferred,
        )

        if decision is None:
            return JSONResponse(
                status_code=503,
                content={"error": "No healthy spoke available for this request"},
            )

        return JSONResponse(content={
            "target_region": decision.target_spoke.region,
            "reason": decision.reason,
            "candidates_considered": decision.candidates_considered,
            "fallback_used": decision.fallback_used,
        })

    async def mark_spoke_status(self, request: Request) -> JSONResponse:
        """PUT /admin/regions/{region}/status — manually override spoke status."""
        region = request.path_params["region"]
        body = await request.json()
        new_status = body.get("status", "")

        spoke = self.router.config.get_spoke(region)
        if spoke is None:
            return JSONResponse(status_code=404, content={"error": f"Region '{region}' not found"})

        if new_status == "healthy":
            self.monitor.mark_healthy(region)
        elif new_status == "unhealthy":
            self.monitor.mark_unhealthy(region)
        elif new_status == "draining":
            self.monitor.mark_draining(region)
        else:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid status. Valid: healthy, unhealthy, draining"},
            )

        return JSONResponse(content={
            "region": region,
            "status": spoke.status.value,
            "message": f"Spoke {region} marked as {new_status}",
        })

    async def trigger_failover(self, request: Request) -> JSONResponse:
        """POST /admin/regions/failover — force failover from primary."""
        primary = self.router.config.get_primary()
        if primary:
            self.monitor.mark_unhealthy(primary.region)

        decision = self.router.failover()
        if decision is None:
            return JSONResponse(
                status_code=503,
                content={"error": "No failover candidates available"},
            )

        return JSONResponse(content={
            "failover_to": decision.target_spoke.region,
            "reason": decision.reason,
            "primary_marked_unhealthy": primary.region if primary else None,
        })

    def _detect_mode(self, config) -> str:
        if len(config.spokes) <= 1:
            return "single_region"
        has_failover = any(s.role == SpokeRole.FAILOVER for s in config.spokes)
        if has_failover:
            return "active_passive"
        return "active_active"


def create_region_routes(region_api: RegionAPI) -> list[Route]:
    """Create Starlette routes for multi-region management."""
    return [
        Route("/admin/regions", region_api.get_topology, methods=["GET"]),
        Route("/admin/regions/health", region_api.get_health, methods=["GET"]),
        Route("/admin/regions/health/check", region_api.check_health_now, methods=["POST"]),
        Route("/admin/regions/route", region_api.route_test, methods=["POST"]),
        Route("/admin/regions/failover", region_api.trigger_failover, methods=["POST"]),
        Route("/admin/regions/{region}/status", region_api.mark_spoke_status, methods=["PUT"]),
    ]
