"""Tests for ProviderAdapter base class and AdapterRegistry."""

import pytest

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.adapters.registry import AdapterRegistry
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
    HealthStatus,
    TokenUsage,
)


class FakeAdapter(ProviderAdapter):
    """Concrete adapter for testing the abstract base class."""

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        return {"messages": request.messages, "model": request.model}

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        return ChatCompletionResponse(
            id="resp-1",
            choices=provider_response.get("choices", []),
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model="test-model",
            provider="fake",
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        return StreamChunk(id="chunk-1", choices=[], model="test-model")

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="fake-model", provider="fake")]

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider="fake", status=HealthStatus.HEALTHY)


class TestProviderAdapterBase:
    """Tests that ProviderAdapter enforces the abstract interface."""

    def test_cannot_instantiate_abstract_class(self):
        with pytest.raises(TypeError):
            ProviderAdapter()  # type: ignore[abstract]

    def test_concrete_subclass_instantiates(self):
        adapter = FakeAdapter()
        assert isinstance(adapter, ProviderAdapter)

    @pytest.mark.asyncio
    async def test_translate_request(self):
        adapter = FakeAdapter()
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        result = await adapter.translate_request(req)
        assert result["model"] == "m"

    def test_translate_response(self):
        adapter = FakeAdapter()
        resp = adapter.translate_response({"choices": [{"text": "hello"}]})
        assert resp.provider == "fake"

    def test_translate_stream_chunk(self):
        adapter = FakeAdapter()
        chunk = adapter.translate_stream_chunk({})
        assert isinstance(chunk, StreamChunk)

    @pytest.mark.asyncio
    async def test_list_models(self):
        adapter = FakeAdapter()
        models = await adapter.list_models()
        assert len(models) == 1

    @pytest.mark.asyncio
    async def test_health_check(self):
        adapter = FakeAdapter()
        health = await adapter.health_check()
        assert health.status == HealthStatus.HEALTHY


class TestAdapterRegistry:
    """Tests for the AdapterRegistry."""

    def test_register_and_get(self):
        registry = AdapterRegistry()
        adapter = FakeAdapter()
        registry.register("fake", adapter)
        assert registry.get("fake") is adapter

    def test_get_unknown_provider_raises_key_error(self):
        registry = AdapterRegistry()
        with pytest.raises(KeyError, match="No adapter registered for provider 'unknown'"):
            registry.get("unknown")

    def test_register_multiple_providers(self):
        registry = AdapterRegistry()
        a1 = FakeAdapter()
        a2 = FakeAdapter()
        registry.register("provider_a", a1)
        registry.register("provider_b", a2)
        assert registry.get("provider_a") is a1
        assert registry.get("provider_b") is a2

    def test_register_overwrites_existing(self):
        registry = AdapterRegistry()
        old = FakeAdapter()
        new = FakeAdapter()
        registry.register("p", old)
        registry.register("p", new)
        assert registry.get("p") is new

    def test_lazy_registration_constructs_once_on_first_use(self):
        registry = AdapterRegistry()
        built: list[FakeAdapter] = []

        def factory() -> FakeAdapter:
            adapter = FakeAdapter()
            built.append(adapter)
            return adapter

        registry.register_lazy("lazy", factory)

        assert built == []
        assert registry.get("lazy") is registry.get("lazy")
        assert len(built) == 1

    def test_error_message_lists_available_providers(self):
        registry = AdapterRegistry()
        registry.register("openai", FakeAdapter())
        registry.register("anthropic", FakeAdapter())
        with pytest.raises(KeyError, match="Available providers:"):
            registry.get("bedrock")
