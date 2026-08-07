"""Lifecycle, readiness, and shutdown coverage for AgentCore runtime services."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

import agentcore_agent
from src.gateway.agentcore.runtime import (
    RuntimeCloseHook,
    RuntimeDependency,
    RuntimeInitializationError,
    RuntimeProvider,
    RuntimeServices,
    RuntimeState,
    RuntimeUnavailableError,
    build_runtime_services,
)
from src.gateway.auth.cedar_policy import CedarPolicyService
from src.gateway.models import AuthMethod, RequestContext


def _services(
    *,
    checks: tuple[RuntimeDependency, ...] = (),
    close_hooks: tuple[RuntimeCloseHook, ...] = (),
) -> RuntimeServices:
    return RuntimeServices(
        gateway=SimpleNamespace(),
        token_verifier=SimpleNamespace(),
        principal_resolver=SimpleNamespace(),
        project_resolver=SimpleNamespace(),
        readiness_checks=checks,
        close_hooks=close_hooks,
    )


@pytest.mark.asyncio
async def test_get_does_not_trigger_first_request_initialization() -> None:
    calls = 0

    def factory() -> RuntimeServices:
        nonlocal calls
        calls += 1
        return _services()

    provider = RuntimeProvider(factory)

    with pytest.raises(RuntimeUnavailableError):
        await provider.get()

    assert calls == 0
    assert (await provider.readiness()).as_dict() == {
        "status": "not_ready",
        "ready": False,
        "state": "not_initialized",
        "dependencies": {"runtime": "not_initialized"},
    }


@pytest.mark.asyncio
async def test_explicit_initialization_is_single_flight_and_off_loop() -> None:
    calls = 0
    services = _services()

    def factory() -> RuntimeServices:
        nonlocal calls
        calls += 1
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        time.sleep(0.02)
        return services

    provider = RuntimeProvider(factory)
    results = await asyncio.gather(*(provider.initialize() for _ in range(20)))

    assert calls == 1
    assert all(result is services for result in results)
    assert provider.state is RuntimeState.READY
    assert await provider.get() is services
    await provider.close()


@pytest.mark.asyncio
async def test_failed_dependency_probe_fails_startup_and_closes_runtime() -> None:
    close_calls: list[str] = []

    async def unavailable() -> bool:
        return False

    async def close_http() -> None:
        close_calls.append("provider_http")

    provider = RuntimeProvider(
        lambda: _services(
            checks=(
                RuntimeDependency(
                    "principal_store",
                    unavailable,
                    unavailable,
                ),
            ),
            close_hooks=(RuntimeCloseHook("provider_http", close_http),),
        )
    )

    with pytest.raises(
        RuntimeInitializationError,
        match="dependencies are unavailable",
    ):
        await provider.initialize()

    report = await provider.readiness()
    assert report.ready is False
    assert report.state == "failed"
    assert report.dependencies == {
        "runtime": "ready",
        "principal_store": "unavailable",
    }
    assert close_calls == ["provider_http"]
    with pytest.raises(RuntimeUnavailableError):
        await provider.get()
    await provider.close()
    assert close_calls == ["provider_http"]


@pytest.mark.asyncio
async def test_initialization_timeout_is_bounded_and_remains_fail_closed() -> None:
    release_factory = threading.Event()
    close_calls = 0

    async def close_runtime() -> None:
        nonlocal close_calls
        close_calls += 1

    def factory() -> RuntimeServices:
        release_factory.wait(timeout=1)
        return _services(close_hooks=(RuntimeCloseHook("runtime", close_runtime),))

    provider = RuntimeProvider(
        factory,
        initialization_timeout_seconds=0.02,
        shutdown_timeout_seconds=0.2,
    )
    started = time.monotonic()

    with pytest.raises(
        RuntimeInitializationError,
        match="initialization timed out",
    ):
        await provider.initialize()

    assert time.monotonic() - started < 0.15
    assert provider.state is RuntimeState.FAILED
    with pytest.raises(RuntimeUnavailableError):
        await provider.get()

    release_factory.set()
    await provider.close()
    assert provider.state is RuntimeState.CLOSED
    assert close_calls == 1


@pytest.mark.asyncio
async def test_readiness_probe_timeout_does_not_hang_or_rebuild_runtime() -> None:
    block_probe = False
    never = asyncio.Event()
    services: RuntimeServices

    async def dependency() -> bool:
        if block_probe:
            await never.wait()
        return True

    services = _services(
        checks=(
            RuntimeDependency(
                "identity_provider",
                dependency,
                dependency,
            ),
        )
    )
    provider = RuntimeProvider(
        lambda: services,
        readiness_timeout_seconds=0.02,
        readiness_cache_seconds=0,
    )
    await provider.initialize()
    assert await provider.get() is services
    block_probe = True
    started = time.monotonic()

    report = await provider.readiness(force=True)

    assert time.monotonic() - started < 0.15
    assert report.ready is False
    assert report.dependencies["identity_provider"] == "timeout"
    assert await provider.get() is services
    await provider.close()


@pytest.mark.asyncio
async def test_shutdown_runs_hooks_on_the_request_event_loop_once() -> None:
    main_loop = asyncio.get_running_loop()
    request_loop = asyncio.new_event_loop()
    request_loop_started = threading.Event()
    close_loops: list[asyncio.AbstractEventLoop] = []

    async def close_http() -> None:
        close_loops.append(asyncio.get_running_loop())

    services = _services(close_hooks=(RuntimeCloseHook("provider_http", close_http),))
    provider = RuntimeProvider(lambda: services)
    await provider.initialize()

    def run_request_loop() -> None:
        asyncio.set_event_loop(request_loop)
        request_loop_started.set()
        request_loop.run_forever()

    thread = threading.Thread(target=run_request_loop)
    thread.start()
    request_loop_started.wait(timeout=1)
    try:
        get_future = asyncio.run_coroutine_threadsafe(
            provider.get(),
            request_loop,
        )
        assert get_future.result(timeout=1) is services

        await provider.close()
        await provider.close()

        assert close_loops == [request_loop]
        assert close_loops[0] is not main_loop
    finally:
        request_loop.call_soon_threadsafe(request_loop.stop)
        thread.join(timeout=1)
        request_loop.close()


@pytest.mark.asyncio
async def test_first_request_binds_shared_checks_to_service_loop() -> None:
    main_loop = asyncio.get_running_loop()
    service_loop = asyncio.new_event_loop()
    service_loop_started = threading.Event()
    first_probe_started = threading.Event()
    release_first_probe = asyncio.Event()
    startup_loops: list[asyncio.AbstractEventLoop] = []
    service_loops: list[asyncio.AbstractEventLoop] = []
    oidc_like_lock = asyncio.Lock()

    async def startup_check() -> bool:
        startup_loops.append(asyncio.get_running_loop())
        return True

    async def shared_service_check() -> bool:
        async with oidc_like_lock:
            service_loops.append(asyncio.get_running_loop())
            if len(service_loops) == 1:
                first_probe_started.set()
                await release_first_probe.wait()
            return True

    services = _services(
        checks=(
            RuntimeDependency(
                "identity_provider",
                shared_service_check,
                startup_check,
            ),
        )
    )
    provider = RuntimeProvider(
        lambda: services,
        readiness_timeout_seconds=1,
        readiness_cache_seconds=0,
    )
    await provider.initialize()

    assert startup_loops == [main_loop]
    assert service_loops == []

    def run_service_loop() -> None:
        asyncio.set_event_loop(service_loop)
        service_loop_started.set()
        service_loop.run_forever()

    thread = threading.Thread(target=run_service_loop)
    thread.start()
    service_loop_started.wait(timeout=1)
    try:
        get_future = asyncio.run_coroutine_threadsafe(
            provider.get(),
            service_loop,
        )
        assert first_probe_started.wait(timeout=1)
        assert not get_future.done()

        service_loop.call_soon_threadsafe(release_first_probe.set)
        assert get_future.result(timeout=1) is services

        report = await provider.readiness(force=True)

        assert report.ready is True
        assert startup_loops == [main_loop]
        assert service_loops == [service_loop, service_loop]
        await provider.close()
    finally:
        if provider.state is not RuntimeState.CLOSED:
            await provider.close()
        service_loop.call_soon_threadsafe(service_loop.stop)
        thread.join(timeout=1)
        service_loop.close()


@pytest.mark.asyncio
async def test_inflight_readiness_is_invalidated_when_close_begins() -> None:
    service_loop = asyncio.new_event_loop()
    service_loop_started = threading.Event()
    probe_started = threading.Event()
    release_probe = threading.Event()
    block_probe = False

    async def startup_check() -> bool:
        return True

    async def service_check() -> bool:
        if block_probe:
            probe_started.set()
            while not release_probe.is_set():
                await asyncio.sleep(0.005)
        return True

    services = _services(
        checks=(
            RuntimeDependency(
                "principal_store",
                service_check,
                startup_check,
            ),
        )
    )
    provider = RuntimeProvider(
        lambda: services,
        readiness_timeout_seconds=1,
        readiness_cache_seconds=0,
        shutdown_timeout_seconds=1,
    )
    await provider.initialize()

    def run_service_loop() -> None:
        asyncio.set_event_loop(service_loop)
        service_loop_started.set()
        service_loop.run_forever()

    thread = threading.Thread(target=run_service_loop)
    thread.start()
    service_loop_started.wait(timeout=1)
    try:
        get_future = asyncio.run_coroutine_threadsafe(
            provider.get(),
            service_loop,
        )
        assert get_future.result(timeout=1) is services

        block_probe = True
        readiness_task = asyncio.create_task(provider.readiness(force=True))
        deadline = time.monotonic() + 1
        while not probe_started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert probe_started.is_set()

        close_task = asyncio.create_task(provider.close())
        deadline = time.monotonic() + 1
        while provider.state is not RuntimeState.CLOSING and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert provider.state is RuntimeState.CLOSING

        release_probe.set()
        report = await readiness_task
        await close_task

        assert report.ready is False
        assert report.state in {"closing", "closed"}
        assert provider.state is RuntimeState.CLOSED
        final_report = await provider.readiness()
        assert final_report.ready is False
        assert final_report.state == "closed"
        assert final_report.dependencies == {"runtime": "closed"}
    finally:
        release_probe.set()
        if provider.state is not RuntimeState.CLOSED:
            await provider.close()
        service_loop.call_soon_threadsafe(service_loop.stop)
        thread.join(timeout=1)
        service_loop.close()


@pytest.mark.asyncio
async def test_production_runtime_probes_and_closes_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Verifier:
        def _validated_oidc_issuer(self) -> str:
            return "https://idp.example.test"

        def _validated_oidc_audience(self) -> str:
            return "axon"

        async def _fetch_valid_jwks(self) -> dict[str, list[dict[str, str]]]:
            events.append("startup_jwks")
            return {"keys": [{"kid": "key-1"}]}

        async def _get_jwks(self) -> dict[str, list[dict[str, str]]]:
            events.append("service_jwks")
            return {"keys": [{"kid": "key-1"}]}

    class Persistence:
        enabled = True

        async def health_status(self) -> dict[str, Any]:
            return {"enabled": True, "reachable": True}

    class HttpClient:
        async def close(self) -> None:
            events.append("provider_http")

    class OTLP:
        def shutdown(self) -> None:
            events.append("otlp")

    policies = [
        {
            "name": "deny-agentcore-writes",
            "policy_text": (
                'forbid(principal, action == Action::"write", resource);'
            ),
            "mode": "ENFORCE",
        }
    ]
    components = SimpleNamespace(
        gateway_agent=SimpleNamespace(_otlp_exporter=OTLP()),
        oidc_service=Verifier(),
        principal_resolver=SimpleNamespace(),
        project_resolver=SimpleNamespace(),
        policies=policies,
        persistence=Persistence(),
        multi_factory=SimpleNamespace(_http_client=HttpClient()),
    )
    monkeypatch.setattr(
        "src.gateway.bootstrap.build_gateway_components",
        lambda: components,
    )

    services = build_runtime_services()
    assert isinstance(services.policy_service, CedarPolicyService)
    assert services.policy_service._policies is policies
    assert services.policy_service._persistence is components.persistence
    policy_context = RequestContext(
        user_id="principal-123",
        project_id="project-a",
        roles=["tenant_member"],
        scopes=["inference.invoke"],
        auth_method=AuthMethod.OIDC_JWT,
        tenant_id="tenant-a",
    )
    assert (
        await services.policy_service.evaluate(
            policy_context,
            "post",
            "/v1/chat/completions",
        )
        == "DENY"
    )
    startup_report = await services.check_startup_readiness(0.1)
    service_report = await services.check_readiness(0.1)
    await services.close(0.1)

    assert startup_report.ready is True
    assert startup_report.dependencies == {
        "runtime": "ready",
        "identity_provider": "ready",
        "principal_store": "ready",
    }
    assert service_report.ready is True
    assert service_report.dependencies == {
        "runtime": "ready",
        "identity_provider": "ready",
        "principal_store": "ready",
    }
    assert sorted(events) == [
        "otlp",
        "provider_http",
        "service_jwks",
        "startup_jwks",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready", "expected_status"),
    [(True, 200), (False, 503)],
)
async def test_readiness_route_uses_http_status_not_liveness(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    expected_status: int,
) -> None:
    class Adapter:
        async def readiness(self) -> dict[str, Any]:
            return {
                "status": "ready" if ready else "not_ready",
                "ready": ready,
                "state": "ready",
                "dependencies": {
                    "runtime": "ready",
                    "principal_store": "ready" if ready else "unavailable",
                },
            }

    monkeypatch.setattr(agentcore_agent, "_adapter", Adapter())

    response = await agentcore_agent.readiness(None)

    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body)["ready"] is ready


def test_agentcore_sdk_registers_distinct_ready_route() -> None:
    registered = agentcore_agent.app.routes
    if isinstance(registered, dict):
        routes = {path: {"GET"} for path in registered}
    else:
        routes = {getattr(route, "path", None): getattr(route, "methods", set()) for route in registered}

    assert "/ping" in routes
    assert "/ready" in routes
    assert "GET" in routes["/ready"]


@pytest.mark.asyncio
async def test_agentcore_lifespan_initializes_and_closes_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Adapter:
        async def initialize(self) -> None:
            events.append("initialize")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(agentcore_agent, "_adapter", Adapter())

    async with agentcore_agent._lifespan(None):
        events.append("serving")

    assert events == ["initialize", "serving", "close"]


@pytest.mark.asyncio
async def test_agentcore_lifespan_closes_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Adapter:
        async def initialize(self) -> None:
            events.append("initialize")
            raise RuntimeError("startup failed")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(agentcore_agent, "_adapter", Adapter())

    with pytest.raises(RuntimeError, match="startup failed"):
        async with agentcore_agent._lifespan(None):
            pytest.fail("lifespan yielded after failed initialization")

    assert events == ["initialize", "close"]
