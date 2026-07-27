"""Cohere provider adapter for the LLM-Router."""

import json
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
        tool_results: list[dict] = []

        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if preamble is None:
                    preamble = content
                continue
            # Cohere carries tool output in a top-level `tool_results` field, not
            # as a history turn — and pairs each result with the call that
            # produced it. Keeping it out of chat_history also stops a tool
            # result from being mistaken for the user's next message below.
            if role == "tool":
                tool_results.append({
                    "call": {"name": msg.get("name", ""), "parameters": {}},
                    "outputs": [{"output": content if isinstance(content, str)
                                 else json.dumps(content)}],
                })
                continue
            cohere_role = "CHATBOT" if role == "assistant" else "USER"
            entry: dict = {"role": cohere_role, "message": content or ""}
            # An assistant turn that called tools has content=None; record the
            # calls so the model sees its own prior turn.
            if role == "assistant" and msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {"name": (tc.get("function") or {}).get("name", tc.get("name", "")),
                     "parameters": _cohere_args((tc.get("function") or {}).get(
                         "arguments", tc.get("arguments", {})))}
                    for tc in msg["tool_calls"]
                ]
            chat_history.append(entry)

        if chat_history and chat_history[-1]["role"] == "USER":
            last_user_message = chat_history.pop()["message"]

        payload: dict = {
            "message": last_user_message,
            "model": request.model,
        }

        if tool_results:
            # Cohere requires message to be empty when tool_results is set — the
            # turn *is* the tool output, not new user text.
            payload["tool_results"] = tool_results

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
        if request.tools:
            payload["tools"] = [_openai_tool_to_cohere(t) for t in request.tools]
            # Cohere's v1 chat has no tool_choice equivalent — the model always
            # decides. Say so rather than dropping the caller's instruction
            # silently, which is how tools went missing in the first place.
            if request.tool_choice not in (None, "auto"):
                warnings.append(
                    f"Parameter 'tool_choice'={request.tool_choice!r} is not supported by "
                    "Cohere v1 chat; the model chooses whether to call a tool"
                )

        if request.stream:
            warnings.append("Parameter 'stream' is not natively supported by Cohere; handled at gateway level")

        if warnings:
            payload["_warnings"] = warnings

        return payload

    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        text = provider_response.get("text", "")

        tool_calls = [
            {
                # Cohere returns no call id; synthesize a stable one for the
                # round-trip (the caller echoes it back, Cohere matches on name).
                "id": f"call_{tc.get('name', 'fn')}_{i}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    # Cohere calls it "parameters" and sends an object; OpenAI
                    # callers json.loads() an "arguments" string.
                    "arguments": json.dumps(tc.get("parameters", {})),
                },
            }
            for i, tc in enumerate(provider_response.get("tool_calls") or [])
        ]

        message: dict = {"role": "assistant", "content": text}
        finish_reason = provider_response.get("finish_reason", "stop")
        if tool_calls:
            message["tool_calls"] = tool_calls
            if not text:
                message["content"] = None
            finish_reason = "tool_calls"

        choices = [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
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


# --- OpenAI ⇄ Cohere tool translation ---------------------------------------


def _cohere_args(raw) -> dict:
    """OpenAI sends tool arguments as a JSON string; Cohere wants an object.

    Malformed JSON from a model must not fail the request — send {} and let the
    tool report the bad call.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError):
            return {}
    return raw or {}


def _openai_tool_to_cohere(tool: dict) -> dict:
    """Convert one OpenAI tool spec to Cohere's parameter_definitions shape.

    Cohere describes parameters one-by-one with a type/description/required
    triple rather than taking a JSON Schema object, so the schema's properties
    have to be unrolled.
    """
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    schema = fn.get("parameters") or tool.get("input_schema") or {}
    required = set(schema.get("required") or [])
    definitions = {}
    for name, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        definitions[name] = {
            # Cohere expects Python-ish type names ("str", "int", "list"), not
            # JSON Schema's. Anything unrecognized falls back to str, which the
            # model can still fill in.
            "type": _COHERE_TYPES.get(spec.get("type", "string"), "str"),
            "description": spec.get("description", ""),
            "required": name in required,
        }
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "parameter_definitions": definitions,
    }


_COHERE_TYPES = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "array": "list", "object": "dict",
}
