"""Bounded lifecycle and dependency readiness for AgentCore services."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from src.gateway.auth.principal import PrincipalResolver
from src.gateway.auth.project_repository import ProjectResolver
from src.gateway.models import RequestContext

if TYPE_CHECKING:
    from src.gateway.query.service import QueryService

logger = logging.getLogger(__name__)

DEFAULT_INITIALIZATION_TIMEOUT_SECONDS = 60.0
DEFAULT_READINESS_TIMEOUT_SECONDS = 5.0
DEFAULT_READINESS_CACHE_SECONDS = 5.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached task outcome without extending a deadline."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def _cancel_detached_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_background_task)


class GatewayProtocol(Protocol):
    """Gateway operations used by the AgentCore adapter."""

    async def handle_chat_completion(
        self,
        request_data: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]: ...

    async def handle_list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        authorized_project: Any | None = None,
    ) -> dict[str, Any]: ...


class OIDCTokenVerifier(Protocol):
    """Cryptographic verifier for runtime-forwarded bearer tokens."""

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None: ...


class PolicyService(Protocol):
    """Policy evaluation used by the AgentCore adapter."""

    async def evaluate(
        self,
        context: RequestContext,
        action: str,
        resource: str,
    ) -> str: ...


class ConfigSync(Protocol):
    """Request-path fleet convergence used by both runtime front doors."""

    async def refresh_if_stale(self) -> bool: ...


@dataclass(frozen=True)
class RuntimeDependency:
    """One bounded dependency check required for runtime readiness."""

    name: str
    check: Callable[[], Awaitable[bool]]
    startup_check: Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class RuntimeCloseHook:
    """One asynchronous cleanup operation owned by the runtime."""

    name: str
    close: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class RuntimeReadiness:
    """Sanitized runtime and dependency readiness."""

    ready: bool
    state: str
    dependencies: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "ready": self.ready,
            "state": self.state,
            "dependencies": dict(self.dependencies),
        }


@dataclass(frozen=True)
class RuntimeServices:
    """AgentCore dependencies built and closed as one runtime unit."""

    gateway: GatewayProtocol
    token_verifier: OIDCTokenVerifier
    principal_resolver: PrincipalResolver
    project_resolver: ProjectResolver
    query_service: QueryService | None = None
    policy_service: PolicyService | None = None
    config_sync: ConfigSync | None = None
    readiness_checks: tuple[RuntimeDependency, ...] = field(default_factory=tuple)
    close_hooks: tuple[RuntimeCloseHook, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        readiness_names = [check.name for check in self.readiness_checks]
        close_names = [hook.name for hook in self.close_hooks]
        for kind, names in (
            ("readiness check", readiness_names),
            ("close hook", close_names),
        ):
            if any(not name or name != name.strip() for name in names):
                raise ValueError(f"AgentCore {kind} names must be non-empty")
            if len(names) != len(set(names)):
                raise ValueError(f"AgentCore {kind} names must be unique")
        if "runtime" in readiness_names:
            raise ValueError("AgentCore readiness check name 'runtime' is reserved")

    async def _check_readiness(
        self,
        timeout_seconds: float,
        *,
        startup: bool,
    ) -> RuntimeReadiness:
        dependencies = {"runtime": "ready"}
        if not self.readiness_checks:
            return RuntimeReadiness(True, RuntimeState.READY.value, dependencies)

        async def _run(check: RuntimeDependency) -> str:
            try:
                callback = check.startup_check if startup else check.check
                return "ready" if await callback() else "unavailable"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "AgentCore readiness dependency failed: %s",
                    check.name,
                    exc_info=True,
                )
                return "unavailable"

        tasks = {check.name: asyncio.create_task(_run(check)) for check in self.readiness_checks}
        try:
            done, _ = await asyncio.wait(
                tasks.values(),
                timeout=timeout_seconds,
            )
        except BaseException:
            _cancel_detached_tasks(list(tasks.values()))
            raise
        for name, task in tasks.items():
            if task in done and not task.cancelled():
                dependencies[name] = task.result()
            else:
                dependencies[name] = "timeout"
                _cancel_detached_tasks([task])

        ready = all(status == "ready" for status in dependencies.values())
        return RuntimeReadiness(ready, RuntimeState.READY.value, dependencies)

    async def check_startup_readiness(
        self,
        timeout_seconds: float,
    ) -> RuntimeReadiness:
        """Probe dependencies without touching service-loop-owned async state."""
        return await self._check_readiness(timeout_seconds, startup=True)

    async def check_readiness(self, timeout_seconds: float) -> RuntimeReadiness:
        """Probe shared dependencies on the authenticated-handler service loop."""
        return await self._check_readiness(timeout_seconds, startup=False)

    async def close(self, timeout_seconds: float) -> None:
        """Run all registered cleanup hooks within one shared deadline."""
        if not self.close_hooks:
            return

        async def _run(hook: RuntimeCloseHook) -> None:
            try:
                await hook.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "AgentCore shutdown hook failed: %s",
                    hook.name,
                    exc_info=True,
                )

        tasks = [asyncio.create_task(_run(hook)) for hook in self.close_hooks]
        try:
            _, pending = await asyncio.wait(
                tasks,
                timeout=timeout_seconds,
            )
        except BaseException:
            _cancel_detached_tasks(tasks)
            raise
        if pending:
            logger.warning(
                "AgentCore shutdown timed out with %d cleanup operation(s) pending",
                len(pending),
            )
            _cancel_detached_tasks(list(pending))


class RuntimeState(str, Enum):
    NOT_INITIALIZED = "not_initialized"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


class RuntimeInitializationError(RuntimeError):
    """The runtime could not become ready within its startup contract."""


class RuntimeUnavailableError(RuntimeError):
    """The explicitly initialized runtime is not available for requests."""


class _DependenciesUnavailable(RuntimeError):
    def __init__(self, readiness: RuntimeReadiness) -> None:
        super().__init__("required AgentCore dependencies are unavailable")
        self.readiness = readiness


def _validate_duration(value: float, name: str, *, allow_zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite {qualifier} duration")
    return value


def build_runtime_services() -> RuntimeServices:
    """Build production services and their AgentCore lifecycle contracts."""
    from src.gateway.admin.webhook_routes import WebhookAPI
    from src.gateway.auth.cedar_policy import CedarPolicyService
    from src.gateway.bootstrap import build_gateway_components
    from src.gateway.config_sync import ConfigSyncService

    components = build_gateway_components()
    if components.oidc_service is None:
        raise RuntimeError("AgentCore OIDC verifier is not configured")
    if components.principal_resolver is None:
        raise RuntimeError("canonical principal resolution is not configured")
    if components.project_resolver is None:
        raise RuntimeError("tenant project resolution is not configured")
    config_sync = ConfigSyncService(
        projects=components.projects,
        user_configs=components.user_configs,
        cost_tracker=components.cost_tracker,
        persistence=components.persistence,
        policy_resolver=components.policy_resolver,
        region_config=components.region_router.config,
        health_monitor=components.health_monitor,
    )
    # AgentCore has no Starlette admin-route construction step. Constructing the
    # manager here installs the dispatcher's tenant destination refresh hook so
    # canonical security events still converge across runtime replicas.
    WebhookAPI(
        dispatcher=components.event_dispatcher,
        persistence=components.persistence,
    )

    async def _identity_provider_ready() -> bool:
        verifier = components.oidc_service
        issuer = getattr(verifier, "_validated_oidc_issuer", None)
        audience = getattr(verifier, "_validated_oidc_audience", None)
        get_jwks = getattr(verifier, "_get_jwks", None)
        if not callable(issuer) or not callable(audience) or not callable(get_jwks):
            return False
        if issuer() is None or audience() is None:
            return False
        return await get_jwks() is not None

    async def _identity_provider_startup_ready() -> bool:
        verifier = components.oidc_service
        issuer = getattr(verifier, "_validated_oidc_issuer", None)
        audience = getattr(verifier, "_validated_oidc_audience", None)
        fetch_jwks = getattr(verifier, "_fetch_valid_jwks", None)
        if not callable(issuer) or not callable(audience) or not callable(fetch_jwks):
            return False
        if issuer() is None or audience() is None:
            return False
        # Fetch through a request-local HTTP client without acquiring or populating
        # the verifier's service-loop-owned JWKS cache and asyncio lock.
        return await fetch_jwks() is not None

    async def _principal_store_ready() -> bool:
        status = await components.persistence.health_status()
        return status.get("enabled") is True and status.get("reachable") is True

    async def _event_outbox_startup_ready() -> bool:
        return await components.event_dispatcher.check_readiness()

    async def _event_outbox_ready() -> bool:
        dispatcher = components.event_dispatcher
        if dispatcher.outbox_enabled and not dispatcher.worker_running:
            await dispatcher.start()
        return await dispatcher.check_readiness()

    async def _close_provider_http() -> None:
        client = getattr(components.multi_factory, "_http_client", None)
        close = getattr(client, "close", None)
        if callable(close):
            await close()

    async def _shutdown_otlp() -> None:
        exporter = getattr(components.gateway_agent, "_otlp_exporter", None)
        shutdown = getattr(exporter, "shutdown", None)
        if callable(shutdown):
            await asyncio.to_thread(shutdown)

    # Bootstrap owns the canonical query repository, executor, and audit trail.
    query_service = getattr(components, "query_service", None)
    return RuntimeServices(
        gateway=components.gateway_agent,
        token_verifier=components.oidc_service,
        principal_resolver=components.principal_resolver,
        project_resolver=components.project_resolver,
        query_service=query_service,
        policy_service=CedarPolicyService(
            components.policies,
            persistence=components.persistence,
        ),
        config_sync=config_sync,
        readiness_checks=(
            RuntimeDependency(
                "identity_provider",
                _identity_provider_ready,
                _identity_provider_startup_ready,
            ),
            RuntimeDependency(
                "principal_store",
                _principal_store_ready,
                _principal_store_ready,
            ),
            RuntimeDependency(
                "security_event_outbox",
                _event_outbox_ready,
                _event_outbox_startup_ready,
            ),
        ),
        close_hooks=(
            RuntimeCloseHook(
                "spoke_health_monitor",
                components.health_monitor.stop,
            ),
            RuntimeCloseHook("provider_http", _close_provider_http),
            RuntimeCloseHook(
                "security_event_outbox",
                components.event_dispatcher.stop,
            ),
            RuntimeCloseHook("otlp", _shutdown_otlp),
        ),
    )


class RuntimeProvider:
    """Explicitly initialize, probe, share, and close AgentCore services."""

    def __init__(
        self,
        factory: Callable[[], RuntimeServices] = build_runtime_services,
        *,
        initialization_timeout_seconds: float = DEFAULT_INITIALIZATION_TIMEOUT_SECONDS,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        readiness_cache_seconds: float = DEFAULT_READINESS_CACHE_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._factory = factory
        self._initialization_timeout = _validate_duration(
            initialization_timeout_seconds,
            "initialization_timeout_seconds",
        )
        self._readiness_timeout = _validate_duration(
            readiness_timeout_seconds,
            "readiness_timeout_seconds",
        )
        self._readiness_cache = _validate_duration(
            readiness_cache_seconds,
            "readiness_cache_seconds",
            allow_zero=True,
        )
        self._shutdown_timeout = _validate_duration(
            shutdown_timeout_seconds,
            "shutdown_timeout_seconds",
        )
        self._runtime: RuntimeServices | None = None
        self._initialization: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]] | None = None
        self._state = RuntimeState.NOT_INITIALIZED
        self._last_readiness = RuntimeReadiness(
            False,
            self._state.value,
            {"runtime": self._state.value},
        )
        self._last_readiness_at = 0.0
        self._last_service_readiness_at = 0.0
        self._service_loop_initialized = False
        self._lifecycle_epoch = 0
        self._lifecycle_lock = asyncio.Lock()
        self._startup_readiness_lock = asyncio.Lock()
        self._readiness_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._service_loop: asyncio.AbstractEventLoop | None = None
        self._service_loop_lock = threading.Lock()

    @property
    def state(self) -> RuntimeState:
        with self._state_lock:
            return self._state

    async def _build_and_check(
        self,
    ) -> tuple[RuntimeServices, RuntimeReadiness]:
        # Gateway bootstrap uses asyncio.run internally, so it must not execute
        # on AgentCore's active ASGI event loop.
        runtime = await asyncio.to_thread(self._factory)
        if not isinstance(runtime, RuntimeServices):
            raise TypeError("AgentCore runtime factory returned an invalid service unit")
        try:
            readiness = await runtime.check_startup_readiness(self._readiness_timeout)
            if not readiness.ready:
                raise _DependenciesUnavailable(readiness)
            return runtime, readiness
        except BaseException:
            await runtime.close(self._shutdown_timeout)
            raise

    async def initialize(self) -> RuntimeServices:
        """Build and verify services before the application accepts traffic."""
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state is RuntimeState.READY and self._runtime is not None:
                    return self._runtime
                if self._state in {RuntimeState.CLOSING, RuntimeState.CLOSED}:
                    raise RuntimeInitializationError("AgentCore runtime is closed")
                if self._initialization is None:
                    self._initialization = asyncio.create_task(self._build_and_check())
                initialization = self._initialization
                self._state = RuntimeState.INITIALIZING

        try:
            runtime, readiness = await asyncio.wait_for(
                asyncio.shield(initialization),
                timeout=self._initialization_timeout,
            )
        except TimeoutError as exc:
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                {"runtime": "initialization_timeout"},
            )
            await self._record_initialization_failure(
                initialization,
                failure,
            )
            raise RuntimeInitializationError("AgentCore runtime initialization timed out") from exc
        except _DependenciesUnavailable as exc:
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                dict(exc.readiness.dependencies),
            )
            await self._record_initialization_failure(
                initialization,
                failure,
            )
            raise RuntimeInitializationError("AgentCore runtime dependencies are unavailable") from exc
        except Exception as exc:
            failure = RuntimeReadiness(
                False,
                RuntimeState.FAILED.value,
                {"runtime": "initialization_failed"},
            )
            await self._record_initialization_failure(
                initialization,
                failure,
            )
            raise RuntimeInitializationError("AgentCore runtime initialization failed") from exc

        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state in {RuntimeState.CLOSING, RuntimeState.CLOSED}:
                    raise RuntimeInitializationError("AgentCore runtime is closed")
                self._runtime = runtime
                self._initialization = None
                self._state = RuntimeState.READY
                self._last_readiness = readiness
                self._last_readiness_at = time.monotonic()
                self._last_service_readiness_at = 0.0
                self._service_loop_initialized = False
                return runtime

    async def _record_initialization_failure(
        self,
        initialization: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]],
        readiness: RuntimeReadiness,
    ) -> None:
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._initialization is not initialization:
                    return
                if self._state not in {RuntimeState.CLOSING, RuntimeState.CLOSED}:
                    self._state = RuntimeState.FAILED
                    self._last_readiness = readiness
                    self._last_readiness_at = time.monotonic()
                if initialization.done():
                    self._initialization = None

    async def get(self) -> RuntimeServices:
        """Return only services that completed explicit startup initialization."""
        with self._state_lock:
            runtime = self._runtime
            if self._state is not RuntimeState.READY or runtime is None:
                raise RuntimeUnavailableError("AgentCore runtime is not ready")

        current_loop = asyncio.get_running_loop()
        with self._service_loop_lock:
            if self._service_loop is None:
                self._service_loop = current_loop
            elif self._service_loop is not current_loop:
                raise RuntimeUnavailableError("AgentCore runtime request loop changed")

        with self._state_lock:
            if self._state is not RuntimeState.READY or self._runtime is not runtime:
                raise RuntimeUnavailableError("AgentCore runtime is not ready")
            service_loop_initialized = self._service_loop_initialized

        if not service_loop_initialized:
            readiness = await self._probe_service_readiness(
                runtime,
                activate=True,
            )
            if not readiness.ready:
                raise RuntimeUnavailableError("AgentCore runtime dependencies are not ready")

        with self._state_lock:
            if (
                self._state is not RuntimeState.READY
                or self._runtime is not runtime
                or not self._service_loop_initialized
            ):
                raise RuntimeUnavailableError("AgentCore runtime is not ready")
        return runtime

    def _unavailable_readiness_locked(self) -> RuntimeReadiness:
        dependencies = {"runtime": self._state.value}
        if self._state is RuntimeState.FAILED:
            dependencies = dict(self._last_readiness.dependencies)
        return RuntimeReadiness(
            False,
            self._state.value,
            dependencies,
        )

    def _commit_readiness(
        self,
        runtime: RuntimeServices,
        lifecycle_epoch: int,
        readiness: RuntimeReadiness,
        *,
        service_probe: bool,
        activate: bool = False,
    ) -> RuntimeReadiness:
        with self._state_lock:
            if (
                self._lifecycle_epoch != lifecycle_epoch
                or self._state is not RuntimeState.READY
                or self._runtime is not runtime
            ):
                return self._unavailable_readiness_locked()
            now = time.monotonic()
            self._last_readiness = readiness
            self._last_readiness_at = now
            if service_probe:
                self._last_service_readiness_at = now
            if activate and readiness.ready:
                self._service_loop_initialized = True
            return readiness

    async def _probe_startup_readiness(
        self,
        runtime: RuntimeServices,
        *,
        force: bool,
    ) -> RuntimeReadiness:
        async with self._startup_readiness_lock:
            with self._state_lock:
                if self._state is not RuntimeState.READY or self._runtime is not runtime:
                    return self._unavailable_readiness_locked()
                now = time.monotonic()
                if not force and now - self._last_readiness_at < self._readiness_cache:
                    return self._last_readiness
                lifecycle_epoch = self._lifecycle_epoch

            readiness = await runtime.check_startup_readiness(self._readiness_timeout)
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                readiness,
                service_probe=False,
            )

    async def _probe_service_readiness(
        self,
        runtime: RuntimeServices,
        *,
        force: bool = False,
        activate: bool = False,
    ) -> RuntimeReadiness:
        async with self._readiness_lock:
            with self._state_lock:
                if self._state is not RuntimeState.READY or self._runtime is not runtime:
                    return self._unavailable_readiness_locked()
                if activate and self._service_loop_initialized:
                    return RuntimeReadiness(
                        True,
                        RuntimeState.READY.value,
                        {"runtime": "ready"},
                    )
                now = time.monotonic()
                if (
                    not force
                    and self._last_service_readiness_at > 0
                    and now - self._last_service_readiness_at < self._readiness_cache
                ):
                    return self._last_readiness
                lifecycle_epoch = self._lifecycle_epoch

            readiness = await runtime.check_readiness(self._readiness_timeout)
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                readiness,
                service_probe=True,
                activate=activate,
            )

    async def readiness(self, *, force: bool = False) -> RuntimeReadiness:
        """Check dependencies on the loop that owns their shared async state."""
        with self._state_lock:
            runtime = self._runtime
            if self._state is not RuntimeState.READY or runtime is None:
                return self._unavailable_readiness_locked()
            lifecycle_epoch = self._lifecycle_epoch

        with self._service_loop_lock:
            service_loop = self._service_loop

        if service_loop is None:
            return await self._probe_startup_readiness(
                runtime,
                force=force,
            )

        current_loop = asyncio.get_running_loop()
        if service_loop is current_loop:
            return await self._probe_service_readiness(
                runtime,
                force=force,
            )
        if not service_loop.is_running():
            failure = RuntimeReadiness(
                False,
                RuntimeState.READY.value,
                {"runtime": "service_loop_unavailable"},
            )
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                failure,
                service_probe=True,
            )

        readiness_future = asyncio.run_coroutine_threadsafe(
            self._probe_service_readiness(runtime, force=force),
            service_loop,
        )
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(readiness_future),
                timeout=self._readiness_timeout + 0.25,
            )
        except TimeoutError:
            readiness_future.cancel()
            failure = RuntimeReadiness(
                False,
                RuntimeState.READY.value,
                {"runtime": "service_loop_timeout"},
            )
            return self._commit_readiness(
                runtime,
                lifecycle_epoch,
                failure,
                service_probe=True,
            )

    async def _close_runtime_with_lock(
        self,
        runtime: RuntimeServices,
        lock: asyncio.Lock,
    ) -> None:
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                async with lock:
                    await runtime.close(self._shutdown_timeout)
        except TimeoutError:
            logger.warning("AgentCore runtime cleanup did not finish before shutdown")

    async def _close_runtime(self, runtime: RuntimeServices) -> None:
        with self._service_loop_lock:
            service_loop = self._service_loop

        current_loop = asyncio.get_running_loop()
        if service_loop is None:
            await self._close_runtime_with_lock(
                runtime,
                self._startup_readiness_lock,
            )
            return
        if service_loop is current_loop:
            await self._close_runtime_with_lock(
                runtime,
                self._readiness_lock,
            )
            return
        if not service_loop.is_running():
            await runtime.close(self._shutdown_timeout)
            return

        close_future = asyncio.run_coroutine_threadsafe(
            self._close_runtime_with_lock(runtime, self._readiness_lock),
            service_loop,
        )
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(close_future),
                timeout=self._shutdown_timeout + 0.1,
            )
        except TimeoutError:
            close_future.cancel()
            logger.warning("AgentCore runtime cleanup did not finish on the request loop")

    def _close_late_initialization(
        self,
        initialization: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]],
    ) -> None:
        """Close a factory result that arrived after shutdown's deadline."""

        def _schedule_close(
            completed: asyncio.Task[tuple[RuntimeServices, RuntimeReadiness]],
        ) -> None:
            try:
                runtime, _ = completed.result()
            except BaseException:
                return
            loop = completed.get_loop()
            if loop.is_running():
                loop.create_task(runtime.close(self._shutdown_timeout))
            else:
                logger.warning("Late AgentCore initialization completed after its loop stopped")

        initialization.add_done_callback(_schedule_close)

    async def close(self) -> None:
        """Stop accepting services and close resources within a deadline."""
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state is RuntimeState.CLOSED:
                    return
                self._state = RuntimeState.CLOSING
                self._lifecycle_epoch += 1
                runtime = self._runtime
                initialization = self._initialization
                self._runtime = None
                self._initialization = None
                self._service_loop_initialized = False

        if runtime is None and initialization is not None:
            try:
                runtime, _ = await asyncio.wait_for(
                    asyncio.shield(initialization),
                    timeout=self._shutdown_timeout,
                )
            except TimeoutError:
                self._close_late_initialization(initialization)
                logger.warning("AgentCore initialization was still running at shutdown")
            except Exception:
                pass

        if runtime is not None:
            await self._close_runtime(runtime)

        async with self._lifecycle_lock:
            with self._state_lock:
                self._state = RuntimeState.CLOSED
                self._last_readiness = RuntimeReadiness(
                    False,
                    self._state.value,
                    {"runtime": self._state.value},
                )
                self._last_readiness_at = time.monotonic()
