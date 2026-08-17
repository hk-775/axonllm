"""Lifecycle, readiness, and shutdown coverage for AgentCore runtime services."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agentcore_agent
import src.gateway.agentcore.runtime as runtime_module
from src.gateway.agentcore.runtime import (
    INITIALIZATION_TIMEOUT_EXIT_CODE,
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
from src.gateway.model_registry import ModelRegistry
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


_REPO = Path(__file__).resolve().parents[2]


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
async def test_degraded_dependency_keeps_lkg_runtime_ready() -> None:
    async def degraded() -> str:
        return "degraded"

    report = await _services(
        checks=(
            RuntimeDependency(
                "routing_configuration",
                degraded,
                degraded,
            ),
        )
    ).check_readiness(0.1)

    assert report.ready is True
    assert report.as_dict()["status"] == "degraded"
    assert report.dependencies["routing_configuration"] == "degraded"


@pytest.mark.asyncio
async def test_initialization_timeout_is_bounded_and_remains_fail_closed() -> None:
    release_factory = threading.Event()
    timeout_fired = threading.Event()
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
        _initialization_timeout_handler=lambda _: timeout_fired.set(),
    )
    started = time.monotonic()

    with pytest.raises(
        RuntimeInitializationError,
        match="initialization timed out",
    ):
        await provider.initialize()

    assert time.monotonic() - started < 0.15
    assert timeout_fired.wait(timeout=0.1)
    assert provider.state is RuntimeState.FAILED
    with pytest.raises(RuntimeUnavailableError):
        await provider.get()

    release_factory.set()
    await provider.close()
    assert provider.state is RuntimeState.CLOSED
    assert close_calls == 1


def test_initialization_watchdog_terminates_cancellation_resistant_process() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import threading

        from src.gateway.agentcore.runtime import RuntimeProvider

        blocker = threading.Event()

        def factory():
            blocker.wait()

        async def main():
            provider = RuntimeProvider(
                factory,
                initialization_timeout_seconds=0.05,
            )
            initialization = asyncio.create_task(provider.initialize())
            await asyncio.sleep(0.01)
            initialization.cancel()
            try:
                await initialization
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(10)

        asyncio.run(main())
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3,
    )

    assert completed.returncode == INITIALIZATION_TIMEOUT_EXIT_CODE


def test_dependency_timeout_keeps_process_watchdog_armed() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import threading

        from src.gateway.agentcore.runtime import (
            RuntimeDependency,
            RuntimeInitializationError,
            RuntimeProvider,
            RuntimeServices,
        )

        blocker = threading.Event()

        async def blocked_dependency():
            return await asyncio.to_thread(blocker.wait)

        def factory():
            return RuntimeServices(
                gateway=object(),
                token_verifier=object(),
                principal_resolver=object(),
                project_resolver=object(),
                readiness_checks=(
                    RuntimeDependency(
                        "principal_store",
                        blocked_dependency,
                        blocked_dependency,
                    ),
                ),
            )

        async def main():
            provider = RuntimeProvider(
                factory,
                initialization_timeout_seconds=0.1,
                readiness_timeout_seconds=0.02,
            )
            try:
                await provider.initialize()
            except RuntimeInitializationError:
                pass
            await asyncio.sleep(10)

        asyncio.run(main())
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3,
    )

    assert completed.returncode == INITIALIZATION_TIMEOUT_EXIT_CODE


def test_cancelled_waiter_and_shutdown_cannot_disarm_dependency_timeout() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import threading

        from src.gateway.agentcore.runtime import (
            RuntimeDependency,
            RuntimeProvider,
            RuntimeServices,
        )

        blocker = threading.Event()
        dependency_started = threading.Event()

        async def blocked_dependency():
            dependency_started.set()
            return await asyncio.to_thread(blocker.wait)

        def factory():
            return RuntimeServices(
                gateway=object(),
                token_verifier=object(),
                principal_resolver=object(),
                project_resolver=object(),
                readiness_checks=(
                    RuntimeDependency(
                        "principal_store",
                        blocked_dependency,
                        blocked_dependency,
                    ),
                ),
            )

        async def main():
            provider = RuntimeProvider(
                factory,
                initialization_timeout_seconds=0.15,
                readiness_timeout_seconds=0.02,
            )
            initialization = asyncio.create_task(provider.initialize())
            await asyncio.to_thread(dependency_started.wait)
            initialization.cancel()
            try:
                await initialization
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.04)
            await provider.close()
            await asyncio.sleep(10)

        asyncio.run(main())
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3,
    )

    assert completed.returncode == INITIALIZATION_TIMEOUT_EXIT_CODE


@pytest.mark.asyncio
async def test_successful_initialization_disarms_process_watchdog() -> None:
    timeout_fired = threading.Event()
    provider = RuntimeProvider(
        _services,
        initialization_timeout_seconds=0.02,
        _initialization_timeout_handler=lambda _: timeout_fired.set(),
    )

    await provider.initialize()
    await asyncio.sleep(0.05)

    assert timeout_fired.is_set() is False
    await provider.close()


@pytest.mark.asyncio
async def test_runtime_cannot_publish_ready_after_initialization_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_started = threading.Event()
    release_factory = threading.Event()
    timeout_fired = threading.Event()

    class DormantTimer:
        daemon = False

        def __init__(self, _timeout: float, _callback: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            pass

    def factory() -> RuntimeServices:
        factory_started.set()
        release_factory.wait()
        return _services()

    monkeypatch.setattr(runtime_module.threading, "Timer", DormantTimer)
    provider = RuntimeProvider(
        factory,
        initialization_timeout_seconds=0.05,
        _initialization_timeout_handler=lambda _: timeout_fired.set(),
    )
    initialization = asyncio.create_task(provider.initialize())
    assert await asyncio.to_thread(factory_started.wait, 0.1)

    await provider._lifecycle_lock.acquire()
    try:
        release_factory.set()
        await asyncio.sleep(0.06)
        assert timeout_fired.is_set() is False
    finally:
        provider._lifecycle_lock.release()

    with pytest.raises(
        RuntimeInitializationError,
        match="initialization timed out",
    ):
        await initialization

    assert timeout_fired.is_set()
    assert provider.state is RuntimeState.FAILED
    with pytest.raises(RuntimeUnavailableError):
        await provider.get()
    await provider.close()


@pytest.mark.asyncio
async def test_cancelled_shutdown_retains_late_initialization_cleanup() -> None:
    factory_started = threading.Event()
    release_factory = threading.Event()
    runtime_closed = asyncio.Event()
    timeout_fired = threading.Event()

    async def close_runtime() -> None:
        runtime_closed.set()

    def factory() -> RuntimeServices:
        factory_started.set()
        release_factory.wait()
        return _services(
            close_hooks=(RuntimeCloseHook("runtime", close_runtime),),
        )

    provider = RuntimeProvider(
        factory,
        initialization_timeout_seconds=0.2,
        shutdown_timeout_seconds=1,
        _initialization_timeout_handler=lambda _: timeout_fired.set(),
    )
    initialization = asyncio.create_task(provider.initialize())
    assert await asyncio.to_thread(factory_started.wait, 0.1)

    shutdown = asyncio.create_task(provider.close())
    for _ in range(20):
        if provider.state is RuntimeState.CLOSING:
            break
        await asyncio.sleep(0.005)
    assert provider.state is RuntimeState.CLOSING
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    release_factory.set()
    await asyncio.wait_for(runtime_closed.wait(), timeout=0.1)
    result = await asyncio.gather(initialization, return_exceptions=True)
    assert isinstance(result[0], RuntimeInitializationError)
    await asyncio.sleep(0.25)

    assert timeout_fired.is_set() is False


@pytest.mark.asyncio
async def test_cancelled_shutdown_retains_ready_runtime_cleanup() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_calls = 0

    async def close_runtime() -> None:
        nonlocal close_calls
        close_calls += 1
        close_started.set()
        await release_close.wait()

    provider = RuntimeProvider(
        lambda: _services(
            close_hooks=(RuntimeCloseHook("runtime", close_runtime),),
        ),
        shutdown_timeout_seconds=1,
    )
    await provider.initialize()

    shutdown = asyncio.create_task(provider.close())
    await asyncio.wait_for(close_started.wait(), timeout=0.1)
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert provider.state is RuntimeState.CLOSING
    release_close.set()
    for _ in range(20):
        if provider.state is RuntimeState.CLOSED:
            break
        await asyncio.sleep(0.005)

    assert provider.state is RuntimeState.CLOSED
    assert close_calls == 1
    await provider.close()
    assert close_calls == 1


def test_shutdown_watchdog_terminates_stuck_cleanup_worker() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import threading

        from src.gateway.agentcore.runtime import (
            RuntimeCloseHook,
            RuntimeProvider,
            RuntimeServices,
        )

        blocker = threading.Event()

        async def blocked_close():
            await asyncio.to_thread(blocker.wait)

        def factory():
            return RuntimeServices(
                gateway=object(),
                token_verifier=object(),
                principal_resolver=object(),
                project_resolver=object(),
                close_hooks=(RuntimeCloseHook("blocked", blocked_close),),
            )

        async def main():
            provider = RuntimeProvider(
                factory,
                shutdown_timeout_seconds=0.05,
            )
            await provider.initialize()
            await provider.close()
            await asyncio.sleep(10)

        asyncio.run(main())
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3,
    )

    assert completed.returncode == INITIALIZATION_TIMEOUT_EXIT_CODE


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

        async def get_tenant_cedar_policy_version(
            self,
            tenant_id: str,
        ) -> int:
            assert tenant_id == "tenant-a"
            return 1

        async def load_tenant_cedar_policies_or_none(
            self,
            tenant_id: str,
        ) -> list[dict[str, str]]:
            assert tenant_id == "tenant-a"
            return policies

    class HttpClient:
        async def close(self) -> None:
            events.append("provider_http")

    class OTLP:
        def shutdown(self) -> None:
            events.append("otlp")

    class TraceForwarder:
        async def close(self) -> None:
            events.append("trace_forwarder")

    class HealthMonitor:
        async def stop(self) -> None:
            events.append("health_monitor")

    class EventDispatcher:
        outbox_enabled = True
        worker_running = False

        def set_destination_refresher(self, refresher) -> None:
            self.refresher = refresher

        async def check_readiness(self) -> bool:
            events.append("event_outbox_ready")
            return True

        async def start(self) -> None:
            events.append("event_outbox_start")
            self.worker_running = True

        async def stop(self) -> None:
            events.append("event_outbox_stop")
            self.worker_running = False

    class QueryWorker:
        running = False

        async def start(self) -> None:
            events.append("query_reconciliation_start")
            self.running = True

        async def stop(self) -> None:
            events.append("query_reconciliation_stop")
            self.running = False

    policies = [
        {
            "name": "deny-agentcore-writes",
            "policy_text": (
                'forbid(principal, action == Action::"write", resource);'
            ),
            "mode": "ENFORCE",
            "tenant_id": "tenant-a",
        }
    ]
    query_service = SimpleNamespace()
    components = SimpleNamespace(
        gateway_agent=SimpleNamespace(
            _otlp_exporter=OTLP(),
            _trace_forwarder=TraceForwarder(),
        ),
        oidc_service=Verifier(),
        principal_resolver=SimpleNamespace(),
        project_resolver=SimpleNamespace(),
        projects={},
        user_configs={},
        cost_tracker=SimpleNamespace(),
        policy_resolver=None,
        region_router=SimpleNamespace(
            config=SimpleNamespace(revision=0)
        ),
        health_monitor=HealthMonitor(),
        event_dispatcher=EventDispatcher(),
            policies=policies,
            persistence=Persistence(),
            registry=ModelRegistry.from_config(
                {
                    "models": [
                        {
                            "name": "runtime-test",
                            "description": "runtime test",
                            "routing_strategy": "round-robin",
                            "providers": [
                                {
                                    "provider": "openai",
                                    "model_id": "runtime-test",
                                }
                            ],
                        }
                    ]
                }
            ),
            multi_factory=SimpleNamespace(_http_client=HttpClient()),
        audit_trail=SimpleNamespace(durable_enabled=True),
        query_service=query_service,
        query_reconciliation_worker=QueryWorker(),
    )
    monkeypatch.setattr(
        "src.gateway.bootstrap.build_gateway_components",
        lambda: components,
    )

    services = build_runtime_services()
    assert services.query_service is query_service
    assert services.project_config_store is components.project_resolver
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
        "routing_configuration": "ready",
        "security_event_outbox": "ready",
        "query_reconciliation": "ready",
    }
    assert service_report.ready is True
    assert service_report.dependencies == {
        "runtime": "ready",
        "identity_provider": "ready",
        "principal_store": "ready",
        "routing_configuration": "ready",
        "security_event_outbox": "ready",
        "query_reconciliation": "ready",
    }
    assert sorted(events) == [
        "event_outbox_ready",
        "event_outbox_ready",
        "event_outbox_start",
        "event_outbox_stop",
        "health_monitor",
        "otlp",
        "provider_http",
        "query_reconciliation_start",
        "query_reconciliation_stop",
        "service_jwks",
        "startup_jwks",
        "trace_forwarder",
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
