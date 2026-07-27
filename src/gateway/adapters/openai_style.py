"""Shared base for OpenAI-compatible adapters (OpenAI, Azure OpenAI).

Both providers use the same request/response/streaming format.
Subclasses only need to set PROVIDER_NAME and _MODELS.
"""

import re
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

# OpenAI reasoning models: o1, o3, o4 families (incl. -mini/-preview and dated
# variants like o3-2025-...). They require max_completion_tokens (not
# max_tokens) and only accept the default temperature.
_OPENAI_REASONING_RE = re.compile(r"^o[134]([.-]|$)")


def _is_openai_reasoning_model(model_id: str) -> bool:
    return bool(_OPENAI_REASONING_RE.match((model_id or "").strip().lower()))


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

        # OpenAI reasoning models (o1/o3/o4 families) reject 'max_tokens' (they
        # require 'max_completion_tokens') and only accept temperature=1. Detect
        # by model id and adjust, so smart routing to these models doesn't 400
        # and trip the provider's circuit breaker.
        is_reasoning = _is_openai_reasoning_model(request.model)

        if request.temperature is not None and not is_reasoning:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            if is_reasoning:
                payload["max_completion_tokens"] = request.max_tokens
            else:
                payload["max_tokens"] = request.max_tokens
        if request.top_p is not None and not is_reasoning:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
        # Tools are already in OpenAI's own dialect — pass them straight through.
        if request.tools:
            payload["tools"] = request.tools
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
        if request.stream:
            payload["stream"] = True
            # Ask the provider to include a final usage chunk so end-of-stream
            # cost accounting uses real token counts (else we estimate).
            payload["stream_options"] = {"include_usage": True}

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

        # With stream_options.include_usage, OpenAI sends a trailing chunk that
        # has empty choices and a populated usage object. Treat it as final and
        # attach the token counts for end-of-stream cost accounting.
        usage = None
        usage_data = chunk.get("usage")
        if usage_data:
            prompt_tokens = usage_data.get("prompt_tokens", 0)
            completion_tokens = usage_data.get("completion_tokens", 0)
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
            )
            is_final = True

        return StreamChunk(
            id=chunk.get("id", ""),
            choices=choices,
            model=chunk.get("model", ""),
            is_final=is_final,
            usage=usage,
        )

    async def list_models(self) -> list[ModelInfo]:
        return list(self._MODELS)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthStatus.HEALTHY,
            last_check=datetime.now(timezone.utc),
        )
