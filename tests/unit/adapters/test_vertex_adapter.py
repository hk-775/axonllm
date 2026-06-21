"""Unit tests for the Google Vertex AI provider adapter."""

import pytest

from src.gateway.adapters.vertex_adapter import VertexAIAdapter
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
    return VertexAIAdapter()


# --- translate_request ---


class TestTranslateRequest:
    @pytest.mark.asyncio
    async def test_basic_request_uses_contents_format(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
            model="gemini-1.5-pro",
        )
        result = await adapter.translate_request(req)
        assert result["model"] == "gemini-1.5-pro"
        assert result["contents"] == [
            {"role": "user", "parts": [{"text": "Hello"}]}
        ]
        assert "generationConfig" not in result
        assert "systemInstruction" not in result

    @pytest.mark.asyncio
    async def test_system_message_mapped_to_system_instruction(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-1.5-pro",
            system="You are helpful.",
        )
        result = await adapter.translate_request(req)
        assert result["systemInstruction"] == {"parts": [{"text": "You are helpful."}]}
        # System should not appear in contents
        assert all(c["role"] != "system" for c in result["contents"])

    @pytest.mark.asyncio
    async def test_system_message_extracted_from_messages(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
            model="gemini-1.5-pro",
        )
        result = await adapter.translate_request(req)
        assert result["systemInstruction"] == {"parts": [{"text": "Be concise."}]}
        assert len(result["contents"]) == 1
        assert result["contents"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_system_field_takes_priority(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "system", "content": "From messages."},
                {"role": "user", "content": "Hi"},
            ],
            model="gemini-1.5-pro",
            system="From field.",
        )
        result = await adapter.translate_request(req)
        assert result["systemInstruction"] == {"parts": [{"text": "From field."}]}
        assert len(result["contents"]) == 1

    @pytest.mark.asyncio
    async def test_assistant_role_mapped_to_model(self, adapter):
        req = ChatCompletionRequest(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "How are you?"},
            ],
            model="gemini-1.5-pro",
        )
        result = await adapter.translate_request(req)
        assert result["contents"][0]["role"] == "user"
        assert result["contents"][1]["role"] == "model"
        assert result["contents"][2]["role"] == "user"

    @pytest.mark.asyncio
    async def test_generation_config_params(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-1.5-pro",
            temperature=0.7,
            max_tokens=100,
            top_p=0.9,
            stop=["\n", "END"],
        )
        result = await adapter.translate_request(req)
        gc = result["generationConfig"]
        assert gc["temperature"] == 0.7
        assert gc["maxOutputTokens"] == 100
        assert gc["topP"] == 0.9
        assert gc["stopSequences"] == ["\n", "END"]

    @pytest.mark.asyncio
    async def test_stream_param_produces_warning(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-1.5-pro",
            stream=True,
        )
        result = await adapter.translate_request(req)
        assert "_warnings" in result
        assert any("stream" in w.lower() for w in result["_warnings"])

    @pytest.mark.asyncio
    async def test_no_system_instruction_when_none(self, adapter):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-1.5-pro",
        )
        result = await adapter.translate_request(req)
        assert "systemInstruction" not in result

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_messages(self, adapter):
        original = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Hi"}]
        req = ChatCompletionRequest(messages=original, model="gemini-1.5-pro")
        await adapter.translate_request(req)
        assert len(original) == 2


# --- translate_response ---


class TestTranslateResponse:
    def test_full_response(self, adapter):
        raw = {
            "id": "vertex_123",
            "candidates": [
                {
                    "content": {"parts": [{"text": "Hello there!"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5},
            "model": "gemini-1.5-pro",
        }
        resp = adapter.translate_response(raw)
        assert isinstance(resp, ChatCompletionResponse)
        assert resp.id == "vertex_123"
        assert resp.choices[0]["message"]["content"] == "Hello there!"
        assert resp.choices[0]["finish_reason"] == "STOP"
        assert resp.usage == TokenUsage(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        assert resp.provider == "vertex_ai"

    def test_multiple_parts(self, adapter):
        raw = {
            "id": "v",
            "candidates": [
                {"content": {"parts": [{"text": "Part 1"}, {"text": " Part 2"}]}}
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 10},
            "model": "gemini-1.5-pro",
        }
        resp = adapter.translate_response(raw)
        assert resp.choices[0]["message"]["content"] == "Part 1 Part 2"

    def test_missing_usage_defaults_to_zero(self, adapter):
        raw = {"id": "x", "candidates": [], "model": "m"}
        resp = adapter.translate_response(raw)
        assert resp.usage == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    def test_empty_candidates(self, adapter):
        raw = {"id": "x", "candidates": [], "model": "m"}
        resp = adapter.translate_response(raw)
        assert resp.choices[0]["message"]["content"] == ""

    def test_empty_response(self, adapter):
        resp = adapter.translate_response({})
        assert resp.id == ""
        assert resp.model == ""
        assert resp.provider == "vertex_ai"


# --- translate_stream_chunk ---


class TestTranslateStreamChunk:
    def test_intermediate_chunk(self, adapter):
        raw = {
            "id": "v_chunk",
            "candidates": [
                {"content": {"parts": [{"text": "Hello"}]}, "finishReason": None}
            ],
            "model": "gemini-1.5-pro",
        }
        chunk = adapter.translate_stream_chunk(raw)
        assert isinstance(chunk, StreamChunk)
        assert chunk.choices[0]["delta"]["content"] == "Hello"
        assert chunk.is_final is False

    def test_final_chunk(self, adapter):
        raw = {
            "id": "v_chunk",
            "candidates": [
                {"content": {"parts": [{"text": ""}]}, "finishReason": "STOP"}
            ],
            "model": "gemini-1.5-pro",
        }
        chunk = adapter.translate_stream_chunk(raw)
        assert chunk.is_final is True
        assert chunk.choices[0]["finish_reason"] == "STOP"

    def test_empty_candidates(self, adapter):
        chunk = adapter.translate_stream_chunk({"id": "x", "candidates": [], "model": "m"})
        assert chunk.is_final is False


# --- list_models ---


class TestListModels:
    @pytest.mark.asyncio
    async def test_returns_known_models(self, adapter):
        models = await adapter.list_models()
        assert len(models) > 0
        assert all(isinstance(m, ModelInfo) for m in models)
        assert all(m.provider == "vertex_ai" for m in models)
        model_ids = [m.model_id for m in models]
        assert "gemini-1.5-pro" in model_ids
        assert "gemini-1.5-flash" in model_ids


# --- health_check ---


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_returns_healthy(self, adapter):
        health = await adapter.health_check()
        assert health.provider == "vertex_ai"
        assert health.status == HealthStatus.HEALTHY
        assert health.last_check is not None
