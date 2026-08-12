"""Package-local development server for the AxonLLM gateway.

Run: AXON_LOAD_DEMO_DATA=true python -m src.gateway.local_server
Then open: http://localhost:8000/admin/dashboard
"""

import asyncio
import os
from pathlib import Path
import sys
import threading
import webbrowser

import uvicorn

from src.gateway.admin.pricing_drift import audit_pricing, format_startup_notice
from src.gateway.admin.production_checklist import (
    format_startup_notice as checklist_notice,
    run_checklist,
)
from src.gateway.bootstrap import build_starlette_app
from src.gateway.config_loader import load_app_config, load_pricing_config
from src.gateway.dev_env import load_dev_env_file
from src.gateway.health_check_task import HealthCheckTask
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.provider_loader import load_provider_configs


_RUNTIME_ROOT = Path(__file__).resolve().parent / "resources" / "runtime"


def _configure_packaged_runtime() -> None:
    """Point default runtime paths at immutable resources shipped in the wheel."""
    invocation_dir = Path.cwd()
    local_env = invocation_dir / ".env"
    if local_env.is_file():
        os.environ.setdefault("AXON_DEV_ENV_FILE", str(local_env))

    config_dir = _RUNTIME_ROOT / "config"
    defaults = {
        "AXON_MODELS_CONFIG": config_dir / "models.yaml",
        "AXON_PROVIDERS_CONFIG": config_dir / "providers.yaml",
        "AXON_PRICING_CONFIG": config_dir / "pricing.yaml",
        "AXON_DEMO_SEED_CONFIG": config_dir / "demo_seed.yaml",
        "AXON_CATALOG_CONFIG": config_dir / "catalog.yaml",
        "AXON_ENSEMBLE_CONFIG": config_dir / "ensemble.yaml",
        "AXON_SPOKES_CONFIG": config_dir / "spokes.yaml",
    }
    for name, path in defaults.items():
        os.environ.setdefault(name, str(path))

    # AdminAPI intentionally resolves the public site from a project root.
    # Installed wheels have no project root, so bind that lookup to the
    # packaged runtime tree before building routes.
    from src.gateway.admin import routes as admin_routes

    admin_routes._PROJECT_ROOT = _RUNTIME_ROOT
    os.chdir(_RUNTIME_ROOT)


def build_app() -> tuple:
    """Build the Starlette app and return (app, app_config)."""
    _configure_packaged_runtime()
    # Read the local .env before the demo-data default below is applied: the
    # loader's gate is whether the operator set AXON_LOAD_DEMO_DATA themselves,
    # and this same entrypoint is the Dockerfile CMD. Applying the default first
    # would make the file load in production containers too. It never overwrites
    # an existing variable, so platform-injected secrets always win.
    load_dev_env_file()

    # Default to loading demo data when running the dev server directly
    if "AXON_LOAD_DEMO_DATA" not in os.environ:
        os.environ["AXON_LOAD_DEMO_DATA"] = "true"

    # This is the local-development entrypoint. Container and AWS deployments
    # inject their profile explicitly, so only an unconfigured direct run gets
    # the development contract.
    if "AXON_DEPLOYMENT_PROFILE" not in os.environ:
        os.environ["AXON_DEPLOYMENT_PROFILE"] = "development"

    # The local dev server is meant to be run without credentials so the admin
    # dashboard (which sends no auth header yet — see task #10) works out of the
    # box. Production defaults to ENFORCE; only this dev entrypoint opts out, and
    # only when the operator hasn't set AXON_AUTH_MODE themselves.
    if "AXON_AUTH_MODE" not in os.environ:
        os.environ["AXON_AUTH_MODE"] = "LOG_ONLY"

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

    base = f"http://localhost:{app_config.server_port}"
    print(f"\n  Dashboard: {base}/admin/dashboard")
    print(f"  Chat:      {base}/chat")

    # --- Pricing coverage ---
    # Reuse the registry already loaded above rather than building a second one.
    drift = audit_pricing(registry, load_pricing_config(app_config.pricing_config_path))
    drift_url = f"{base}/admin/pricing-drift"
    print(f"  Pricing:   {drift_url}\n")

    notice = format_startup_notice(drift, drift_url)
    if notice:
        print(notice + "\n")
        # Open the page rather than only printing about it. An unpriced model is
        # unavailable in production and under-accounted in development, and a
        # line in the startup scroll is exactly the kind of warning that gets
        # missed.
        #
        # Two guards, because this same file is the Dockerfile CMD: an explicit
        # AXON_NO_BROWSER, and a tty check. A container or CI runner has no
        # terminal and nobody watching a browser, and while webbrowser.open
        # merely fails there, asking is pointless.
        opt_out = os.environ.get("AXON_NO_BROWSER", "").lower() in ("1", "true", "yes")
        if not opt_out and sys.stdout.isatty():
            # uvicorn.run blocks, so the open has to be deferred to a timer: the
            # server is not accepting connections yet at this point. daemon, so
            # a Ctrl-C in the first second and a half exits immediately rather
            # than waiting on the timer.
            def _open() -> None:
                try:
                    webbrowser.open(drift_url)
                except Exception:  # pragma: no cover - platform dependent
                    pass

            timer = threading.Timer(1.5, _open)
            timer.daemon = True
            timer.start()
    elif drift.total_mappings:
        print(f"  ✓ All {drift.total_mappings} provider mappings are priced.\n")

    # --- Production readiness ---
    # Only outside demo mode. In a demo every check fails correctly and none of
    # it means anything, and the live provider calls should not happen at all.
    if not app_config.load_demo_data:
        checklist_url = f"{base}/admin/production-checklist"
        try:
            checklist = asyncio.run(
                run_checklist(
                    app_config=app_config,
                    model_registry=registry,
                    pricing_config=load_pricing_config(app_config.pricing_config_path),
                    provider_configs=load_provider_configs(app_config.providers_config_path),
                )
            )
        except Exception as exc:
            # Never fatal. A readiness report that can stop a boot is strictly
            # worse than one that fails to print: the gateway serves correctly
            # either way, and an operator locked out by their own checklist has
            # no way to read what it found.
            print(f"  ⚠  Readiness checklist could not run ({type(exc).__name__}).\n")
        else:
            notice = checklist_notice(checklist, checklist_url)
            if notice:
                print(notice + "\n")
            elif checklist.unresolved:
                print(f"  Readiness: {checklist.unresolved} item(s) to review — {checklist_url}\n")
            else:
                print("  ✓ Production readiness: all checks pass.\n")

    # Flush before handing off to uvicorn, which reconfigures logging and can
    # otherwise interleave its own output through the banner above. The gap is
    # only ever reported, never fatal: the gateway routes correctly either way,
    # and a dev server that refused to start over a missing price would be worse
    # than one that bills at zero.
    sys.stdout.flush()
    uvicorn.run(app, host=app_config.server_host, port=app_config.server_port)
