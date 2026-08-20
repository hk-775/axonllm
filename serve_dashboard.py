"""Quick dev server to preview the Admin Dashboard locally.

Run: AXON_LOAD_DEMO_DATA=true python serve_dashboard.py
Then open: http://localhost:8000/admin/dashboard
"""

import os
import sys
import threading
import webbrowser

import uvicorn

from src.gateway.admin.pricing_drift import audit_pricing, format_startup_notice
from src.gateway.bootstrap import build_starlette_app
from src.gateway.config_loader import load_app_config, load_pricing_config
from src.gateway.dev_env import load_dev_env_file
from src.gateway.model_registry import ModelRegistry


def build_app() -> tuple:
    """Build the Starlette app and return (app, app_config)."""
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


def main() -> None:
    """Start the dashboard without making external calls before binding."""
    app, app_config = build_app()

    registry = ModelRegistry()
    registry.load(app_config.models_config_path)

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

    # The production checklist performs live provider and AWS catalogue calls.
    # Running it here delayed the server bind for up to five minutes when a
    # worker-thread call timed out, so a two-task ECS service could never reach
    # steady state. The admin route runs the same report on demand after the
    # server is accepting traffic.
    if not app_config.load_demo_data:
        print(f"  Readiness: {base}/admin/production-checklist\n")

    # Flush before handing off to uvicorn, which reconfigures logging and can
    # otherwise interleave its own output through the banner above. The gap is
    # only ever reported, never fatal: the gateway routes correctly either way,
    # and a dev server that refused to start over a missing price would be worse
    # than one that bills at zero.
    sys.stdout.flush()
    uvicorn.run(app, host=app_config.server_host, port=app_config.server_port)


if __name__ == "__main__":
    main()
