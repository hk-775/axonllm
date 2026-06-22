"""AWS Bedrock Mantle provider adapter — OpenAI-compatible Chat Completions via bedrock-mantle endpoint."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "bedrock-mantle"

_MANTLE_MODELS = [
    ModelInfo(model_id="anthropic.claude-sonnet-4-6", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="anthropic.claude-opus-4-6-v1", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="anthropic.claude-haiku-4-5-20251001-v1:0", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="openai.gpt-5.5", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai.gpt-5.4", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="meta.llama4-maverick-17b-instruct-v1:0", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class MantleAdapter(OpenAIStyleAdapter):
    """Translates between the unified Gateway format and Bedrock Mantle's OpenAI-compatible API.

    Mantle uses the standard OpenAI Chat Completions request/response format.
    Auth is handled via SigV4 or Bedrock API key at the HTTP layer.
    """

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _MANTLE_MODELS
