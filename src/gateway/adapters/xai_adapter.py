"""xAI (Grok) provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "xai"

_XAI_MODELS = [
    ModelInfo(model_id="grok-3", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="grok-3-mini", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="grok-2-vision-1212", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "vision"]),
]


class XAIAdapter(OpenAIStyleAdapter):
    """xAI Grok API — OpenAI-compatible format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _XAI_MODELS
