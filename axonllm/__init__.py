"""Public AxonLLM routing API."""

from importlib.metadata import PackageNotFoundError, version

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelSummary,
    StreamChunk,
    TokenUsage,
    ValidationError,
)
from src.gateway.router import AllProvidersExhaustedError, ProviderError

from .router import AsyncRouter, InvalidRequestError, RouterClosedError

try:
    __version__ = version("axon-llm")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = [
    "AllProvidersExhaustedError",
    "AsyncRouter",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "InvalidRequestError",
    "ModelSummary",
    "ProviderError",
    "RouterClosedError",
    "StreamChunk",
    "TokenUsage",
    "ValidationError",
    "__version__",
]
