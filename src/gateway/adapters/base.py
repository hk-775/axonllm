"""Abstract base class for provider adapters."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    HealthStatus,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
)


class ProviderAdapter(ABC):
    """Interface that all provider adapters must implement.

    Subclasses that set PROVIDER_NAME and _MODELS get default
    implementations of list_models() and health_check().
    """

    PROVIDER_NAME: str = ""
    _MODELS: list[ModelInfo] = []

    @abstractmethod
    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        """Translate unified request to provider-native format.

        Unsupported parameters are ignored and a warning is added.
        """
        ...

    @abstractmethod
    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        """Translate provider response to unified format."""
        ...

    @abstractmethod
    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        """Translate a single streaming chunk to unified SSE format."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Return available models from this provider."""
        return list(self._MODELS)

    async def health_check(self) -> ProviderHealth:
        """Check provider connectivity and return health status."""
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthStatus.HEALTHY,
            last_check=datetime.now(timezone.utc),
        )
