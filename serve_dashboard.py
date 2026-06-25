"""Quick dev server to preview the Admin Dashboard locally.

Run: AXON_LOAD_DEMO_DATA=true python serve_dashboard.py
Then open: http://localhost:8000/admin/dashboard
"""

import asyncio
import os

import uvicorn

from src.gateway.bootstrap import build_starlette_app
from src.gateway.config import AppConfig
from src.gateway.config_loader import load_app_config
from src.gateway.health_check_task import HealthCheckTask
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry


def build_app() -> tuple:
    """Build the Starlette app and return (app, app_config)."""
    # Default to loading demo data when running the dev server directly
    if "AXON_LOAD_DEMO_DATA" not in os.environ:
        os.environ["AXON_LOAD_DEMO_DATA"] = "true"

    app_config = load_app_config()
    app = build_starlette_app(app_config)
    return app, app_config


if __name__ == "__main__":
    app, app_config = build_app()

    # --- Start background health check task ---
    registry = ModelRegistry()
    registry.load(app_config.models_config_path)
    providers: set[str] = set()
    for model_cfg in registry.models.values():
        for pm in model_cfg.providers:
            providers.add(pm.provider)

    health_tracker = ProviderHealthTracker()

    async def _default_check_fn(provider: str) -> bool:
        """Placeholder health check — always returns True."""
        return True

    health_task = HealthCheckTask(
        health_tracker=health_tracker,
        providers=sorted(providers),
        check_fn=_default_check_fn,
    )

    asyncio.run(health_task.start())

    print(f"\n  Dashboard: http://localhost:{app_config.server_port}/admin/dashboard")
    print(f"  Chat:      http://localhost:{app_config.server_port}/chat\n")
    uvicorn.run(app, host=app_config.server_host, port=app_config.server_port)
