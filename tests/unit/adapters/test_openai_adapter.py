"""Unit tests for the OpenAI provider adapter."""

import pytest

from src.gateway.adapters.openai_adapter import OpenAIAdapter
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
    return OpenAIAdapter()


# --- translate_request ---


class TestTranslateRequest:
    @pytest.mark.asyncio
    async def test_basic_request(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-4o",
        )
        result = await adapter.translate_request(req)
        assert result["messages"] == [{"role": "user", "content": "Hello"}]
        assert result["model"] == "gpt-4o"
        # Optional params should be absent when not set
        assert "temperature" not in result
        assert "max_tokens" not in result
        assert "top_p" not in result
        assert "stop" not in result
        assert "stream" not in result

    @pytest.mark.asyncio
    async def test_all_optional_params(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            temperature=0.7,
            max_tokens=100,
            top_p=0.9,
            stop=["\n"],
            stream=True,
        )
        result = await adapter.translate_request(req)
        assert result["temperature"] == 0.7
        assert result["max_tokens"] == 100
        assert result["top_p"] == 0.9
        assert result["stop"] == ["\n"]
        assert result["stream"] is True

    @pytest.mark.asyncio
    async def test_system_message_prepended(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req)
        assert len(result["messages"]) == 2
        assert result["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert result["messages"][1] == {"role": "user", "content": "Hi"}

    @pytest.mark.asyncio
    async def test_system_none_does_not_prepend(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            system=None,
        )
        result = await adapter.translate_request(req)
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_messages(self, adapter):
        original_messages = [{"role": "user", "content": "Hi"}]
        req = ChatCompletionRequest(
            messages=original_messages,
            model="gpt-4o",
            system="Be concise.",
        )
        await adapter.translate_request(req)
        # Original list should be untouched
        assert len(original_messages) == 1


# --- translate_response ---


class TestTranslateResponse:
    def test_full_response(self, adapter):
        raw = {
            "id": "chatcmpl-abc123",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "model": "gpt-4o-2024-05-13",
        }
        resp = adapter.translate_response(raw)
        assert isinstance(resp, ChatCompletionResponse)
        assert resp.id == "chatcmpl-abc123"
        assert resp.choices == raw["choices"]
        assert resp.usage == TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        assert resp.model == "gpt-4o-2024-05-13"
        assert resp.provider == "openai"
        assert resp.warnings == []

    def test_missing_usage_defaults_to_zero(self, adapter):
        raw = {"id": "x", "choices": [], "model": "gpt-4o"}
        resp = adapter.translate_response(raw)
        assert resp.usage == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    def test_missing_fields_default_to_empty(self, adapter):
        resp = adapter.translate_response({})
        assert resp.id == ""
        assert resp.choices == []
        assert resp.model == ""
        assert resp.provider == "openai"


# --- translate_stream_chunk ---


class TestTranslateStreamChunk:
    def test_intermediate_chunk(self, adapter):
        raw = {
            "id": "chatcmpl-abc",
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
            "model": "gpt-4o",
        }
        chunk = adapter.translate_stream_chunk(raw)
        assert isinstance(chunk, StreamChunk)
        assert chunk.id == "chatcmpl-abc"
        assert chunk.model == "gpt-4o"
        assert chunk.is_final is False
        assert chunk.choices == raw["choices"]

    def test_final_chunk(self, adapter):
        raw = {
            "id": "chatcmpl-abc",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "model": "gpt-4o",
        }
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is True

    def test_empty_choices(self, adapter):
        chunk = adapter.translate_stream_chunk({"id": "x", "choices": [], "model": "m"})
        assert chunk.is_final is False
        assert chunk.choices == []

    def test_usage_chunk_from_include_usage(self, adapter):
        # stream_options.include_usage → trailing chunk with empty choices + usage.
        raw = {"id": "x", "choices": [], "model": "gpt-4o",
               "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}}
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is True
        assert chunk.usage is not None
        assert chunk.usage.prompt_tokens == 30 and chunk.usage.completion_tokens == 12

    async def test_stream_request_sets_include_usage(self, adapter):
        from src.gateway.models import ChatCompletionRequest
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="gpt-4o", stream=True)
        payload = await adapter.translate_request(req)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}


# --- list_models ---


class TestListModels:
    @pytest.mark.asyncio
    async def test_returns_known_models(self, adapter):
        models = await adapter.list_models()
        assert len(models) > 0
        assert all(isinstance(m, ModelInfo) for m in models)
        assert all(m.provider == "openai" for m in models)
        model_ids = [m.model_id for m in models]
        assert "gpt-4o" in model_ids
        assert "gpt-4o-mini" in model_ids


# --- health_check ---


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_returns_healthy(self, adapter):
        health = await adapter.health_check()
        assert health.provider == "openai"
        assert health.status == HealthStatus.HEALTHY
        assert health.last_check is not None
