"""Fail-closed AgentCore authorization and gateway dispatch."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from src.gateway.auth.authorization import (
    Action,
    AuthorizationDenied,
    ResourceRef,
    require_authorized,
)

from .errors import AgentCoreAdapterError
from .identity import InvocationIdentity, resolve_invocation_identity
from .runtime import RuntimeServices
from .schemas import InvocationAction, parse_invocation_payload


class RuntimeProviderProtocol(Protocol):
    async def get(self) -> RuntimeServices: ...


def _gateway_context(identity: InvocationIdentity) -> dict[str, Any]:
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
        action = Action.MODEL_LIST if parsed.action is InvocationAction.LIST_MODELS else Action.INFERENCE_INVOKE
        resource = ResourceRef(
            resource_type="project",
            resource_id=identity.project_id,
            tenant_id=identity.tenant_id,
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

        if parsed.action is InvocationAction.LIST_MODELS:
            return await runtime.gateway.handle_list_models(
                project_id=identity.project_id,
                user_id=identity.principal.principal_id,
            )

        if parsed.request_data is None:
            raise AgentCoreAdapterError(
                400,
                "invalid_payload",
                "Chat payload is required.",
            )
        result = await runtime.gateway.handle_chat_completion(
            parsed.request_data,
            _gateway_context(identity),
        )
        if hasattr(result, "__aiter__"):
            return _forward_stream(result)
        return result
