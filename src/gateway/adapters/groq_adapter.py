"""Groq provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "groq"

_GROQ_MODELS = [
    ModelInfo(model_id="openai/gpt-oss-120b", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="openai/gpt-oss-20b", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
]


class GroqAdapter(OpenAIStyleAdapter):
    """Groq API — OpenAI-compatible format with ultra-fast inference."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _GROQ_MODELS
