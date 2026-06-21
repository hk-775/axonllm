"""Shared base for OpenAI-compatible adapters (OpenAI, Azure OpenAI).

Both providers use the same request/response/streaming format.
Subclasses only need to set PROVIDER_NAME and _MODELS.
"""

from datetime import datetime, timezone

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    HealthStatus,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
    TokenUsage,
)


class OpenAIStyleAdapter(ProviderAdapter):
    """Base adapter for providers that use the OpenAI request/response format.

    Subclasses must define:
        PROVIDER_NAME: str
        _MODELS: list[ModelInfo]
    """

    PROVIDER_NAME: str = ""
    _MODELS: list[ModelInfo] = []

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        messages = list(request.messages)

        if request.system:
            messages = [{"role": "system", "content": request.system}, *messages]

        payload: dict = {
            "messages": messages,
            "model": request.model,
        }

        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
        if request.stream:
            payload["stream"] = True

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        usage_data = provider_response.get("usage", {})
        prompt_tokens = usage_data.get("prompt_tokens", 0)
        completion_tokens = usage_data.get("completion_tokens", 0)

        return ChatCompletionResponse(
            id=provider_response.get("id", ""),
            choices=provider_response.get("choices", []),
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            model=provider_response.get("model", ""),
            provider=self.PROVIDER_NAME,
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        choices = chunk.get("choices", [])
        is_final = bool(choices and choices[-1].get("finish_reason") is not None)

        return StreamChunk(
            id=chunk.get("id", ""),
            choices=choices,
            model=chunk.get("model", ""),
            is_final=is_final,
        )

    async def list_models(self) -> list[ModelInfo]:
        return list(self._MODELS)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthStatus.HEALTHY,
            last_check=datetime.now(timezone.utc),
        )
