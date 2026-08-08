"""Fail-closed AgentCore authorization and gateway dispatch."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from src.gateway.auth.authorization import (
    Action,
    AuthorizationDenied,
    ResourceRef,
    require_authorized,
)
from src.gateway.auth.project_repository import ProjectStoreUnavailable
from src.gateway.config_sync import RegionTopologyUnavailable
from src.gateway.models import Project

from .errors import AgentCoreAdapterError
from .identity import InvocationIdentity, resolve_invocation_identity
from .runtime import RuntimeReadiness, RuntimeServices
from .schemas import InvocationAction, parse_invocation_payload

logger = logging.getLogger(__name__)


class RuntimeProviderProtocol(Protocol):
    async def get(self) -> RuntimeServices: ...

    async def initialize(self) -> RuntimeServices: ...

    async def readiness(self, *, force: bool = False) -> RuntimeReadiness: ...

    async def close(self) -> None: ...


def _gateway_context(
    identity: InvocationIdentity,
    project: Project,
) -> dict[str, Any]:
    context = identity.request_context
    return {
        "user_id": context.user_id,
        "project_id": context.project_id,
        "roles": list(context.roles),
        "scopes": list(context.scopes),
        "tenant_id": context.tenant_id,
        "auth_method": context.auth_method.value,
        "principal_id": context.principal_id,
        "authorization_version": context.authorization_version,
        "authorized_project": project,
    }


async def _forward_stream(
    stream: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    async for chunk in stream:
        yield chunk


class AgentCoreAdapter:
    """Authorize trusted runtime identity before invoking gateway operations."""

    def __init__(self, runtime_provider: RuntimeProviderProtocol) -> None:
        self._runtime_provider = runtime_provider

    async def initialize(self) -> None:
        """Initialize and verify runtime dependencies before serving."""
        await self._runtime_provider.initialize()

    async def readiness(self) -> dict[str, Any]:
        """Return sanitized dependency readiness without authenticating a user."""
        return (await self._runtime_provider.readiness()).as_dict()

    async def close(self) -> None:
        """Close runtime-owned resources during graceful shutdown."""
        await self._runtime_provider.close()

    async def invoke(self, payload: Any, context: Any) -> Any:
        parsed = parse_invocation_payload(payload)
        if parsed.action is InvocationAction.HEALTH:
            return {
                "status": "alive",
                "ready": False,
                "dependencies": "not_checked",
            }

        try:
            runtime = await self._runtime_provider.get()
        except AgentCoreAdapterError:
            raise
        except Exception as exc:
            raise AgentCoreAdapterError(
                503,
                "gateway_initialization_failed",
                "Gateway initialization is temporarily unavailable.",
            ) from exc

        identity = await resolve_invocation_identity(
            context,
            runtime.token_verifier,
            runtime.principal_resolver,
        )
        if runtime.config_sync is not None:
            try:
                await runtime.config_sync.refresh_if_stale()
            except RegionTopologyUnavailable as exc:
                raise AgentCoreAdapterError(
                    503,
                    "region_topology_unavailable",
                    "Region routing configuration is temporarily unavailable.",
                ) from exc
            except Exception:
                logger.warning(
                    "AgentCore config refresh failed; using loaded config",
                    exc_info=True,
                )
        try:
            project = await runtime.project_resolver.resolve(
                identity.tenant_id,
                identity.project_id,
            )
        except ProjectStoreUnavailable as exc:
            raise AgentCoreAdapterError(
                503,
                "project_resolver_unavailable",
                "Project authorization is temporarily unavailable.",
            ) from exc
        if project is None:
            raise AgentCoreAdapterError(
                404,
                "resource_not_found",
                "Resource not found.",
            )

        action = (
            Action.MODEL_LIST
            if parsed.action is InvocationAction.LIST_MODELS
            else Action.INFERENCE_INVOKE
        )
        resource = ResourceRef(
            resource_type="project",
            resource_id=identity.project_id,
            tenant_id=project.tenant_id,
            project_id=identity.project_id,
        )
        try:
            require_authorized(identity.principal, action, resource)
        except AuthorizationDenied as exc:
            message = "Resource not found." if exc.decision.conceal_resource else "Action is not permitted."
            raise AgentCoreAdapterError(
                exc.decision.status_code,
                "authorization_denied",
                message,
            ) from exc

        if runtime.policy_service is not None:
            refresh = getattr(runtime.policy_service, "refresh_if_stale", None)
            if callable(refresh):
                try:
                    await refresh()
                except Exception:
                    logger.warning(
                        "AgentCore policy refresh failed; using compiled policy",
                        exc_info=True,
                    )

            policy_action, policy_resource = (
                ("get", "/v1/models")
                if parsed.action is InvocationAction.LIST_MODELS
                else ("post", "/v1/chat/completions")
            )
            try:
                policy_decision = await runtime.policy_service.evaluate(
                    identity.request_context,
                    policy_action,
                    policy_resource,
                )
            except Exception as exc:
                raise AgentCoreAdapterError(
                    503,
                    "policy_evaluation_failed",
                    "Authorization is temporarily unavailable.",
                ) from exc

            if policy_decision == "DENY":
                raise AgentCoreAdapterError(
                    403,
                    "authorization_denied",
                    "Access denied by policy.",
                )
            if policy_decision != "ALLOW":
                raise AgentCoreAdapterError(
                    503,
                    "policy_evaluation_failed",
                    "Authorization is temporarily unavailable.",
                )

        if parsed.action is InvocationAction.LIST_MODELS:
            return await runtime.gateway.handle_list_models(
                project_id=identity.project_id,
                user_id=identity.principal.principal_id,
                tenant_id=identity.tenant_id,
                authorized_project=project,
            )

        if parsed.request_data is None:
            raise AgentCoreAdapterError(
                400,
                "invalid_payload",
                "Chat payload is required.",
            )
        result = await runtime.gateway.handle_chat_completion(
            parsed.request_data,
            _gateway_context(identity, project),
        )
        if hasattr(result, "__aiter__"):
            return _forward_stream(result)
        return result
