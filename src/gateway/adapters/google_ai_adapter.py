"""Google AI Studio (Generative Language API) provider adapter."""

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

PROVIDER_NAME = "google_ai"

_GOOGLE_AI_MODELS = [
    ModelInfo(model_id="gemini-2.5-pro", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gemini-2.5-flash", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gemini-2.0-flash", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gemini-1.5-pro", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="gemini-1.5-flash", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
]


class GoogleAIAdapter(ProviderAdapter):
    """Translates between the unified Gateway format and Google AI Studio's Generative Language API.

    Uses the same request/response format as Vertex AI (contents + generationConfig)
    but authenticates with a simple API key passed as a query parameter.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _GOOGLE_AI_MODELS

    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        contents = []
        system_text = request.system
        for msg in request.messages:
            role = msg.get("role", "user")
            if role == "system":
                if system_text is None:
                    system_text = msg.get("content", "")
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({
                "role": gemini_role,
                "parts": [{"text": msg.get("content", "")}],
            })

        payload: dict = {
            "contents": contents,
        }

        if system_text is not None:
            payload["systemInstruction"] = {
                "parts": [{"text": system_text}],
            }

        gen_config: dict = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            gen_config["maxOutputTokens"] = request.max_tokens
        if request.top_p is not None:
            gen_config["topP"] = request.top_p
        if request.stop is not None:
            gen_config["stopSequences"] = request.stop

        if gen_config:
            payload["generationConfig"] = gen_config

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        candidates = provider_response.get("candidates", [])
        combined_text = ""
        finish_reason = "stop"
        if candidates:
            first = candidates[0]
            parts = first.get("content", {}).get("parts", [])
            text_parts = [p.get("text", "") for p in parts]
            combined_text = "".join(text_parts)
            finish_reason = first.get("finishReason", "stop")

        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": combined_text},
                "finish_reason": finish_reason,
            }
        ]

        usage_data = provider_response.get("usageMetadata", {})
        prompt_tokens = usage_data.get("promptTokenCount", 0)
        completion_tokens = usage_data.get("candidatesTokenCount", 0)

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
        candidates = chunk.get("candidates", [])
        delta_content = ""
        is_final = False

        if candidates:
            first = candidates[0]
            parts = first.get("content", {}).get("parts", [])
            text_parts = [p.get("text", "") for p in parts]
            delta_content = "".join(text_parts)
            if first.get("finishReason") is not None:
                is_final = True

        choices = [
            {
                "index": 0,
                "delta": {"content": delta_content} if delta_content else {},
                "finish_reason": candidates[0].get("finishReason") if candidates else None,
            }
        ]

        return StreamChunk(
            id=chunk.get("id", ""),
            choices=choices,
            model=chunk.get("model", ""),
            is_final=is_final,
        )
