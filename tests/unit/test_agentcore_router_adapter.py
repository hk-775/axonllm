"""Focused coverage for the inference-only AgentCore public boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from starlette.exceptions import HTTPException

import agentcore_agent
from src.gateway.agentcore.errors import AgentCoreAdapterError
from src.gateway.agentcore.router_adapter import (
    AgentCoreRouterAdapter,
    parse_router_invocation,
)


class _InternalAdapter:
    def __init__(
        self,
        handler: Callable[[dict[str, Any], Any], Any] | None = None,
    ) -> None:
        self.handler = handler
        self.calls: list[tuple[dict[str, Any], Any]] = []
        self.lifecycle: list[str] = []

    async def initialize(self) -> None:
        self.lifecycle.append("initialize")

    async def readiness(self) -> dict[str, Any]:
        self.lifecycle.append("readiness")
        return {"status": "ready", "ready": True}

    async def close(self) -> None:
        self.lifecycle.append("close")

    async def invoke(self, payload: Any, context: Any) -> Any:
        assert isinstance(payload, dict)
        self.calls.append((payload, context))
        if self.handler is None:
            raise AssertionError("unexpected internal invocation")
        result = self.handler(payload, context)
        if hasattr(result, "__await__"):
            return await result
        return result


def _chat_response(
    *,
    content: str = "hello",
    finish_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "id": "provider-response",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
        },
        "model": "logical-model",
        "provider": "openai",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": {"model": "logical-model", "messages": []},
        },
        {
            "method": "POST",
            "path": "/v1/responses",
            "body": {"model": "logical-model", "input": "hello"},
        },
        {
            "method": "POST",
            "path": "/v1/embeddings",
            "body": {"model": "embedding-model", "input": "hello"},
        },
        {"method": "GET", "path": "/v1/models"},
    ],
)
def test_public_parser_accepts_only_supported_method_path_pairs(
    payload: dict[str, Any],
) -> None:
    parsed = parse_router_invocation(payload)

    assert parsed.method == payload["method"]
    assert parsed.path == payload["path"]


@pytest.mark.parametrize(
    ("payload", "status_code", "code"),
    [
        (
            {
                "action": "query",
                "datasource_id": "warehouse",
                "sql": "SELECT 1",
            },
            400,
            "invalid_payload",
        ),
        (
            {
                "method": "POST",
                "path": "/v1/query",
                "body": {"sql": "SELECT 1"},
            },
            404,
            "route_not_found",
        ),
        (
            {
                "method": "GET",
                "path": "/v1/tenant/config",
            },
            404,
            "route_not_found",
        ),
        (
            {
                "method": "POST",
                "path": "/v1/chat/completions",
                "body": {
                    "model": "logical-model",
                    "messages": [],
                    "tenant_id": "attacker",
                },
            },
            400,
            "untrusted_identity_fields",
        ),
    ],
)
def test_public_parser_rejects_legacy_and_untrusted_surfaces(
    payload: dict[str, Any],
    status_code: int,
    code: str,
) -> None:
    with pytest.raises(AgentCoreAdapterError) as raised:
        parse_router_invocation(payload)

    assert raised.value.status_code == status_code
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_public_adapter_delegates_lifecycle() -> None:
    internal = _InternalAdapter()
    adapter = AgentCoreRouterAdapter(internal)

    await adapter.initialize()
    readiness = await adapter.readiness()
    await adapter.close()

    assert readiness == {"status": "ready", "ready": True}
    assert internal.lifecycle == ["initialize", "readiness", "close"]


@pytest.mark.asyncio
async def test_chat_completion_is_openai_shaped() -> None:
    context = object()
    internal = _InternalAdapter(
        lambda _payload, _context: _chat_response()
    )
    adapter = AgentCoreRouterAdapter(internal)

    response = await adapter.invoke(
        {
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": {
                "model": "logical-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        },
        context,
    )

    assert response["object"] == "chat.completion"
    assert response["model"] == "logical-model"
    assert response["choices"][0]["message"]["content"] == "hello"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["usage"]["total_tokens"] == 5
    assert "provider" not in response
    assert internal.calls == [
        (
            {
                "action": "chat",
                "model": "logical-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
            context,
        )
    ]


@pytest.mark.asyncio
async def test_embeddings_use_internal_authenticated_action() -> None:
    context = object()

    def handler(payload: dict[str, Any], _context: Any) -> dict[str, Any]:
        assert payload["action"] == "embeddings"
        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": [0.1, 0.2],
                }
            ],
            "model": "embedding-model",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
            "_rate_limit_headers": {"x-ratelimit-limit": "10"},
        }

    internal = _InternalAdapter(handler)
    adapter = AgentCoreRouterAdapter(internal)

    response = await adapter.invoke(
        {
            "method": "POST",
            "path": "/v1/embeddings",
            "body": {
                "model": "embedding-model",
                "input": ["first", "second"],
                "provider": "openai",
            },
        },
        context,
    )

    assert response["object"] == "list"
    assert response["data"][0]["embedding"] == [0.1, 0.2]
    assert "_rate_limit_headers" not in response
    assert internal.calls[0] == (
        {
            "action": "embeddings",
            "model": "embedding-model",
            "input": ["first", "second"],
            "provider": "openai",
        },
        context,
    )


@pytest.mark.asyncio
async def test_model_listing_is_openai_shaped() -> None:
    internal = _InternalAdapter(
        lambda _payload, _context: {
            "models": [
                {"name": "fast"},
                {"name": "accurate"},
            ]
        }
    )
    adapter = AgentCoreRouterAdapter(internal)

    response = await adapter.invoke(
        {"method": "GET", "path": "/v1/models"},
        object(),
    )

    assert response["object"] == "list"
    assert [model["id"] for model in response["data"]] == [
        "fast",
        "accurate",
    ]
    assert internal.calls[0][0] == {"action": "list_models"}


@pytest.mark.asyncio
async def test_responses_request_reuses_governed_chat_action() -> None:
    internal = _InternalAdapter(
        lambda _payload, _context: _chat_response(content="response text")
    )
    adapter = AgentCoreRouterAdapter(internal)

    response = await adapter.invoke(
        {
            "method": "POST",
            "path": "/v1/responses",
            "body": {
                "model": "logical-model",
                "input": "hello",
                "instructions": "Be concise.",
            },
        },
        object(),
    )

    assert response["object"] == "response"
    assert response["status"] == "completed"
    assert response["output"][0]["content"][0]["text"] == "response text"
    internal_payload = internal.calls[0][0]
    assert internal_payload["action"] == "chat"
    assert internal_payload["messages"] == [
        {"role": "user", "content": "hello"}
    ]
    assert internal_payload["system"] == "Be concise."


@pytest.mark.asyncio
async def test_chat_stream_emits_openai_events_and_closes_upstream() -> None:
    closed = False

    async def stream() -> AsyncIterator[dict[str, Any]]:
        nonlocal closed
        try:
            yield {"_rate_limit_headers": {"x-ratelimit-limit": "10"}}
            yield {
                "data": {
                    "id": "provider-id",
                    "model": "logical-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "hel"},
                            "finish_reason": None,
                        }
                    ],
                }
            }
            yield {
                "data": {
                    "id": "provider-id",
                    "model": "logical-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "lo"},
                            "finish_reason": "end_turn",
                        }
                    ],
                }
            }
            yield {"data": "[DONE]"}
        finally:
            closed = True

    internal = _InternalAdapter(
        lambda _payload, _context: stream()
    )
    adapter = AgentCoreRouterAdapter(internal)

    result = await adapter.invoke(
        {
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": {
                "model": "logical-model",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        },
        object(),
    )
    events = [event async for event in result]

    assert [event["event"] for event in events] == [
        "data",
        "data",
        "done",
    ]
    assert events[0]["data"]["choices"][0]["delta"] == {
        "content": "hel",
        "role": "assistant",
    }
    assert events[1]["data"]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["data"] == "[DONE]"
    assert closed is True


@pytest.mark.asyncio
async def test_responses_stream_emits_responses_lifecycle_events() -> None:
    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "data": {
                "model": "logical-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "hello"},
                        "finish_reason": None,
                    }
                ],
            }
        }
        yield {
            "data": {
                "model": "logical-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "end_turn",
                    }
                ],
            }
        }
        yield {"data": "[DONE]"}

    internal = _InternalAdapter(
        lambda _payload, _context: stream()
    )
    adapter = AgentCoreRouterAdapter(internal)

    result = await adapter.invoke(
        {
            "method": "POST",
            "path": "/v1/responses",
            "body": {
                "model": "logical-model",
                "input": "hello",
                "stream": True,
            },
        },
        object(),
    )
    events = [event async for event in result]
    event_types = [event["event"] for event in events]

    assert event_types[:2] == [
        "response.created",
        "response.in_progress",
    ]
    assert "response.output_text.delta" in event_types
    assert event_types[-1] == "response.completed"
    assert events[-1]["data"]["response"]["output"][0]["content"][0][
        "text"
    ] == "hello"


def test_deployed_entrypoint_uses_public_router_adapter() -> None:
    assert isinstance(
        agentcore_agent._adapter,
        AgentCoreRouterAdapter,
    )


@pytest.mark.asyncio
async def test_deployed_entrypoint_rejects_legacy_actions_before_runtime() -> None:
    with pytest.raises(HTTPException) as raised:
        await agentcore_agent.invoke(
            {
                "action": "update_tenant_config",
                "expected_revision": 1,
                "config": {"name": "attacker"},
            },
            None,
        )

    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == "invalid_payload"
