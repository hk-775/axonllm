"""Shared base for Anthropic-compatible adapters (Anthropic, Bedrock).

Both providers use system as a separate field, content blocks for responses,
and stop_sequences instead of stop. Subclasses override only provider-specific
response field names and streaming nuances.
"""

from datetime import datetime, timezone

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.config import DEFAULT_CONFIG
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    HealthStatus,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
    TokenUsage,
)


class AnthropicStyleAdapter(ProviderAdapter):
    """Base adapter for providers that use the Anthropic request/response format.

    Subclasses must define:
        PROVIDER_NAME: str
        _MODELS: list[ModelInfo]

    Subclasses may override:
        _prompt_tokens_key / _completion_tokens_key for response parsing.
        translate_stream_chunk for provider-specific streaming differences.
    """

    PROVIDER_NAME: str = ""
    _MODELS: list[ModelInfo] = []

    # Response usage field names — Anthropic uses input_tokens/output_tokens,
    # Bedrock may use inputTokens/outputTokens as well.
    _prompt_tokens_key: str = "input_tokens"
    _completion_tokens_key: str = "output_tokens"
    # Bedrock alternate keys (checked as fallback)
    _prompt_tokens_alt: str | None = None
    _completion_tokens_alt: str | None = None

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        warnings: list[str] = []

        system_text = request.system
        messages = []
        for msg in request.messages:
            if msg.get("role") == "system":
                if system_text is None:
                    system_text = msg.get("content", "")
            else:
                messages.append(msg)

        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else DEFAULT_CONFIG.adapter.default_max_tokens
        )

        payload: dict = {
            "model": request.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if system_text is not None:
            if prompt_caching_enabled:
                # Convert system to content-block array with cache_control on last block
                if isinstance(system_text, str):
                    payload["system"] = [
                        {
                            "type": "text",
                            "text": system_text,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                elif isinstance(system_text, list):
                    # Already a list of content blocks — add cache_control to last block only
                    blocks = []
                    for i, block in enumerate(system_text):
                        new_block = {k: v for k, v in block.items() if k != "cache_control"}
                        if i == len(system_text) - 1:
                            new_block["cache_control"] = {"type": "ephemeral"}
                        blocks.append(new_block)
                    payload["system"] = blocks
            else:
                payload["system"] = system_text
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop
        if request.stream:
            payload["stream"] = True

        if warnings:
            payload["_warnings"] = warnings

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        content_blocks = provider_response.get("content", [])
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        ]
        combined_text = "".join(text_parts)

        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": combined_text},
                "finish_reason": provider_response.get("stop_reason", "stop"),
            }
        ]

        usage_data = provider_response.get("usage", {})
        prompt_tokens = usage_data.get(self._prompt_tokens_key, 0)
        if not prompt_tokens and self._prompt_tokens_alt:
            prompt_tokens = usage_data.get(self._prompt_tokens_alt, 0)
        completion_tokens = usage_data.get(self._completion_tokens_key, 0)
        if not completion_tokens and self._completion_tokens_alt:
            completion_tokens = usage_data.get(self._completion_tokens_alt, 0)

        cached_tokens = usage_data.get("cache_read_input_tokens", 0)
        cache_creation_tokens = usage_data.get("cache_creation_input_tokens", 0)

        return ChatCompletionResponse(
            id=provider_response.get("id", ""),
            choices=choices,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
            ),
            model=provider_response.get("model", ""),
            provider=self.PROVIDER_NAME,
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        chunk_type = chunk.get("type", "")
        delta_content = ""
        is_final = False

        if chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            delta_content = delta.get("text", "")
        elif chunk_type == "message_stop":
            is_final = True

        choices = [
            {
                "index": 0,
                "delta": {"content": delta_content} if delta_content else {},
                "finish_reason": "stop" if is_final else None,
            }
        ]

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
