"""Bedrock provider function using boto3 — bypasses the generic HTTP client.

Uses the bedrock-runtime invoke_model / converse API with proper SigV4 signing
handled automatically by boto3. Supports both Anthropic-style (Claude) and
OpenAI-style (Nova, DeepSeek) models.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import boto3

from src.gateway.adapters.bedrock_adapter import BedrockAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    TokenUsage,
)
from src.gateway.router import ProviderError

# Models that use Anthropic Messages API format
_ANTHROPIC_PREFIXES = ("anthropic.",)

# Models that use the Bedrock Converse API (Nova, DeepSeek, etc.)
_CONVERSE_MODELS = True  # default for non-Anthropic models


def _is_anthropic_model(model_id: str) -> bool:
    return any(model_id.startswith(p) or f".{p}" in model_id for p in _ANTHROPIC_PREFIXES)


def create_bedrock_provider_fn(
    region: str = "us-east-1",
) -> Callable[[ChatCompletionRequest], Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]]:
    """Return a factory that creates provider_fn callables for Bedrock."""
    client = boto3.client("bedrock-runtime", region_name=region)
    anthropic_adapter = BedrockAdapter()

    def create(request: ChatCompletionRequest, prompt_caching_enabled: bool = False) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        async def provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            try:
                if _is_anthropic_model(mapping.model_id):
                    return await _invoke_anthropic(client, anthropic_adapter, request, mapping, prompt_caching_enabled=prompt_caching_enabled)
                else:
                    return await _invoke_converse(client, request, mapping, prompt_caching_enabled=prompt_caching_enabled)
            except ProviderError:
                raise
            except client.exceptions.AccessDeniedException as exc:
                raise ProviderError(403, mapping.provider, f"Bedrock access denied: {exc}") from exc
            except client.exceptions.ResourceNotFoundException as exc:
                raise ProviderError(404, mapping.provider, f"Model not found: {exc}") from exc
            except client.exceptions.ThrottlingException as exc:
                raise ProviderError(429, mapping.provider, f"Bedrock throttled: {exc}") from exc
            except Exception as exc:
                raise ProviderError(502, mapping.provider, f"Bedrock error: {exc}") from exc

        return provider_fn

    return create


async def _invoke_anthropic(
    client, adapter: BedrockAdapter, request: ChatCompletionRequest, mapping: ProviderModelMapping,
    prompt_caching_enabled: bool = False,
) -> ChatCompletionResponse:
    """Invoke Anthropic-style models (Claude) via invoke_model."""
    payload = await adapter.translate_request(request, prompt_caching_enabled=prompt_caching_enabled)
    payload.pop("stream", None)
    payload.pop("model", None)
    payload.pop("_warnings", None)
    payload["anthropic_version"] = "bedrock-2023-05-31"

    body = json.dumps(payload)

    def _call():
        return client.invoke_model(
            modelId=mapping.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

    response = await asyncio.to_thread(_call)
    response_body = json.loads(response["body"].read())
    result = adapter.translate_response(response_body)
    return ChatCompletionResponse(
        id=result.id or "bedrock-response",
        choices=result.choices,
        usage=result.usage,
        model=mapping.model_id,
        provider=mapping.provider,
    )


async def _invoke_converse(
    client, request: ChatCompletionRequest, mapping: ProviderModelMapping,
    prompt_caching_enabled: bool = False,
) -> ChatCompletionResponse:
    """Invoke non-Anthropic models (Nova, DeepSeek) via the Converse API."""
    messages = []
    system_parts = []

    for msg in request.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append({"text": content})
        else:
            messages.append({"role": role, "content": [{"text": content}]})

    if request.system:
        system_parts.insert(0, {"text": request.system})

    kwargs: dict = {
        "modelId": mapping.model_id,
        "messages": messages,
    }
    if system_parts:
        # Add cache_control to last system block for Anthropic models when caching is enabled
        if prompt_caching_enabled and _is_anthropic_model(mapping.model_id) and system_parts:
            system_parts[-1]["cache_control"] = {"type": "ephemeral"}
        kwargs["system"] = system_parts

    inference_config: dict = {}
    if request.max_tokens is not None:
        inference_config["maxTokens"] = request.max_tokens
    else:
        inference_config["maxTokens"] = 4096
    if request.temperature is not None:
        inference_config["temperature"] = request.temperature
    if request.top_p is not None:
        inference_config["topP"] = request.top_p
    if inference_config:
        kwargs["inferenceConfig"] = inference_config

    def _call():
        return client.converse(**kwargs)

    response = await asyncio.to_thread(_call)

    # Parse Converse API response
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks)

    usage_data = response.get("usage", {})
    prompt_tokens = usage_data.get("inputTokens", 0)
    completion_tokens = usage_data.get("outputTokens", 0)

    return ChatCompletionResponse(
        id=response.get("ResponseMetadata", {}).get("RequestId", "bedrock-response"),
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": response.get("stopReason", "stop"),
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )
