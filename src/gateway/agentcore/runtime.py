"""Concurrency-safe, event-loop-safe AgentCore service initialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.gateway.auth.principal import PrincipalResolver
from src.gateway.models import RequestContext


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
    ) -> dict[str, Any]: ...


class OIDCTokenVerifier(Protocol):
    """Cryptographic verifier for runtime-forwarded bearer tokens."""

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None: ...


@dataclass(frozen=True)
class RuntimeServices:
    """AgentCore dependencies built as one immutable runtime unit."""

    gateway: GatewayProtocol
    token_verifier: OIDCTokenVerifier
    principal_resolver: PrincipalResolver


def build_runtime_services() -> RuntimeServices:
    """Build production services on a worker thread.

    ``build_gateway_components`` currently performs synchronous bootstrap work
    using ``asyncio.run``. ``RuntimeProvider`` always calls this function via
    ``asyncio.to_thread``, so it is safe when AgentCore invokes us from its
    active worker event loop.
    """
    from src.gateway.bootstrap import build_gateway_components

    components = build_gateway_components()
    if components.oidc_service is None:
        raise RuntimeError("AgentCore OIDC verifier is not configured")
    if components.principal_resolver is None:
        raise RuntimeError("canonical principal resolution is not configured")
    return RuntimeServices(
        gateway=components.gateway_agent,
        token_verifier=components.oidc_service,
        principal_resolver=components.principal_resolver,
    )


class RuntimeProvider:
    """Initialize services once, share concurrent work, and retry failures."""

    def __init__(
        self,
        factory: Callable[[], RuntimeServices] = build_runtime_services,
    ) -> None:
        self._factory = factory
        self._runtime: RuntimeServices | None = None
        self._initialization: asyncio.Task[RuntimeServices] | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> RuntimeServices:
        """Return initialized services without blocking the active event loop."""
        if self._runtime is not None:
            return self._runtime

        async with self._lock:
            if self._runtime is not None:
                return self._runtime
            if self._initialization is None:
                self._initialization = asyncio.create_task(asyncio.to_thread(self._factory))
            initialization = self._initialization

        try:
            runtime = await asyncio.shield(initialization)
        except Exception:
            async with self._lock:
                if self._initialization is initialization:
                    self._initialization = None
            raise

        async with self._lock:
            if self._runtime is None:
                self._runtime = runtime
            if self._initialization is initialization:
                self._initialization = None
            return self._runtime
