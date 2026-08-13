"""Tests for the stable embedded AxonLLM routing API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from axonllm import (
    AsyncRouter,
    ChatCompletionResponse,
    EmbeddingData,
    EmbeddingResponse,
    InvalidRequestError,
    ProviderError,
    RouterClosedError,
    StreamChunk,
    TokenUsage,
)


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    models = tmp_path / "models.yaml"
    models.write_text(
        """
models:
  - name: balanced
    description: Multi-provider test model
    capabilities: [chat, streaming, tools]
    routing_strategy: round-robin
    providers:
      - provider: openai
        model_id: openai-test
        fallback_order: 0
      - provider: anthropic
        model_id: anthropic-test
        fallback_order: 1
""",
        encoding="utf-8",
    )
    providers = tmp_path / "providers.yaml"
    providers.write_text(
        """
providers:
  openai:
    base_url: https://openai.example
    auth_type: api_key
    api_key: test-openai
  anthropic:
    base_url: https://anthropic.example
    auth_type: api_key
    api_key: test-anthropic
""",
        encoding="utf-8",
    )
    return models, providers


def _router(tmp_path: Path) -> AsyncRouter:
    models, providers = _write_config(tmp_path)
    return AsyncRouter.from_files(
        models=models,
        providers=providers,
        enabled_providers={"openai", "anthropic"},
        max_retries=0,
    )


@pytest.mark.asyncio
async def test_public_router_uses_existing_multi_provider_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router = _router(tmp_path)
    calls: list[str] = []

    def create(request):
        async def provider_fn(mapping):
            calls.append(mapping.provider)
            if mapping.provider == "openai":
                raise ProviderError(503, "openai", "temporarily unavailable")
            return ChatCompletionResponse(
                id="completion-1",
                choices=[
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "routed",
                        },
                        "finish_reason": "stop",
                    }
                ],
                usage=TokenUsage(2, 1, 3),
                model=mapping.model_id,
                provider=mapping.provider,
            )

        return provider_fn

    monkeypatch.setattr(router._provider_factory, "create", create)

    try:
        response = await router.chat.completions.create(
            model="balanced",
            messages=[{"role": "user", "content": "hello"}],
        )
    finally:
        await router.close()

    assert isinstance(response, ChatCompletionResponse)
    assert calls == ["openai", "anthropic"]
    assert response.model == "balanced"
    assert response.provider == "anthropic"
    assert response.provider_model == "anthropic-test"


@pytest.mark.asyncio
async def test_public_router_validates_requests_and_lists_available_models(
    tmp_path: Path,
) -> None:
    router = _router(tmp_path)
    try:
        models = await router.models.list()
        route_snapshot = router.route_snapshot()
        with pytest.raises(InvalidRequestError) as exc_info:
            await router.chat.completions.create(
                model="missing",
                messages=[{"role": "user", "content": "hello"}],
            )
    finally:
        await router.close()

    assert [model.name for model in models] == ["balanced"]
    assert models[0].providers == ["anthropic", "openai"]
    assert models[0].capabilities == ["chat", "streaming", "tools"]
    assert "test-openai" not in repr(route_snapshot)
    assert "test-anthropic" not in repr(route_snapshot)
    assert exc_info.value.errors[0].field == "model"


def test_public_router_rejects_partial_model_configuration(
    tmp_path: Path,
) -> None:
    models, providers = _write_config(tmp_path)
    models.write_text(
        """
models:
  - name: valid
    description: Valid model
    providers:
      - provider: openai
        model_id: openai-test
  - name: invalid
    description: Invalid model
    routing_strategy: not-a-strategy
    providers:
      - provider: anthropic
        model_id: anthropic-test
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid model registry snapshot"):
        AsyncRouter.from_files(models=models, providers=providers)


@pytest.mark.asyncio
async def test_public_router_streams_and_rewrites_logical_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router = _router(tmp_path)
    attempts: list[str] = []

    async def execute_streaming(
        request,
        mapping,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        attempts.append(mapping.provider)
        if mapping.provider == "openai":
            raise ProviderError(503, "openai", "stream unavailable")
        yield StreamChunk(
            id="chunk-1",
            choices=[
                {
                    "index": 0,
                    "delta": {"content": "hello"},
                    "finish_reason": None,
                }
            ],
            model=mapping.model_id,
        )
        yield StreamChunk(
            id="chunk-1",
            choices=[
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
            model=mapping.model_id,
            is_final=True,
        )

    monkeypatch.setattr(
        router._provider_factory,
        "execute_streaming",
        execute_streaming,
    )

    try:
        stream = await router.chat.completions.create(
            model="balanced",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        assert not isinstance(stream, ChatCompletionResponse)
        chunks = [chunk async for chunk in stream]
    finally:
        await router.close()

    assert attempts == ["openai", "anthropic"]
    assert [chunk.model for chunk in chunks] == ["balanced", "balanced"]
    assert chunks[-1].is_final


@pytest.mark.asyncio
async def test_public_router_simulates_streaming_when_native_sse_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router = _router(tmp_path)
    request_stream_flags: list[bool] = []

    async def execute_streaming(request, mapping, **kwargs):
        if False:
            yield
        raise ProviderError(
            501,
            mapping.provider,
            "native streaming unavailable",
            retryable=False,
            provider_unavailable=False,
        )

    def create(request):
        request_stream_flags.append(request.stream)

        async def provider_fn(mapping):
            return ChatCompletionResponse(
                id="completion-buffered",
                choices=[
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "buffered response",
                        },
                        "finish_reason": "stop",
                    }
                ],
                usage=TokenUsage(2, 2, 4),
                model=mapping.model_id,
                provider=mapping.provider,
            )

        return provider_fn

    monkeypatch.setattr(
        router._provider_factory,
        "execute_streaming",
        execute_streaming,
    )
    monkeypatch.setattr(router._provider_factory, "create", create)

    try:
        stream = await router.chat.completions.create(
            model="balanced",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        assert not isinstance(stream, ChatCompletionResponse)
        chunks = [chunk async for chunk in stream]
    finally:
        await router.close()

    assert request_stream_flags == [False]
    assert "".join(
        chunk.choices[0]["delta"].get("content", "")
        for chunk in chunks
    ) == "buffered response"
    assert all(chunk.model == "balanced" for chunk in chunks)
    assert chunks[-1].is_final


@pytest.mark.asyncio
async def test_public_router_cleanup_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    router = _router(tmp_path)
    close_calls = 0

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(router._provider_factory, "close", close)

    await router.close()
    await router.close()

    assert close_calls == 1
    with pytest.raises(RouterClosedError):
        await router.models.list()


@pytest.mark.asyncio
async def test_public_router_exposes_routed_embeddings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    models = tmp_path / "embedding-models.yaml"
    models.write_text(
        """
models:
  - name: text-embedding
    description: Multi-provider embedding model
    capabilities: [embeddings]
    routing_strategy: round-robin
    providers:
      - provider: openai
        model_id: text-embedding-openai
        fallback_order: 0
      - provider: azure_openai
        model_id: text-embedding-azure
        fallback_order: 1
""",
        encoding="utf-8",
    )
    providers = tmp_path / "embedding-providers.yaml"
    providers.write_text(
        """
providers:
  openai:
    base_url: https://openai.example
    auth_type: api_key
    api_key: test-openai
  azure_openai:
    base_url: https://azure.example
    auth_type: azure_key
    api_key: test-azure
""",
        encoding="utf-8",
    )
    router = AsyncRouter.from_files(
        models=models,
        providers=providers,
        max_retries=0,
    )
    calls: list[str] = []

    def create_embeddings(request):
        async def provider_fn(mapping):
            calls.append(mapping.provider)
            if mapping.provider == "openai":
                raise ProviderError(503, "openai", "unavailable")
            return EmbeddingResponse(
                id="embedding-1",
                data=[EmbeddingData(index=0, embedding=[0.25, 0.75])],
                usage=TokenUsage(3, 0, 3),
                model=mapping.model_id,
                provider=mapping.provider,
            )

        return provider_fn

    monkeypatch.setattr(
        router._provider_factory,
        "create_embeddings",
        create_embeddings,
    )

    try:
        response = await router.embeddings.create(
            model="text-embedding",
            input="hello",
            preferred_provider="openai",
        )
    finally:
        await router.close()

    assert calls == ["openai", "azure_openai"]
    assert response.model == "text-embedding"
    assert response.provider == "azure_openai"
    assert response.provider_model == "text-embedding-azure"
    assert response.data == [
        EmbeddingData(index=0, embedding=[0.25, 0.75])
    ]
