"""Unified multi-provider factory that routes to the correct backend per provider.

- Bedrock providers use boto3 (invoke_model / converse API)
- All other providers use the generic HttpClient with ProviderConfig
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.adapters.cohere_adapter import CohereAdapter
from src.gateway.adapters.mantle_adapter import MantleAdapter
from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.azure_adapter import AzureOpenAIAdapter
from src.gateway.adapters.registry import AdapterRegistry
from src.gateway.adapters.vertex_adapter import VertexAIAdapter
from src.gateway.bedrock_provider import create_bedrock_provider_fn
from src.gateway.http_client import HttpClient
from src.gateway.mantle_provider import create_mantle_provider_fn
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
)
from src.gateway.provider_config import ProviderConfig
from src.gateway.router import ProviderError


class MultiProviderFactory:
    """Creates provider_fn callables that dispatch to the right backend.

    Bedrock calls go through boto3. Everything else goes through HttpClient.
    """

    def __init__(
        self,
        provider_configs: dict[str, ProviderConfig] | None = None,
        bedrock_region: str = "us-east-1",
    ) -> None:
        # HTTP-based providers
        self._adapter_registry = AdapterRegistry()
        self._adapter_registry.register("openai", OpenAIAdapter())
        self._adapter_registry.register("anthropic", AnthropicAdapter())
        self._adapter_registry.register("azure_openai", AzureOpenAIAdapter())
        self._adapter_registry.register("vertex_ai", VertexAIAdapter())
        self._adapter_registry.register("cohere", CohereAdapter())
        self._adapter_registry.register("bedrock", BedrockAdapter())
        self._adapter_registry.register("bedrock-mantle", MantleAdapter())

        self._provider_configs = provider_configs or {}
        self._http_client = HttpClient()

        # Bedrock provider (boto3-based, Converse API)
        self._bedrock_create = create_bedrock_provider_fn(region=bedrock_region)

        # Bedrock Mantle provider (OpenAI-compatible Chat Completions)
        self._mantle_create = create_mantle_provider_fn(region=bedrock_region)

    def create(
        self,
        request: ChatCompletionRequest,
        prompt_caching_enabled: bool = False,
    ) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        """Return a provider_fn that dispatches based on provider type."""
        bedrock_fn = self._bedrock_create(request, prompt_caching_enabled=prompt_caching_enabled)

        mantle_fn = self._mantle_create(request, prompt_caching_enabled=prompt_caching_enabled)

        async def _provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            # Bedrock Mantle — OpenAI-compatible via SigV4
            if mapping.provider == "bedrock-mantle":
                return await mantle_fn(mapping)

            # Bedrock — boto3 Converse API
            if mapping.provider == "bedrock":
                return await bedrock_fn(mapping)

            # Everything else goes through HttpClient
            try:
                adapter = self._adapter_registry.get(mapping.provider)
            except KeyError:
                raise ProviderError(
                    status_code=500,
                    provider=mapping.provider,
                    message=f"No adapter registered for provider '{mapping.provider}'",
                )

            config = self._provider_configs.get(mapping.provider)
            if config is None:
                raise ProviderError(
                    status_code=500,
                    provider=mapping.provider,
                    message=f"No provider configuration found for '{mapping.provider}'. Add it to provider_configs.",
                )

            return await self._http_client.execute(
                request, mapping, adapter, config,
                prompt_caching_enabled=prompt_caching_enabled,
            )

        return _provider_fn
