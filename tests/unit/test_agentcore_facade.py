"""Feature-parity facade contracts for AgentCore-backed deployments."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.gateway.agentcore.facade_client import AgentCoreGatewayProxy
from src.gateway.agentcore.facade_identity import (
    FACADE_IDENTITY_HEADER,
    decode_facade_identity,
    encode_facade_identity,
)
from src.gateway.chat.client_agent import ClientAgent
from src.gateway.models import AuthMethod, RequestContext
from src.gateway.request_validator import RequestValidator


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm-AbCdEf1234"


class _Events:
    def __init__(self) -> None:
        self.handler: Any = None
        self.event_name: str | None = None

    def register(self, event_name: str, handler: Any) -> None:
        self.event_name = event_name
        self.handler = handler


class _Body(io.BytesIO):
    pass


class _RuntimeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.meta = SimpleNamespace(events=_Events())
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        request = SimpleNamespace(headers={})
        self.meta.events.handler(request)
        self.calls.append(
            {
                **kwargs,
                "identity": request.headers[FACADE_IDENTITY_HEADER],
            }
        )
        return self.responses.pop(0)


class _LocalGateway:
    def __init__(self) -> None:
        self.cost_tracker = SimpleNamespace(
            _records=[],
            synced_records=None,
        )
        self._user_configs: dict[str, dict[str, Any]] = {}
        self.request_validator = RequestValidator()


def _context(
    *,
    subject: str = "subject-a",
    project_id: str = "project-a",
) -> RequestContext:
    return RequestContext(
        user_id="principal-a",
        project_id=project_id,
        roles=["tenant_member"],
        scopes=["inference.invoke", "model.list"],
        auth_method=AuthMethod.OIDC_JWT,
        tenant_id="tenant-a",
        issuer="https://issuer.example",
        subject=subject,
        principal_id="principal-a",
        authorization_version=7,
    )


def _json_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "contentType": "application/json",
        "response": _Body(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    }


def _stream_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    payload = b"".join(
        ("data: " + json.dumps(event, separators=(",", ":")) + "\r\n\r\n").encode("utf-8") for event in events
    )
    return {
        "statusCode": 200,
        "contentType": "text/event-stream",
        "response": _Body(payload),
    }


def _proxy(
    client: _RuntimeClient,
) -> AgentCoreGatewayProxy:
    return AgentCoreGatewayProxy(
        runtime_arn=RUNTIME_ARN,
        qualifier="production",
        region="us-east-1",
        local_gateway=_LocalGateway(),
        client=client,
    )


def test_facade_identity_excludes_server_held_authority() -> None:
    context = _context()

    encoded = encode_facade_identity(context)
    decoded = decode_facade_identity(encoded)

    assert decoded.roles == []
    assert decoded.scopes == []
    assert decoded.principal_id is None
    assert decoded.authorization_version is None
    assert decoded.issuer == context.issuer
    assert decoded.subject == context.subject
    assert decoded.tenant_id == context.tenant_id
    assert decoded.project_id == context.project_id


def test_api_key_facade_identity_round_trip() -> None:
    context = RequestContext(
        user_id="principal-service",
        project_id="project-a",
        roles=["service"],
        scopes=["inference.invoke"],
        auth_method=AuthMethod.API_KEY,
        tenant_id="tenant-a",
        api_key_id="key-123",
        issuer="urn:axonllm:api-key",
        subject="key-123",
        principal_id="principal-service",
        authorization_version=4,
    )

    decoded = decode_facade_identity(encode_facade_identity(context))

    assert decoded.auth_method is AuthMethod.API_KEY
    assert decoded.api_key_id == "key-123"
    assert decoded.roles == []
    assert decoded.scopes == []


@pytest.mark.asyncio
async def test_facade_chat_uses_sigv4_signed_custom_identity() -> None:
    client = _RuntimeClient(
        [
            _json_response(
                {
                    "id": "completion-1",
                    "model": "claude-sonnet",
                    "provider": "bedrock",
                    "choices": [
                        {
                            "message": {"content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            )
        ]
    )
    agent = ClientAgent(_proxy(client))

    result = await agent.chat(
        "claude-sonnet",
        [{"role": "user", "content": "hi"}],
        request_context=_context(),
    )

    assert result["content"] == "hello"
    assert result["provider"] == "bedrock"
    assert client.meta.events.event_name == ("before-sign.bedrock-agentcore.InvokeAgentRuntime")
    call = client.calls[0]
    assert call["agentRuntimeArn"] == RUNTIME_ARN
    assert call["qualifier"] == "production"
    assert call["contentType"] == "application/json"
    forwarded = decode_facade_identity(call["identity"])
    assert forwarded.roles == []
    assert forwarded.scopes == []


@pytest.mark.asyncio
async def test_facade_stream_preserves_runtime_chunks() -> None:
    client = _RuntimeClient(
        [
            _stream_response(
                [
                    {
                        "data": {
                            "id": "completion-1",
                            "model": "claude-sonnet",
                            "choices": [
                                {
                                    "delta": {"content": "hel"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    },
                    {
                        "data": {
                            "id": "completion-1",
                            "model": "claude-sonnet",
                            "choices": [
                                {
                                    "delta": {"content": "lo"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    },
                    {"data": "[DONE]"},
                ]
            )
        ]
    )
    agent = ClientAgent(_proxy(client))

    chunks = [
        chunk
        async for chunk in agent.chat_stream(
            "claude-sonnet",
            [{"role": "user", "content": "hi"}],
            request_context=_context(),
        )
    ]

    assert [chunk.get("content") for chunk in chunks[:-1]] == [
        "hel",
        "lo",
    ]
    assert chunks[-1] == {"done": True}


@pytest.mark.asyncio
async def test_concurrent_facade_calls_do_not_cross_identity_headers() -> None:
    client = _RuntimeClient(
        [
            _json_response({"models": [{"name": "model-a"}]}),
            _json_response({"models": [{"name": "model-a"}]}),
        ]
    )
    proxy = _proxy(client)

    await asyncio.gather(
        proxy.handle_list_models(request_context=_context(subject="a")),
        proxy.handle_list_models(request_context=_context(subject="b")),
    )

    subjects = {decode_facade_identity(call["identity"]).subject for call in client.calls}
    assert subjects == {"a", "b"}
