"""Perplexity provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "perplexity"

_PERPLEXITY_MODELS = [
    ModelInfo(model_id="sonar-pro", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "search"]),
    ModelInfo(model_id="sonar-reasoning-pro", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "search", "reasoning"]),
    ModelInfo(model_id="sonar", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "search"]),
]


class PerplexityAdapter(OpenAIStyleAdapter):
    """Perplexity API — OpenAI-compatible format with search augmentation."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _PERPLEXITY_MODELS
