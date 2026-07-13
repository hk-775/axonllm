"""Together AI provider adapter — OpenAI-compatible."""

from src.gateway.adapters.openai_style import OpenAIStyleAdapter
from src.gateway.models import ModelInfo

PROVIDER_NAME = "together"

_TOGETHER_MODELS = [
    ModelInfo(model_id="meta-llama/Llama-3.3-70B-Instruct-Turbo", provider=PROVIDER_NAME, capabilities=["chat", "streaming", "function_calling"]),
    ModelInfo(model_id="meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="deepseek-ai/DeepSeek-R1", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="Qwen/Qwen2.5-72B-Instruct-Turbo", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="mistralai/Mistral-Small-24B-Instruct-2501", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class TogetherAdapter(OpenAIStyleAdapter):
    """Together AI API — OpenAI-compatible format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _TOGETHER_MODELS
