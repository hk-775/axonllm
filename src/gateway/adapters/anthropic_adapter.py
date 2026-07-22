"""Anthropic provider adapter for the LLM-Router."""

import logging

from src.gateway.adapters.anthropic_style import AnthropicStyleAdapter
from src.gateway.models import ModelInfo, StreamChunk, TokenUsage

logger = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"

_ANTHROPIC_MODELS = [
    ModelInfo(model_id="claude-3-opus-20240229", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="claude-3-sonnet-20240229", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="claude-3-haiku-20240307", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class AnthropicAdapter(AnthropicStyleAdapter):
    """Translates between the unified Gateway format and Anthropic's native API format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _ANTHROPIC_MODELS

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        """Anthropic streaming uses message_start with nested message.id/model.

        Usage arrives in two events: ``message_start`` carries input_tokens (and
        cache tokens) in message.usage; ``message_delta`` carries the cumulative
        output_tokens. We attach whatever usage the event carries; the agent's
        end-of-stream accumulator merges input+output across the stream.
        """
        chunk_type = chunk.get("type", "")
        delta_content = ""
        is_final = False
        usage = None

        if chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            delta_content = delta.get("text", "")
        elif chunk_type == "message_start":
            u = chunk.get("message", {}).get("usage", {}) or {}
            if u:
                usage = TokenUsage(
                    prompt_tokens=u.get("input_tokens", 0),
                    completion_tokens=u.get("output_tokens", 0),
                    total_tokens=u.get("input_tokens", 0) + u.get("output_tokens", 0),
                    cached_tokens=u.get("cache_read_input_tokens", 0),
                    cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
                )
        elif chunk_type == "message_delta":
            u = chunk.get("usage", {}) or {}
            if u:
                usage = TokenUsage(
                    prompt_tokens=u.get("input_tokens", 0),
                    completion_tokens=u.get("output_tokens", 0),
                    total_tokens=u.get("input_tokens", 0) + u.get("output_tokens", 0),
                )
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
            id=(
                chunk.get("message", {}).get("id", "")
                if chunk_type == "message_start"
                else chunk.get("id", "")
            ),
            choices=choices,
            model=(
                chunk.get("message", {}).get("model", "")
                if chunk_type == "message_start"
                else chunk.get("model", "")
            ),
            is_final=is_final,
            usage=usage,
        )
