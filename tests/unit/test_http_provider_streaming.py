"""Provider HTTP transport regressions for native streaming."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.gateway.http_client as http_transport
from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.adapters.azure_adapter import AzureOpenAIAdapter
from src.gateway.adapters.cohere_adapter import CohereAdapter
from src.gateway.adapters.google_ai_adapter import GoogleAIAdapter
from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.vertex_adapter import VertexAIAdapter
from src.gateway.http_client import HttpClient
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    ProviderModelMapping,
    TokenUsage,
)
from src.gateway.provider_config import ProviderConfig
from src.gateway.router import ProviderError


class _Content:
    def __init__(self, lines: list[bytes | Exception]) -> None:
        self._lines = lines

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line


class _Response:
    def __init__(
        self,
        lines: list[bytes | Exception],
        *,
        status: int = 200,
        body: dict | None = None,
    ) -> None:
        self.status = status
        content = list(lines)
        if body is not None and not content:
            content.append(json.dumps(body).encode("utf-8"))
        self.content = _Content(content)
        self._body = body if body is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self) -> str:
        return json.dumps(self._body)

    async def json(self, **_kwargs) -> dict:
        return self._body


def _client_with_response(
    provider: str,
    response: _Response,
) -> tuple[HttpClient, MagicMock]:
    client = HttpClient()
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    client._sessions[provider] = session
    return client, session


def _request(model: str = "logical-model") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model=model,
        stream=True,
    )


async def _collect(stream) -> list:
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_read_timeout_is_an_idle_socket_timeout_not_total_lifetime() -> None:
    client = HttpClient()
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
        connect_timeout=7,
        read_timeout=53,
    )

    try:
        session = client._get_or_create_session(config)

        assert session.timeout.connect == 7
        assert session.timeout.sock_read == 53
        assert session.timeout.total == 60
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_internal_adapter_metadata_is_not_sent_over_http() -> None:
    client, session = _client_with_response(
        "openai",
        _Response([], body={}),
    )
    adapter = MagicMock()
    adapter.validate_request = MagicMock()
    adapter.translate_request = AsyncMock(
        return_value={
            "model": "logical-model",
            "messages": [],
            "_warnings": ["adapter warning"],
            "_private_trace": "must-not-leave-process",
        }
    )
    adapter.translate_response = MagicMock(
        return_value=ChatCompletionResponse(
            id="response-1",
            choices=[],
            usage=TokenUsage(0, 0, 0),
            model="provider-model",
            provider="openai",
        )
    )
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="openai",
        model_id="provider-model",
    )

    response = await client.execute(_request(), mapping, adapter, config)

    payload = session.post.call_args.kwargs["json"]
    assert payload == {"model": "provider-model", "messages": []}
    translated_request = adapter.translate_request.await_args.args[0]
    assert translated_request.model == "provider-model"
    assert response.warnings == ["adapter warning"]


@pytest.mark.asyncio
async def test_resolved_openai_model_drives_responses_payload() -> None:
    client, session = _client_with_response(
        "openai",
        _Response(
            [],
            body={
                "id": "response-1",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.5-pro",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.openai.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="openai",
        model_id="gpt-5.5-pro",
    )
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="premium-alias",
        max_tokens=64,
        temperature=0.5,
    )

    await client.execute(request, mapping, OpenAIAdapter(), config)

    assert session.post.call_args.args[0].endswith("/v1/responses")
    payload = session.post.call_args.kwargs["json"]
    assert payload["model"] == "gpt-5.5-pro"
    assert payload["input"] == "hello"
    assert payload["max_output_tokens"] == 64
    assert "messages" not in payload
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_resolved_openai_model_drives_reasoning_parameters() -> None:
    client, session = _client_with_response(
        "openai",
        _Response(
            [],
            body={
                "id": "chat-1",
                "model": "o3",
                "choices": [],
                "usage": {},
            },
        ),
    )
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.openai.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(provider="openai", model_id="o3")
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="reasoning-alias",
        max_tokens=64,
        temperature=0.5,
        top_p=0.9,
    )

    await client.execute(request, mapping, OpenAIAdapter(), config)

    assert session.post.call_args.args[0].endswith("/v1/chat/completions")
    payload = session.post.call_args.kwargs["json"]
    assert payload["model"] == "o3"
    assert payload["max_completion_tokens"] == 64
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "adapter", "auth_type", "expected_url"),
    [
        (
            "openai",
            OpenAIAdapter(),
            "api_key",
            "https://api.example/v1/embeddings",
        ),
        (
            "azure_openai",
            AzureOpenAIAdapter(),
            "azure_key",
            (
                "https://api.example/openai/deployments/embed-deployment"
                "/embeddings?api-version=2024-02-01"
            ),
        ),
    ],
)
async def test_embeddings_transport_uses_provider_model_and_url(
    provider,
    adapter,
    auth_type,
    expected_url,
) -> None:
    client, session = _client_with_response(
        provider,
        _Response(
            [],
            body={
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [0.25, 0.75],
                    }
                ],
                "model": "embed-deployment",
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        ),
    )
    config = ProviderConfig(
        provider_name=provider,
        base_url="https://api.example",
        auth_type=auth_type,
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider=provider,
        model_id="embed-deployment",
    )
    request = EmbeddingRequest(
        input=["hello"],
        model="logical-embedding",
        encoding_format="float",
        dimensions=256,
    )

    response = await client.execute_embeddings(
        request,
        mapping,
        adapter,
        config,
    )

    assert session.post.call_args.args[0] == expected_url
    assert session.post.call_args.kwargs["json"] == {
        "model": "embed-deployment",
        "input": ["hello"],
        "encoding_format": "float",
        "dimensions": 256,
    }
    assert response.data[0].embedding == [0.25, 0.75]
    assert response.usage.prompt_tokens == 3
    assert response.usage.completion_tokens == 0


@pytest.mark.asyncio
async def test_embeddings_reject_adapter_without_capability() -> None:
    client = HttpClient()
    config = ProviderConfig(
        provider_name="anthropic",
        base_url="https://api.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="anthropic",
        model_id="not-an-embedding-model",
    )

    with pytest.raises(ProviderError) as exc_info:
        await client.execute_embeddings(
            EmbeddingRequest(input=["hello"], model="logical-embedding"),
            mapping,
            AnthropicAdapter(),
            config,
        )

    assert exc_info.value.status_code == 501
    assert exc_info.value.provider_unavailable is False


@pytest.mark.asyncio
async def test_resolved_openai_model_drives_streaming_responses_payload() -> None:
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "item-1",
            "delta": "hello",
        },
        {
            "type": "response.completed",
            "response": {
                "id": "response-1",
                "status": "completed",
                "model": "gpt-5.5-pro",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]
    client, session = _client_with_response(
        "openai",
        _Response([
            f"data: {json.dumps(event)}\n".encode()
            for event in events
        ]),
    )
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.openai.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="openai",
        model_id="gpt-5.5-pro",
    )
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="premium-alias",
    )

    chunks = await _collect(
        client.execute_streaming(
            request,
            mapping,
            OpenAIAdapter(),
            config,
        )
    )

    assert session.post.call_args.args[0].endswith("/v1/responses")
    payload = session.post.call_args.kwargs["json"]
    assert payload["model"] == "gpt-5.5-pro"
    assert payload["input"] == "hello"
    assert payload["stream"] is True
    assert "messages" not in payload
    assert "stream_options" not in payload
    assert chunks[0].choices[0]["delta"]["content"] == "hello"
    assert chunks[-1].is_final is True


@pytest.mark.asyncio
async def test_cohere_ndjson_stream_forces_native_streaming_and_keeps_usage() -> None:
    events = [
        {
            "event_type": "stream-start",
            "generation_id": "generation-1",
        },
        {
            "event_type": "text-generation",
            "generation_id": "generation-1",
            "text": "hello",
        },
        {
            "event_type": "stream-end",
            "response": {
                "response_id": "response-1",
                "model": "command-r-plus",
                "finish_reason": "COMPLETE",
                "meta": {
                    "tokens": {
                        "input_tokens": 4,
                        "output_tokens": 2,
                    }
                },
            },
        },
    ]
    client, session = _client_with_response(
        "cohere",
        _Response([(json.dumps(event) + "\n").encode() for event in events]),
    )
    config = ProviderConfig(
        provider_name="cohere",
        base_url="https://api.cohere.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="cohere",
        model_id="command-r-plus",
    )

    chunks = await _collect(
        client.execute_streaming(
            ChatCompletionRequest(
                messages=[{"role": "user", "content": "hello"}],
                model="logical-model",
                stream=False,
            ),
            mapping,
            CohereAdapter(),
            config,
        )
    )

    assert session.post.call_args.kwargs["json"]["stream"] is True
    assert session.post.call_args.args[0].endswith("/v1/chat")
    assert chunks[0].id == "generation-1"
    assert chunks[1].choices[0]["delta"]["content"] == "hello"
    assert chunks[-1].usage == TokenUsage(4, 2, 6)


@pytest.mark.asyncio
async def test_vertex_sse_uses_native_endpoint_without_body_model() -> None:
    event = {
        "responseId": "vertex-response-1",
        "modelVersion": "gemini-2.5-pro-001",
        "candidates": [
            {
                "content": {"parts": [{"text": "hello"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 1,
            "thoughtsTokenCount": 4,
            "totalTokenCount": 8,
            "cachedContentTokenCount": 2,
        },
    }
    client, session = _client_with_response(
        "vertex_ai",
        _Response([f"data: {json.dumps(event)}\n".encode()]),
    )
    credential_provider = type(
        "_Credentials",
        (),
        {"get_token": lambda self: "short-lived-token"},
    )()
    config = ProviderConfig(
        provider_name="vertex_ai",
        base_url="https://us-central1-aiplatform.googleapis.com",
        auth_type="gcp_service_account",
        credentials={"credential_source": "google-auth"},
        credential_provider=credential_provider,
        extra_params={
            "project": "project-a",
            "location": "us-central1",
        },
    )
    mapping = ProviderModelMapping(
        provider="vertex_ai",
        model_id="gemini-2.5-pro",
    )

    chunks = await _collect(
        client.execute_streaming(
            _request(),
            mapping,
            VertexAIAdapter(),
            config,
        )
    )

    assert session.post.call_args.args[0].endswith("/models/gemini-2.5-pro:streamGenerateContent?alt=sse")
    assert "model" not in session.post.call_args.kwargs["json"]
    assert chunks[0].id == "vertex-response-1"
    assert chunks[0].usage == TokenUsage(3, 5, 8, cached_tokens=2)


@pytest.mark.asyncio
async def test_google_ai_sse_keeps_tools_usage_and_native_metadata() -> None:
    event = {
        "responseId": "google-response-1",
        "modelVersion": "gemini-2.5-pro-001",
        "candidates": [{
            "content": {"parts": [{
                "functionCall": {
                    "name": "lookup",
                    "args": {"city": "Paris"},
                }
            }]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": 6,
            "candidatesTokenCount": 2,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 13,
            "cachedContentTokenCount": 4,
        },
    }
    client, session = _client_with_response(
        "google_ai",
        _Response([f"data: {json.dumps(event)}\n".encode()]),
    )
    config = ProviderConfig(
        provider_name="google_ai",
        base_url="https://generativelanguage.googleapis.com",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="google_ai",
        model_id="gemini-2.5-pro",
    )

    chunks = await _collect(
        client.execute_streaming(
            _request(),
            mapping,
            GoogleAIAdapter(),
            config,
        )
    )

    assert session.post.call_args.args[0].endswith(
        "/models/gemini-2.5-pro:streamGenerateContent?alt=sse"
    )
    call = chunks[0].choices[0]["delta"]["tool_calls"][0]
    assert call["function"]["name"] == "lookup"
    assert chunks[0].choices[0]["finish_reason"] == "tool_calls"
    assert chunks[0].id == "google-response-1"
    assert chunks[0].usage == TokenUsage(6, 7, 13, cached_tokens=4)


def test_google_ai_buffered_response_keeps_native_metadata() -> None:
    response = GoogleAIAdapter().translate_response(
        {
            "responseId": "google-response-1",
            "modelVersion": "gemini-2.5-pro-001",
            "candidates": [
                {
                    "content": {"parts": [{"text": "hello"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 1,
                "totalTokenCount": 4,
            },
        }
    )

    assert response.id == "google-response-1"
    assert response.model == "gemini-2.5-pro-001"


@pytest.mark.asyncio
async def test_successful_http_response_with_no_stream_signal_is_rejected() -> None:
    client, _ = _client_with_response(
        "cohere",
        _Response(
            [
                b": keepalive\n",
                b'{"event_type":"stream-start","generation_id":"g1"}\n',
            ]
        ),
    )
    config = ProviderConfig(
        provider_name="cohere",
        base_url="https://api.cohere.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="cohere",
        model_id="command-r-plus",
    )

    with pytest.raises(ProviderError, match="empty streaming response") as exc:
        await _collect(
            client.execute_streaming(
                _request(),
                mapping,
                CohereAdapter(),
                config,
            )
        )

    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_content_followed_by_clean_eof_is_rejected() -> None:
    event = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "partial"},
    }
    client, _ = _client_with_response(
        "anthropic",
        _Response([f"data: {json.dumps(event)}\n".encode()]),
    )
    config = ProviderConfig(
        provider_name="anthropic",
        base_url="https://api.anthropic.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="anthropic",
        model_id="claude-sonnet",
    )

    with pytest.raises(
        ProviderError,
        match="ended without a terminal event",
    ) as exc:
        await _collect(
            client.execute_streaming(
                _request(),
                mapping,
                AnthropicAdapter(),
                config,
            )
        )

    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_malformed_stream_json_is_rejected_without_echoing_payload() -> None:
    client, _ = _client_with_response(
        "cohere",
        _Response([b"data: {not-json credential=secret}\n"]),
    )
    config = ProviderConfig(
        provider_name="cohere",
        base_url="https://api.cohere.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="cohere",
        model_id="command-r-plus",
    )

    with pytest.raises(ProviderError, match="malformed streaming JSON") as exc:
        await _collect(
            client.execute_streaming(
                _request(),
                mapping,
                CohereAdapter(),
                config,
            )
        )

    assert "credential=secret" not in exc.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("after_content", [False, True])
async def test_provider_error_event_fails_stream_safely(
    after_content: bool,
) -> None:
    events = []
    if after_content:
        events.append({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "partial"},
        })
    events.append({
        "type": "error",
        "error": {
            "type": "overloaded_error",
            "message": "credential=provider-secret",
        },
    })
    client, _ = _client_with_response(
        "anthropic",
        _Response(
            [f"data: {json.dumps(event)}\n".encode() for event in events]
        ),
    )
    config = ProviderConfig(
        provider_name="anthropic",
        base_url="https://api.anthropic.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="anthropic",
        model_id="claude-sonnet",
    )

    with pytest.raises(
        ProviderError,
        match="Provider reported a streaming error",
    ) as exc:
        await _collect(
            client.execute_streaming(
                _request(),
                mapping,
                AnthropicAdapter(),
                config,
            )
        )

    assert "provider-secret" not in exc.value.message


@pytest.mark.asyncio
async def test_done_terminates_without_waiting_for_connection_close() -> None:
    event = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "done"},
    }
    client, _ = _client_with_response(
        "anthropic",
        _Response(
            [
                f"data: {json.dumps(event)}\n".encode(),
                b"data: [DONE]\n",
                RuntimeError("stream should not be read after DONE"),
            ]
        ),
    )
    config = ProviderConfig(
        provider_name="anthropic",
        base_url="https://api.anthropic.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="anthropic",
        model_id="claude-sonnet",
    )

    chunks = await _collect(
        client.execute_streaming(
            _request(),
            mapping,
            AnthropicAdapter(),
            config,
        )
    )

    assert chunks[0].choices[0]["delta"]["content"] == "done"


@pytest.mark.asyncio
async def test_buffered_provider_body_limit_is_enforced(
    monkeypatch,
) -> None:
    monkeypatch.setattr(http_transport, "_MAX_PROVIDER_BODY_BYTES", 4)
    client, _ = _client_with_response(
        "openai",
        _Response([b"12345"]),
    )
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="openai",
        model_id="gpt-test",
    )

    with pytest.raises(ProviderError, match="maximum size") as raised:
        await client.execute(
            _request(),
            mapping,
            OpenAIAdapter(),
            config,
        )

    assert raised.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_event_line_limit_is_enforced(monkeypatch) -> None:
    monkeypatch.setattr(
        http_transport,
        "_MAX_PROVIDER_STREAM_LINE_BYTES",
        16,
    )
    client, _ = _client_with_response(
        "cohere",
        _Response([b"data: " + (b"x" * 32) + b"\n"]),
    )
    config = ProviderConfig(
        provider_name="cohere",
        base_url="https://api.cohere.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="cohere",
        model_id="command-r-plus",
    )

    with pytest.raises(
        ProviderError,
        match="event exceeded the maximum size",
    ):
        await _collect(
            client.execute_streaming(
                _request(),
                mapping,
                CohereAdapter(),
                config,
            )
        )


@pytest.mark.asyncio
async def test_cumulative_raw_stream_limit_is_enforced(monkeypatch) -> None:
    monkeypatch.setattr(
        http_transport,
        "_MAX_PROVIDER_STREAM_BYTES",
        20,
    )
    client, _ = _client_with_response(
        "cohere",
        _Response([b": keepalive\n", b": keepalive\n"]),
    )
    config = ProviderConfig(
        provider_name="cohere",
        base_url="https://api.cohere.example",
        auth_type="api_key",
        credentials={"api_key": "secret"},
    )
    mapping = ProviderModelMapping(
        provider="cohere",
        model_id="command-r-plus",
    )

    with pytest.raises(
        ProviderError,
        match="stream exceeded the maximum size",
    ):
        await _collect(
            client.execute_streaming(
                _request(),
                mapping,
                CohereAdapter(),
                config,
            )
        )
