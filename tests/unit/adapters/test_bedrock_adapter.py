"""Unit tests for the AWS Bedrock provider adapter."""

import pytest

from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    HealthStatus,
    ModelInfo,
    StreamChunk,
    TokenUsage,
)


@pytest.fixture
def adapter():
    return BedrockAdapter()


# --- translate_request ---


class TestTranslateRequest:
    @pytest.mark.asyncio
    async def test_basic_request_has_default_max_tokens(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
        )
        result = await adapter.translate_request(req)
        assert result["model"] == "anthropic.claude-3-sonnet-20240229-v1:0"
        assert result["messages"] == [{"role": "user", "content": "Hello"}]
        assert result["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_explicit_max_tokens(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
            max_tokens=200,
        )
        result = await adapter.translate_request(req)
        assert result["max_tokens"] == 200

    @pytest.mark.asyncio
    async def test_system_field_placed_separately(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req)
        assert result["system"] == "You are helpful."
        assert all(m.get("role") != "system" for m in result["messages"])

    @pytest.mark.asyncio
    async def test_system_message_extracted_from_messages(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
        )
        result = await adapter.translate_request(req)
        assert result["system"] == "Be concise."
        assert len(result["messages"]) == 1
        assert result["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_system_field_takes_priority_over_system_message(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "From messages."},
                {"role": "user", "content": "Hi"},
            ],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
            system="From field.",
        )
        result = await adapter.translate_request(req)
        assert result["system"] == "From field."
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_stop_mapped_to_stop_sequences(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
            stop=["\n", "END"],
        )
        result = await adapter.translate_request(req)
        assert result["stop_sequences"] == ["\n", "END"]
        assert "stop" not in result

    @pytest.mark.asyncio
    async def test_optional_params(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
            temperature=0.5,
            top_p=0.8,
            stream=True,
        )
        result = await adapter.translate_request(req)
        assert result["temperature"] == 0.5
        assert result["top_p"] == 0.8
        assert result["stream"] is True

    @pytest.mark.asyncio
    async def test_no_system_key_when_none(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
        )
        result = await adapter.translate_request(req)
        assert "system" not in result

    @pytest.mark.asyncio
    async def test_tool_choice_none_omits_tools(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-sonnet-20240229-v1:0",
            tools=[{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            tool_choice="none",
        )

        result = await adapter.translate_request(req)

        assert "tools" not in result
        assert "tool_choice" not in result

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_messages(self, adapter):
        original = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Hi"}]
        req = ChatCompletionRequest(messages=original, model="anthropic.claude-3-sonnet-20240229-v1:0")
        await adapter.translate_request(req)
        assert len(original) == 2


# --- translate_response ---


class TestTranslateResponse:
    def test_full_response_with_camel_case_usage(self, adapter):
        raw = {
            "id": "msg_bedrock_123",
            "content": [{"type": "text", "text": "Hello there!"}],
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "stop_reason": "end_turn",
            "usage": {"inputTokens": 12, "outputTokens": 5},
        }
        resp = adapter.translate_response(raw)
        assert isinstance(resp, ChatCompletionResponse)
        assert resp.id == "msg_bedrock_123"
        assert resp.choices[0]["message"]["content"] == "Hello there!"
        assert resp.choices[0]["finish_reason"] == "end_turn"
        assert resp.usage == TokenUsage(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        assert resp.provider == "bedrock"

    def test_response_with_snake_case_usage(self, adapter):
        raw = {
            "id": "msg_x",
            "content": [{"type": "text", "text": "Hi"}],
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
        resp = adapter.translate_response(raw)
        assert resp.usage == TokenUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11)

    def test_multiple_content_blocks(self, adapter):
        raw = {
            "id": "msg_x",
            "content": [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": " Part 2"},
            ],
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "usage": {"inputTokens": 5, "outputTokens": 10},
        }
        resp = adapter.translate_response(raw)
        assert resp.choices[0]["message"]["content"] == "Part 1 Part 2"

    def test_missing_usage_defaults_to_zero(self, adapter):
        raw = {"id": "x", "content": [], "model": "m"}
        resp = adapter.translate_response(raw)
        assert resp.usage == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    def test_empty_response(self, adapter):
        resp = adapter.translate_response({})
        assert resp.id == ""
        assert resp.model == ""
        assert resp.provider == "bedrock"


# --- translate_stream_chunk ---


class TestTranslateStreamChunk:
    def test_content_block_delta(self, adapter):
        raw = {"type": "content_block_delta", "delta": {"text": "Hello"}, "id": "c1"}
        chunk = adapter.translate_stream_chunk(raw)
        assert isinstance(chunk, StreamChunk)
        assert chunk.choices[0]["delta"]["content"] == "Hello"
        assert chunk.is_final is False

    def test_message_stop(self, adapter):
        raw = {"type": "message_stop"}
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is True
        assert chunk.choices[0]["finish_reason"] == "stop"

    def test_unknown_event_type(self, adapter):
        raw = {"type": "ping"}
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is False


# --- list_models ---


class TestListModels:
    @pytest.mark.asyncio
    async def test_returns_known_models(self, adapter):
        models = await adapter.list_models()
        assert len(models) > 0
        assert all(isinstance(m, ModelInfo) for m in models)
        assert all(m.provider == "bedrock" for m in models)
        model_ids = [m.model_id for m in models]
        assert "anthropic.claude-3-sonnet-20240229-v1:0" in model_ids
        assert "amazon.titan-text-express-v1" in model_ids


# --- health_check ---


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_returns_healthy(self, adapter):
        health = await adapter.health_check()
        assert health.provider == "bedrock"
        assert health.status == HealthStatus.HEALTHY
        assert health.last_check is not None
