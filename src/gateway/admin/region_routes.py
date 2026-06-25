"""Admin API routes for multi-region hub-and-spoke management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.multi_region.region_config import SpokeConfig, SpokeRole

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
            "data_residency_strict": config.data_residency_strict,
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
                content={"error": "Invalid status. Valid: healthy, unhealthy, draining"},
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

    async def add_spoke(self, request: Request) -> JSONResponse:
        """POST /admin/regions/spokes — add a new spoke to the topology."""
        body = await request.json()
        region = body.get("region", "")
        if not region:
            return JSONResponse(status_code=400, content={"error": "region is required"})

        if self.router.config.get_spoke(region):
            return JSONResponse(status_code=409, content={"error": f"Spoke '{region}' already exists"})

        role_str = body.get("role", "active")
        try:
            role = SpokeRole(role_str)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": f"Invalid role: {role_str}. Valid: primary, failover, active"})

        spoke = SpokeConfig(
            region=region,
            role=role,
            weight=body.get("weight", 50),
            endpoint=body.get("endpoint", ""),
            providers=body.get("providers", []),
            models=body.get("models", []),
            data_residency_zones=body.get("data_residency_zones", []),
            failover_priority=body.get("failover_priority", len(self.router.config.spokes)),
        )
        self.router.config.spokes.append(spoke)
        return JSONResponse(status_code=201, content={"message": f"Spoke '{region}' added", "region": region})

    async def remove_spoke(self, request: Request) -> JSONResponse:
        """DELETE /admin/regions/spokes/{region} — remove a spoke."""
        region = request.path_params["region"]
        spoke = self.router.config.get_spoke(region)
        if spoke is None:
            return JSONResponse(status_code=404, content={"error": f"Spoke '{region}' not found"})

        self.router.config.spokes.remove(spoke)
        return JSONResponse(content={"message": f"Spoke '{region}' removed"})

    async def update_spoke(self, request: Request) -> JSONResponse:
        """PUT /admin/regions/spokes/{region} — update spoke configuration."""
        region = request.path_params["region"]
        spoke = self.router.config.get_spoke(region)
        if spoke is None:
            return JSONResponse(status_code=404, content={"error": f"Spoke '{region}' not found"})

        body = await request.json()
        if "weight" in body:
            spoke.weight = int(body["weight"])
        if "role" in body:
            try:
                spoke.role = SpokeRole(body["role"])
            except ValueError:
                return JSONResponse(status_code=400, content={"error": f"Invalid role: {body['role']}"})
        if "data_residency_zones" in body:
            spoke.data_residency_zones = body["data_residency_zones"]
        if "failover_priority" in body:
            spoke.failover_priority = int(body["failover_priority"])
        if "providers" in body:
            spoke.providers = body["providers"]
        if "models" in body:
            spoke.models = body["models"]

        return JSONResponse(content={"message": f"Spoke '{region}' updated", "spoke": {
            "region": spoke.region, "role": spoke.role.value, "weight": spoke.weight,
            "data_residency_zones": spoke.data_residency_zones,
        }})

    async def update_config(self, request: Request) -> JSONResponse:
        """PUT /admin/regions/config — update hub-level topology settings."""
        body = await request.json()
        config = self.router.config

        if "hub_region" in body:
            config.hub_region = body["hub_region"]
        if "data_residency_strict" in body:
            config.data_residency_strict = bool(body["data_residency_strict"])
        if "health_check_interval_seconds" in body:
            config.health_check_interval_seconds = int(body["health_check_interval_seconds"])
        if "failover_threshold_consecutive" in body:
            config.failover_threshold_consecutive = int(body["failover_threshold_consecutive"])
        if "failover_cooldown_seconds" in body:
            config.failover_cooldown_seconds = int(body["failover_cooldown_seconds"])

        return JSONResponse(content={"message": "Topology config updated", "mode": self._detect_mode(config)})

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
        Route("/admin/regions/config", region_api.update_config, methods=["PUT"]),
        Route("/admin/regions/health", region_api.get_health, methods=["GET"]),
        Route("/admin/regions/health/check", region_api.check_health_now, methods=["POST"]),
        Route("/admin/regions/route", region_api.route_test, methods=["POST"]),
        Route("/admin/regions/spokes", region_api.add_spoke, methods=["POST"]),
        Route("/admin/regions/spokes/{region}", region_api.update_spoke, methods=["PUT"]),
        Route("/admin/regions/spokes/{region}", region_api.remove_spoke, methods=["DELETE"]),
        Route("/admin/regions/failover", region_api.trigger_failover, methods=["POST"]),
        Route("/admin/regions/{region}/status", region_api.mark_spoke_status, methods=["PUT"]),
    ]
