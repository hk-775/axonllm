"""Cohere provider adapter for the LLM-Router."""

import logging

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    StreamChunk,
    TokenUsage,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "cohere"

_COHERE_MODELS = [
    ModelInfo(model_id="command-r-plus", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="command-r", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="command-light", provider=PROVIDER_NAME, capabilities=["chat"]),
]


class CohereAdapter(ProviderAdapter):
    """Translates between the unified Gateway format and Cohere's native chat API format.

    Cohere uses: message (last user message), chat_history (previous messages),
    preamble (system message), temperature, max_tokens, p (top_p), stop_sequences.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _COHERE_MODELS

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        warnings: list[str] = []

        preamble = request.system
        chat_history: list[dict] = []
        last_user_message = ""

        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if preamble is None:
                    preamble = content
                continue
            cohere_role = "CHATBOT" if role == "assistant" else "USER"
            chat_history.append({"role": cohere_role, "message": content})

        if chat_history and chat_history[-1]["role"] == "USER":
            last_user_message = chat_history.pop()["message"]

        payload: dict = {
            "message": last_user_message,
            "model": request.model,
        }

        if chat_history:
            payload["chat_history"] = chat_history
        if preamble is not None:
            payload["preamble"] = preamble
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = request.stop

        if request.stream:
            warnings.append("Parameter 'stream' is not natively supported by Cohere; handled at gateway level")

        if warnings:
            payload["_warnings"] = warnings

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        text = provider_response.get("text", "")

        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": provider_response.get("finish_reason", "stop"),
            }
        ]

        meta = provider_response.get("meta", {})
        tokens = meta.get("tokens", {})
        prompt_tokens = tokens.get("input_tokens", 0)
        completion_tokens = tokens.get("output_tokens", 0)

        return ChatCompletionResponse(
            id=provider_response.get("id", ""),
            choices=choices,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            model=provider_response.get("model", ""),
            provider=PROVIDER_NAME,
        )

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        event_type = chunk.get("event_type", "")
        delta_content = ""
        is_final = False

        if event_type == "text-generation":
            delta_content = chunk.get("text", "")
        elif event_type == "stream-end":
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
