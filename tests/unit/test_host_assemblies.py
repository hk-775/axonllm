from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from axonllm import (
    CredentialResolver,
    IdentityContext,
    OstiariHost,
    RouterLifecycle,
    RoutingConfigurationProvider,
    TelemetrySink,
    UsageSink,
    build_router,
)
from src.gateway import bootstrap
from src.gateway.config import AppConfig
from src.gateway.control_plane_routes import (
    ROUTE_CONTRACT_VERSION,
    RouteDisposition,
    classify_control_route,
    control_route_inventory,
)
from src.gateway.host_assemblies import build_worker
from src.gateway import serverless_control

_REPO = Path(__file__).resolve().parents[2]


def _write_router_config(tmp_path: Path) -> tuple[Path, Path]:
    models = tmp_path / "models.yaml"
    models.write_text(
        """
models:
  - name: balanced
    description: Host assembly test model
    capabilities: [chat]
    providers:
      - provider: openai
        model_id: test-model
""",
        encoding="utf-8",
    )
    providers = tmp_path / "providers.yaml"
    providers.write_text(
        """
providers:
  openai:
    base_url: https://openai.example
    auth_type: api_key
    api_key: not-a-real-key
""",
        encoding="utf-8",
    )
    return models, providers


def test_public_package_import_does_not_require_server_or_aws_packages() -> None:
    code = textwrap.dedent(
        """
        import importlib.abc
        import sys

        blocked = {
            "boto3",
            "botocore",
            "google",
            "sqlglot",
            "starlette",
            "uvicorn",
        }

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition(".")[0] in blocked:
                    raise AssertionError(f"unexpected host dependency import: {fullname}")
                return None

        sys.meta_path.insert(0, Blocker())
        import axonllm

        assert callable(axonllm.build_router)
        assert axonllm.IdentityContext.__module__ == "axonllm.hosts"
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPO)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_build_router_constructs_only_the_embedded_data_plane(
    tmp_path: Path,
) -> None:
    models, providers = _write_router_config(tmp_path)

    router = build_router(
        models=models,
        providers=providers,
        enabled_providers={"openai"},
    )
    try:
        assert router.available_providers == frozenset({"openai"})
        assert [model.name for model in await router.models.list()] == [
            "balanced"
        ]
    finally:
        await router.close()


def test_build_control_components_skips_data_plane_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "false")

    def fail(*_args, **_kwargs):
        raise AssertionError(
            "control assembly constructed data-plane state"
        )

    for name in (
        "AthenaExecutor",
        "GatewayAgent",
        "MultiProviderFactory",
        "OTLPSpanExporter",
        "QueryAdmissionController",
        "QueryReconciliationWorker",
        "QueryService",
        "Router",
        "TraceForwarder",
        "build_embedder",
        "build_entity_detector",
        "load_ensemble_config",
        "load_provider_routes",
    ):
        monkeypatch.setattr(bootstrap, name, fail)
    monkeypatch.setattr(
        bootstrap.DynamoPersistence,
        "create_table_if_not_exists",
        fail,
    )

    components = bootstrap.build_control_components(
        AppConfig(
            auth_mode="LOG_ONLY",
            control_plane_only=True,
        )
    )

    assert components.persistence.enabled is False
    assert components.provider_configs == {}
    assert components.event_dispatcher is not None
    assert components.semantic_cache is not None


def test_control_api_routes_are_complete_and_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_DYNAMODB_ENABLED", "false")
    config = AppConfig(
        auth_mode="LOG_ONLY",
        control_plane_only=True,
    )
    components = bootstrap.build_control_components(config)

    app = bootstrap.build_control_api(config, components)
    inventory = control_route_inventory(app.routes)

    assert ROUTE_CONTRACT_VERSION == 2
    assert inventory
    assert not any(
        route.path.startswith(("/api/", "/v1/"))
        for route in inventory
    )
    assert {
        route.disposition
        for route in inventory
    } == set(RouteDisposition)
    assert ("GET", "/") in {
        (route.method, route.path)
        for route in inventory
    }


def test_unknown_control_route_fails_closed() -> None:
    with pytest.raises(ValueError, match="is not classified"):
        classify_control_route("POST", "/new-control-surface")


def test_serverless_control_adapter_is_lazy_and_warm_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        auth_mode="LOG_ONLY",
        control_plane_only=True,
    )
    application = object()
    components = object()
    calls: list[tuple[object, str]] = []

    class FakeMangum:
        def __init__(self, app, *, lifespan):
            calls.append((app, lifespan))

        def __call__(self, event, context):
            return {"event": event, "context": context}

    monkeypatch.setitem(
        sys.modules,
        "mangum",
        SimpleNamespace(Mangum=FakeMangum),
    )
    monkeypatch.setattr(
        serverless_control,
        "load_app_config",
        lambda: config,
    )
    monkeypatch.setattr(
        serverless_control,
        "build_control_components",
        lambda actual: components
        if actual is config
        else pytest.fail("wrong config"),
    )
    monkeypatch.setattr(
        serverless_control,
        "build_control_api",
        lambda actual, supplied: application
        if actual is config and supplied is components
        else pytest.fail("wrong control assembly"),
    )
    prepared: list[dict] = []
    monkeypatch.setattr(
        serverless_control,
        "_prepare_runtime_environment",
        prepared.append,
    )
    serverless_control.build_lambda_application.cache_clear()
    serverless_control._lambda_adapter.cache_clear()

    first = serverless_control.lambda_handler({"request": 1}, "context")
    second = serverless_control.lambda_handler({"request": 2}, "context")

    assert first == {"event": {"request": 1}, "context": "context"}
    assert second == {"event": {"request": 2}, "context": "context"}
    assert calls == [(application, "off")]
    assert prepared == [{"request": 1}, {"request": 2}]
    serverless_control.build_lambda_application.cache_clear()
    serverless_control._lambda_adapter.cache_clear()


def test_serverless_control_requires_control_only_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        serverless_control,
        "load_app_config",
        lambda: AppConfig(auth_mode="LOG_ONLY"),
    )
    serverless_control.build_lambda_application.cache_clear()

    with pytest.raises(RuntimeError, match="CONTROL_PLANE_ONLY"):
        serverless_control.build_lambda_application()

    serverless_control.build_lambda_application.cache_clear()


def test_serverless_runtime_environment_uses_trusted_cloudfront_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AXON_BROWSER_AUTH_CLIENT_ID",
        "AXON_CONTROL_PLANE_URL",
        "AXON_OIDC_AUDIENCE",
        "AXON_SCIM_TENANTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        serverless_control,
        "_browser_client_id",
        lambda: "browser-client-id",
    )
    monkeypatch.setattr(
        serverless_control,
        "_scim_tenant_configuration",
        lambda: '{"tenant-a":{"token":"secret","issuer":"idp"}}',
    )

    serverless_control._prepare_runtime_environment(
        {
            "headers": {
                "X-Axon-Public-Host": "D123.CloudFront.Net",
            }
        }
    )

    assert os.environ["AXON_CONTROL_PLANE_URL"] == (
        "https://d123.cloudfront.net"
    )
    assert os.environ["AXON_OIDC_AUDIENCE"] == "browser-client-id"
    assert (
        os.environ["AXON_BROWSER_AUTH_CLIENT_ID"]
        == "browser-client-id"
    )
    assert os.environ["AXON_SCIM_TENANTS"].startswith('{"tenant-a"')


def test_serverless_runtime_rejects_untrusted_or_changed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        serverless_control,
        "_browser_client_id",
        lambda: "browser-client-id",
    )
    monkeypatch.setattr(
        serverless_control,
        "_scim_tenant_configuration",
        lambda: None,
    )
    monkeypatch.setenv(
        "AXON_CONTROL_PLANE_URL",
        "https://expected.cloudfront.net",
    )
    monkeypatch.setenv("AXON_OIDC_AUDIENCE", "browser-client-id")

    with pytest.raises(RuntimeError, match="public host changed"):
        serverless_control._prepare_runtime_environment(
            {
                "headers": {
                    "x-axon-public-host": "other.cloudfront.net",
                }
            }
        )
    with pytest.raises(RuntimeError, match="header is invalid"):
        serverless_control._trusted_public_host(
            {"headers": {"x-axon-public-host": "https://invalid"}}
        )


def test_identity_context_requires_explicit_scope() -> None:
    context = IdentityContext(
        principal_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        roles=frozenset({"operator"}),
        scopes=frozenset({"model.list"}),
    )

    assert context.tenant_id == "tenant-1"
    with pytest.raises(ValueError, match="project_id"):
        IdentityContext(
            principal_id="user-1",
            tenant_id="tenant-1",
            project_id="",
        )


def test_ostiari_host_protocols_are_structural() -> None:
    class Host:
        async def load_snapshot(self):
            return None

        async def publish_snapshot(self, config, *, expected_revision):
            return None

        async def resolve(self, *, provider, reference):
            return {}

        async def emit(self, event):
            return None

        async def record(self, usage):
            return None

        async def start(self):
            return None

        async def close(self):
            return None

    host = Host()

    assert isinstance(host, RoutingConfigurationProvider)
    assert isinstance(host, CredentialResolver)
    assert isinstance(host, OstiariHost)
    assert isinstance(host, TelemetrySink)
    assert isinstance(host, UsageSink)
    assert isinstance(host, RouterLifecycle)


class _EventWorker:
    def __init__(
        self,
        events: list[str],
        *,
        ready: bool = True,
        fail_stop: bool = False,
    ) -> None:
        self.events = events
        self.outbox_enabled = True
        self.ready = ready
        self.fail_stop = fail_stop

    async def check_readiness(self) -> bool:
        self.events.append("event:ready")
        return self.ready

    async def start(self) -> None:
        self.events.append("event:start")

    async def stop(self) -> None:
        self.events.append("event:stop")
        if self.fail_stop:
            raise RuntimeError("event stop failed")


class _Monitor:
    def __init__(self, events: list[str], *, fail_stop: bool = False) -> None:
        self.events = events
        self.is_running = True
        self.config = SimpleNamespace(spokes=("a", "b"))
        self.fail_stop = fail_stop

    async def reconcile(self) -> None:
        self.events.append("monitor:reconcile")

    async def stop(self) -> None:
        self.events.append("monitor:stop")
        if self.fail_stop:
            raise RuntimeError("monitor stop failed")


class _PeriodicWorker:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    async def start(self) -> None:
        self.events.append(f"{self.name}:start")

    async def stop(self) -> None:
        self.events.append(f"{self.name}:stop")


def test_build_worker_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_create_task(*args, **kwargs):
        raise AssertionError("worker construction created an asyncio task")

    monkeypatch.setattr(asyncio, "create_task", fail_create_task)

    worker = build_worker()

    assert worker.started is False
    assert worker.closed is False


@pytest.mark.asyncio
async def test_worker_owns_start_and_reverse_cleanup_order() -> None:
    events: list[str] = []
    event_worker = _EventWorker(events)
    monitor = _Monitor(events)
    first = _PeriodicWorker("first", events)
    second = _PeriodicWorker("second", events)

    async def first_hook() -> None:
        events.append("hook:first")

    async def second_hook() -> None:
        events.append("hook:second")

    worker = build_worker(
        event_worker=event_worker,
        reconciliation_monitor=monitor,
        periodic_workers=(first, second),
        close_hooks=(first_hook, second_hook),
    )

    async with worker.lifespan():
        assert worker.started is True
        assert worker.closed is False

    assert events == [
        "event:ready",
        "event:start",
        "monitor:reconcile",
        "first:start",
        "second:start",
        "second:stop",
        "first:stop",
        "event:stop",
        "monitor:stop",
        "hook:second",
        "hook:first",
    ]
    assert worker.closed is True


@pytest.mark.asyncio
async def test_worker_readiness_failure_cleans_up_and_fails_closed() -> None:
    events: list[str] = []
    worker = build_worker(
        event_worker=_EventWorker(events, ready=False),
        reconciliation_monitor=_Monitor(events),
    )

    with pytest.raises(
        RuntimeError,
        match="security event outbox is unavailable",
    ):
        await worker.start()

    assert worker.closed is True
    assert events == [
        "event:ready",
        "event:stop",
        "monitor:stop",
    ]


@pytest.mark.asyncio
async def test_worker_attempts_all_cleanup_after_failures() -> None:
    events: list[str] = []
    hook_calls: list[str] = []

    async def hook() -> None:
        hook_calls.append("hook")

    worker = build_worker(
        event_worker=_EventWorker(events, fail_stop=True),
        reconciliation_monitor=_Monitor(events, fail_stop=True),
        close_hooks=(hook,),
    )

    with pytest.raises(
        RuntimeError,
        match="2 worker cleanup operation",
    ):
        await worker.close()

    assert events == ["event:stop", "monitor:stop"]
    assert hook_calls == ["hook"]
    await worker.close()


@pytest.mark.asyncio
async def test_worker_attempts_remaining_cleanup_before_propagating_cancel() -> None:
    events: list[str] = []

    class CancelledWorker(_PeriodicWorker):
        async def stop(self) -> None:
            self.events.append(f"{self.name}:stop")
            raise asyncio.CancelledError

    async def hook() -> None:
        events.append("hook")

    worker = build_worker(
        periodic_workers=(
            _PeriodicWorker("first", events),
            CancelledWorker("cancelled", events),
        ),
        close_hooks=(hook,),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.close()

    assert events == ["cancelled:stop", "first:stop", "hook"]
