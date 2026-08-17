"""Private IAM-authenticated facade client for AgentCore Runtime."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator
from contextvars import ContextVar
import json
import logging
import uuid
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.gateway.models import RequestContext

from .facade_identity import (
    FACADE_IDENTITY_HEADER,
    encode_facade_identity,
)


logger = logging.getLogger(__name__)
_MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_SSE_EVENT_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_IDENTITY_HEADER_VALUE: ContextVar[str | None] = ContextVar(
    "agentcore_facade_identity",
    default=None,
)


class AgentCoreRuntimeClientProtocol(Protocol):
    """Boto3 operations used by the facade proxy."""

    meta: Any

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]: ...


class AgentCoreFacadeError(RuntimeError):
    """Sanitized AgentCore invocation failure."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _inject_facade_identity(request: Any, **_: Any) -> None:
    value = _IDENTITY_HEADER_VALUE.get()
    if value is None:
        raise AgentCoreFacadeError(
            500,
            "facade_identity_missing",
            "AgentCore identity forwarding is unavailable.",
        )
    request.headers[FACADE_IDENTITY_HEADER] = value


def _bounded_body_read(body: Any, maximum: int) -> bytes:
    output = bytearray()
    try:
        while True:
            chunk = body.read(min(_READ_CHUNK_BYTES, maximum + 1 - len(output)))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise AgentCoreFacadeError(
                    502,
                    "invalid_runtime_response",
                    "AgentCore returned an invalid response.",
                )
            output.extend(chunk)
            if len(output) > maximum:
                raise AgentCoreFacadeError(
                    502,
                    "runtime_response_too_large",
                    "AgentCore returned an oversized response.",
                )
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return bytes(output)


def _error_from_client_exception(exc: ClientError) -> AgentCoreFacadeError:
    metadata = exc.response.get("ResponseMetadata", {})
    status_code = metadata.get("HTTPStatusCode", 503)
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 400 <= status_code <= 599:
        status_code = 503
    if status_code >= 500:
        message = "AgentCore is temporarily unavailable."
        code = "agentcore_unavailable"
    elif status_code == 429:
        message = "AgentCore is temporarily throttled."
        code = "agentcore_throttled"
    elif status_code == 403:
        message = "AgentCore invocation is not authorized."
        code = "agentcore_authorization_denied"
    else:
        message = "AgentCore rejected the request."
        code = "agentcore_request_rejected"
    return AgentCoreFacadeError(status_code, code, message)


class AgentCoreGatewayProxy:
    """GatewayAgent-shaped proxy that invokes one private AgentCore Runtime."""

    requires_request_context = True

    def __init__(
        self,
        *,
        runtime_arn: str,
        qualifier: str = "production",
        region: str,
        local_gateway: Any,
        client: AgentCoreRuntimeClientProtocol | None = None,
    ) -> None:
        if not runtime_arn or runtime_arn != runtime_arn.strip():
            raise ValueError("runtime_arn must be a non-empty ARN")
        if not qualifier or qualifier != qualifier.strip():
            raise ValueError("qualifier must be non-empty")
        self.runtime_arn = runtime_arn
        self.qualifier = qualifier
        self.cost_tracker = local_gateway.cost_tracker
        self._user_configs = local_gateway._user_configs
        self.request_validator = local_gateway.request_validator
        self._client = client or boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=Config(
                connect_timeout=5,
                read_timeout=180,
                retries={
                    "mode": "adaptive",
                    "total_max_attempts": 3,
                },
                user_agent_extra="AxonLLM-AgentCore-Facade",
            ),
        )
        self._client.meta.events.register(
            "before-sign.bedrock-agentcore.InvokeAgentRuntime",
            _inject_facade_identity,
        )

    async def _invoke(
        self,
        payload: dict[str, Any],
        request_context: RequestContext,
        *,
        accept: str,
    ) -> dict[str, Any]:
        identity = encode_facade_identity(request_context)
        token = _IDENTITY_HEADER_VALUE.set(identity)
        try:
            try:
                return await asyncio.to_thread(
                    self._client.invoke_agent_runtime,
                    agentRuntimeArn=self.runtime_arn,
                    qualifier=self.qualifier,
                    runtimeSessionId=f"axon-facade-{uuid.uuid4().hex}",
                    payload=json.dumps(
                        payload,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    contentType="application/json",
                    accept=accept,
                )
            except ClientError as exc:
                raise _error_from_client_exception(exc) from exc
            except BotoCoreError as exc:
                raise AgentCoreFacadeError(
                    503,
                    "agentcore_unavailable",
                    "AgentCore is temporarily unavailable.",
                ) from exc
        finally:
            _IDENTITY_HEADER_VALUE.reset(token)

    async def handle_list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        authorized_project: Any | None = None,
        request_context: RequestContext | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        del project_id, user_id, tenant_id, authorized_project
        if request_context is None:
            raise AgentCoreFacadeError(
                401,
                "facade_identity_required",
                "Authenticated identity is required.",
            )
        response = await self._invoke(
            {"action": "list_models"},
            request_context,
            accept="application/json",
        )
        return await self._json_response(response)

    async def handle_chat_completion(
        self,
        request_data: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        request_context = context.get("request_context")
        if not isinstance(request_context, RequestContext):
            raise AgentCoreFacadeError(
                401,
                "facade_identity_required",
                "Authenticated identity is required.",
            )
        payload = {
            "action": "chat",
            **request_data,
        }
        provider = context.get("provider")
        if provider is not None:
            payload["provider"] = provider
        if context.get("smart_routing") is True:
            payload["smart_routing"] = True

        if request_data.get("stream") is True:
            response = await self._invoke(
                payload,
                request_context,
                accept="text/event-stream",
            )
            return self._stream_response(response)
        response = await self._invoke(
            payload,
            request_context,
            accept="application/json",
        )
        return await self._json_response(response)

    async def _json_response(
        self,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        status_code = response.get("statusCode", 200)
        if isinstance(status_code, bool) or not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            )
        body = response.get("response")
        if body is None or not callable(getattr(body, "read", None)):
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            )
        raw = await asyncio.to_thread(
            _bounded_body_read,
            body,
            _MAX_JSON_RESPONSE_BYTES,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            ) from exc
        if type(payload) is not dict:
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            )
        return payload

    async def _stream_response(
        self,
        response: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        status_code = response.get("statusCode", 200)
        body = response.get("response")
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 200 <= status_code < 300
            or body is None
            or not callable(getattr(body, "read", None))
        ):
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            )

        decoder = codecs.getincrementaldecoder("utf-8")()
        text_buffer = ""
        event_data: list[str] = []
        event_bytes = 0
        try:
            while True:
                chunk = await asyncio.to_thread(
                    body.read,
                    _READ_CHUNK_BYTES,
                )
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise AgentCoreFacadeError(
                        502,
                        "invalid_runtime_response",
                        "AgentCore returned an invalid response.",
                    )
                try:
                    text_buffer += decoder.decode(chunk)
                except UnicodeDecodeError as exc:
                    raise AgentCoreFacadeError(
                        502,
                        "invalid_runtime_response",
                        "AgentCore returned an invalid response.",
                    ) from exc
                while "\n" in text_buffer:
                    line, text_buffer = text_buffer.split("\n", 1)
                    line = line.removesuffix("\r")
                    if line == "":
                        if event_data:
                            yield self._decode_sse_event(event_data)
                        event_data = []
                        event_bytes = 0
                        continue
                    if line.startswith("data:"):
                        value = line[5:]
                        if value.startswith(" "):
                            value = value[1:]
                        event_bytes += len(value.encode("utf-8"))
                        if event_bytes > _MAX_SSE_EVENT_BYTES:
                            raise AgentCoreFacadeError(
                                502,
                                "runtime_response_too_large",
                                "AgentCore returned an oversized response.",
                            )
                        event_data.append(value)
            try:
                text_buffer += decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise AgentCoreFacadeError(
                    502,
                    "invalid_runtime_response",
                    "AgentCore returned an invalid response.",
                ) from exc
            if text_buffer:
                line = text_buffer.removesuffix("\r")
                if line.startswith("data:"):
                    value = line[5:]
                    if value.startswith(" "):
                        value = value[1:]
                    event_data.append(value)
            if event_data:
                yield self._decode_sse_event(event_data)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    @staticmethod
    def _decode_sse_event(event_data: list[str]) -> dict[str, Any]:
        try:
            payload = json.loads("\n".join(event_data))
        except json.JSONDecodeError as exc:
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            ) from exc
        if type(payload) is not dict:
            raise AgentCoreFacadeError(
                502,
                "invalid_runtime_response",
                "AgentCore returned an invalid response.",
            )
        return payload
