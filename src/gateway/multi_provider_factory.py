"""Unified multi-provider factory that routes to the correct backend per provider.

- Bedrock providers use boto3 (invoke_model / converse API)
- All other providers use the generic HttpClient with ProviderConfig
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from src.gateway.adapters.ai21_adapter import AI21Adapter
from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.adapters.cohere_adapter import CohereAdapter
from src.gateway.adapters.fireworks_adapter import FireworksAdapter
from src.gateway.adapters.google_ai_adapter import GoogleAIAdapter
from src.gateway.adapters.groq_adapter import GroqAdapter
from src.gateway.adapters.mantle_adapter import MantleAdapter
from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.azure_adapter import AzureOpenAIAdapter
from src.gateway.adapters.registry import AdapterRegistry
from src.gateway.adapters.together_adapter import TogetherAdapter
from src.gateway.adapters.vertex_adapter import VertexAIAdapter
from src.gateway.adapters.xai_adapter import XAIAdapter
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

if TYPE_CHECKING:
    from src.gateway.multi_region.region_config import SpokeConfig


class MultiProviderFactory:
    """Creates provider_fn callables that dispatch to the right backend.

    Bedrock calls go through boto3. Everything else goes through HttpClient.
    """

    def __init__(
        self,
        provider_configs: dict[str, ProviderConfig] | None = None,
        bedrock_region: str = "us-east-1",
        enabled_providers: frozenset[str] | None = None,
    ) -> None:
        # HTTP-based providers
        self._adapter_registry = AdapterRegistry()
        self._adapter_registry.register("openai", OpenAIAdapter())
        self._adapter_registry.register("anthropic", AnthropicAdapter())
        self._adapter_registry.register("azure_openai", AzureOpenAIAdapter())
        self._adapter_registry.register("vertex_ai", VertexAIAdapter())
        self._adapter_registry.register("cohere", CohereAdapter())
        self._adapter_registry.register("google_ai", GoogleAIAdapter())
        self._adapter_registry.register("bedrock", BedrockAdapter())
        self._adapter_registry.register("bedrock-mantle", MantleAdapter())
        self._adapter_registry.register("xai", XAIAdapter())
        self._adapter_registry.register("groq", GroqAdapter())
        self._adapter_registry.register("together", TogetherAdapter())
        self._adapter_registry.register("fireworks", FireworksAdapter())
        self._adapter_registry.register("ai21", AI21Adapter())

        self._provider_configs = provider_configs or {}
        available = {
            "bedrock",
            "bedrock-mantle",
            *self._provider_configs,
        }
        if enabled_providers is not None:
            available.intersection_update(enabled_providers)
        self._available_providers = frozenset(available)
        self._http_client = HttpClient()
        self._bedrock_region = bedrock_region

        # Bedrock provider (boto3-based, Converse API)
        self._bedrock_create = create_bedrock_provider_fn(region=bedrock_region)

        # Bedrock Mantle provider (OpenAI-compatible Chat Completions)
        self._mantle_create = create_mantle_provider_fn(region=bedrock_region)

        # Per-region bedrock/mantle fn factories, built on demand for spokes in
        # a region other than the default (boto3 clients bake in their region).
        self._bedrock_by_region: dict[str, Callable] = {bedrock_region: self._bedrock_create}
        self._mantle_by_region: dict[str, Callable] = {bedrock_region: self._mantle_create}

    @property
    def available_providers(self) -> frozenset[str]:
        """Providers that this process has credentials and permission to invoke."""
        return self._available_providers

    def config_for(
        self, provider: str, spoke: SpokeConfig | None = None,
    ) -> ProviderConfig | None:
        """Provider config with a region-spoke override applied (endpoint/region).

        Mirrors ProviderFnFactory.config_for so multi-region routing targets the
        selected spoke's endpoint/region for HTTP providers.
        """
        base = self._provider_configs.get(provider)
        if base is None or spoke is None:
            return base
        overrides: dict = {}
        if spoke.endpoint:
            overrides["base_url"] = spoke.endpoint
        if spoke.region and base.auth_type == "aws_credentials":
            creds = dict(base.credentials)
            creds["region"] = spoke.region
            overrides["credentials"] = creds
        if not overrides:
            return base
        return dataclasses.replace(base, **overrides)

    def _bedrock_fn_for(self, region: str) -> Callable:
        fn = self._bedrock_by_region.get(region)
        if fn is None:
            fn = create_bedrock_provider_fn(region=region)
            self._bedrock_by_region[region] = fn
        return fn

    def _mantle_fn_for(self, region: str) -> Callable:
        fn = self._mantle_by_region.get(region)
        if fn is None:
            fn = create_mantle_provider_fn(region=region)
            self._mantle_by_region[region] = fn
        return fn

    def create(
        self,
        request: ChatCompletionRequest,
        prompt_caching_enabled: bool = False,
        spoke: SpokeConfig | None = None,
    ) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        """Return a provider_fn that dispatches based on provider type.

        When multi-region routing supplies a ``spoke`` in a non-default region,
        Bedrock/Mantle calls use a client bound to that region and HTTP-provider
        calls use the spoke's endpoint/region override.
        """
        # Bedrock/Mantle are region-bound at the boto3 client; pick the region's
        # client when the spoke names a different region.
        region = spoke.region if (spoke and spoke.region) else self._bedrock_region
        bedrock_fn = self._bedrock_fn_for(region)(
            request, prompt_caching_enabled=prompt_caching_enabled)
        mantle_fn = self._mantle_fn_for(region)(
            request, prompt_caching_enabled=prompt_caching_enabled)

        async def _provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            if mapping.provider not in self._available_providers:
                raise ProviderError(
                    status_code=503,
                    provider=mapping.provider,
                    message=(
                        f"Provider '{mapping.provider}' is disabled or "
                        "not configured in this deployment"
                    ),
                )
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

            config = self.config_for(mapping.provider, spoke)
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
