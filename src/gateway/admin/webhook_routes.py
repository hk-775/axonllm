"""Admin API routes for event dispatcher / webhook management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.security.event_dispatcher import DestinationType, EventDestination

if TYPE_CHECKING:
    from src.gateway.security.event_dispatcher import EventDispatcher


class WebhookAPI:
    """Manage event dispatch destinations."""

    def __init__(self, dispatcher: EventDispatcher) -> None:
        self.dispatcher = dispatcher

    async def list_destinations(self, request: Request) -> JSONResponse:
        """GET /admin/webhooks"""
        destinations = self.dispatcher.destinations
        return JSONResponse(content={
            "count": len(destinations),
            "destinations": [
                {
                    "name": d.name,
                    "type": d.destination_type.value,
                    "enabled": d.enabled,
                    "event_filter": d.event_filter,
                    "config": {k: v for k, v in d.config.items() if k != "secret"},
                }
                for d in destinations
            ],
            "stats": self.dispatcher.stats,
        })

    async def add_destination(self, request: Request) -> JSONResponse:
        """POST /admin/webhooks"""
        body = await request.json()

        name = body.get("name")
        if not name:
            return JSONResponse(status_code=400, content={"error": "name is required"})

        try:
            dest_type = DestinationType(body.get("type", "webhook"))
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid type. Valid: {[t.value for t in DestinationType]}"},
            )

        dest = EventDestination(
            name=name,
            destination_type=dest_type,
            config=body.get("config", {}),
            event_filter=body.get("event_filter"),
            enabled=body.get("enabled", True),
        )

        self.dispatcher.add_destination(dest)

        return JSONResponse(
            status_code=201,
            content={
                "name": dest.name,
                "type": dest.destination_type.value,
                "enabled": dest.enabled,
                "event_filter": dest.event_filter,
            },
        )

    async def remove_destination(self, request: Request) -> JSONResponse:
        """DELETE /admin/webhooks/{name}"""
        name = request.path_params["name"]
        removed = self.dispatcher.remove_destination(name)

        if not removed:
            return JSONResponse(status_code=404, content={"error": f"Destination '{name}' not found"})

        return JSONResponse(content={"status": "removed", "name": name})

    async def test_destination(self, request: Request) -> JSONResponse:
        """POST /admin/webhooks/{name}/test"""
        name = request.path_params["name"]

        dest = next((d for d in self.dispatcher.destinations if d.name == name), None)
        if dest is None:
            return JSONResponse(status_code=404, content={"error": f"Destination '{name}' not found"})

        from src.gateway.security.event_dispatcher import SecurityEvent
        from datetime import datetime, timezone

        test_event = SecurityEvent(
            event_id="test_event_001",
            event_type="test",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="info",
            data={"message": "AxonLLM webhook test event"},
        )

        try:
            await self.dispatcher._send_to_destination(test_event, dest)
            return JSONResponse(content={"status": "sent", "destination": name})
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"status": "failed", "destination": name, "error": str(e)},
            )

    async def get_stats(self, request: Request) -> JSONResponse:
        """GET /admin/webhooks/stats"""
        return JSONResponse(content=self.dispatcher.stats)


def create_webhook_routes(webhook_api: WebhookAPI) -> list[Route]:
    """Create Starlette routes for webhook/event dispatcher management."""
    return [
        Route("/admin/webhooks", webhook_api.list_destinations, methods=["GET"]),
        Route("/admin/webhooks", webhook_api.add_destination, methods=["POST"]),
        Route("/admin/webhooks/stats", webhook_api.get_stats, methods=["GET"]),
        Route("/admin/webhooks/{name}", webhook_api.remove_destination, methods=["DELETE"]),
        Route("/admin/webhooks/{name}/test", webhook_api.test_destination, methods=["POST"]),
    ]
