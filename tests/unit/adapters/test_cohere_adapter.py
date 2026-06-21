"""Unit tests for the Cohere provider adapter."""

import pytest

from src.gateway.adapters.cohere_adapter import CohereAdapter
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
    return CohereAdapter()


# --- translate_request ---


class TestTranslateRequest:
    @pytest.mark.asyncio
    async def test_basic_request_extracts_last_user_message(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
            model="command-r-plus",
        )
        result = await adapter.translate_request(req)
        assert result["message"] == "Hello"
        assert result["model"] == "command-r-plus"
        assert "chat_history" not in result

    @pytest.mark.asyncio
    async def test_multi_turn_builds_chat_history(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "How are you?"},
            ],
            model="command-r",
        )
        result = await adapter.translate_request(req)
        assert result["message"] == "How are you?"
        assert result["chat_history"] == [
            {"role": "USER", "message": "Hi"},
            {"role": "CHATBOT", "message": "Hello!"},
        ]

    @pytest.mark.asyncio
    async def test_system_message_mapped_to_preamble(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="command-r-plus",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req)
        assert result["preamble"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_system_message_extracted_from_messages(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
            model="command-r-plus",
        )
        result = await adapter.translate_request(req)
        assert result["preamble"] == "Be concise."
        assert "chat_history" not in result

    @pytest.mark.asyncio
    async def test_system_field_takes_priority(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "From messages."},
                {"role": "user", "content": "Hi"},
            ],
            model="command-r-plus",
            system="From field.",
        )
        result = await adapter.translate_request(req)
        assert result["preamble"] == "From field."

    @pytest.mark.asyncio
    async def test_top_p_mapped_to_p(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="command-r-plus",
            top_p=0.9,
        )
        result = await adapter.translate_request(req)
        assert result["p"] == 0.9
        assert "top_p" not in result

    @pytest.mark.asyncio
    async def test_stop_mapped_to_stop_sequences(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="command-r-plus",
            stop=["\n", "END"],
        )
        result = await adapter.translate_request(req)
        assert result["stop_sequences"] == ["\n", "END"]
        assert "stop" not in result

    @pytest.mark.asyncio
    async def test_optional_params(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="command-r-plus",
            temperature=0.5,
            max_tokens=100,
        )
        result = await adapter.translate_request(req)
        assert result["temperature"] == 0.5
        assert result["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_stream_param_produces_warning(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="command-r-plus",
            stream=True,
        )
        result = await adapter.translate_request(req)
        assert "_warnings" in result
        assert any("stream" in w.lower() for w in result["_warnings"])

    @pytest.mark.asyncio
    async def test_no_preamble_when_no_system(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="command-r-plus",
        )
        result = await adapter.translate_request(req)
        assert "preamble" not in result

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_messages(self, adapter):
        original = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Hi"}]
        req = ChatCompletionRequest(messages=original, model="command-r-plus")
        await adapter.translate_request(req)
        assert len(original) == 2


# --- translate_response ---


class TestTranslateResponse:
    def test_full_response(self, adapter):
        raw = {
            "id": "cohere_123",
            "text": "Hello there!",
            "model": "command-r-plus",
            "finish_reason": "COMPLETE",
            "meta": {"tokens": {"input_tokens": 12, "output_tokens": 5}},
        }
        resp = adapter.translate_response(raw)
        assert isinstance(resp, ChatCompletionResponse)
        assert resp.id == "cohere_123"
        assert resp.choices[0]["message"]["content"] == "Hello there!"
        assert resp.choices[0]["finish_reason"] == "COMPLETE"
        assert resp.usage == TokenUsage(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        assert resp.provider == "cohere"

    def test_missing_meta_defaults_to_zero(self, adapter):
        raw = {"id": "x", "text": "Hi", "model": "command-r"}
        resp = adapter.translate_response(raw)
        assert resp.usage == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    def test_empty_response(self, adapter):
        resp = adapter.translate_response({})
        assert resp.id == ""
        assert resp.model == ""
        assert resp.provider == "cohere"
        assert resp.choices[0]["message"]["content"] == ""


# --- translate_stream_chunk ---


class TestTranslateStreamChunk:
    def test_text_generation_chunk(self, adapter):
        raw = {"event_type": "text-generation", "text": "Hello", "id": "c1"}
        chunk = adapter.translate_stream_chunk(raw)
        assert isinstance(chunk, StreamChunk)
        assert chunk.choices[0]["delta"]["content"] == "Hello"
        assert chunk.is_final is False

    def test_stream_end_chunk(self, adapter):
        raw = {"event_type": "stream-end"}
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is True
        assert chunk.choices[0]["finish_reason"] == "stop"

    def test_unknown_event_type(self, adapter):
        raw = {"event_type": "search-results"}
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is False


# --- list_models ---


class TestListModels:
    @pytest.mark.asyncio
    async def test_returns_known_models(self, adapter):
        models = await adapter.list_models()
        assert len(models) > 0
        assert all(isinstance(m, ModelInfo) for m in models)
        assert all(m.provider == "cohere" for m in models)
        model_ids = [m.model_id for m in models]
        assert "command-r-plus" in model_ids
        assert "command-r" in model_ids


# --- health_check ---


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_returns_healthy(self, adapter):
        health = await adapter.health_check()
        assert health.provider == "cohere"
        assert health.status == HealthStatus.HEALTHY
        assert health.last_check is not None
