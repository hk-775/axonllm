"""Bedrock Mantle provider — OpenAI Responses API + Anthropic Messages API via SigV4.

Uses the bedrock-mantle.{region}.api.aws endpoint which supports:
- OpenAI Responses API (/openai/v1/responses) for OpenAI models (GPT-5.5, etc.)
- Anthropic Messages API (/v1/messages) for Claude models
- SigV4 or Bedrock API key authentication
- All billing through AWS account
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Awaitable, Callable

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    TokenUsage,
)
from src.gateway.router import ProviderError

_MANTLE_SERVICE = "bedrock"

_OPENAI_PREFIXES = ("openai.",)
_ANTHROPIC_PREFIXES = ("anthropic.",)


def _is_openai_model(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _OPENAI_PREFIXES)


def _is_anthropic_model(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _ANTHROPIC_PREFIXES)


def create_mantle_provider_fn(
    region: str = "us-east-1",
) -> Callable[[ChatCompletionRequest], Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]]:
    """Return a factory that creates provider_fn callables for Bedrock Mantle."""
    session = boto3.Session()
    credentials = session.get_credentials()
    endpoint = f"https://bedrock-mantle.{region}.api.aws"

    def create(
        request: ChatCompletionRequest, prompt_caching_enabled: bool = False
    ) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        async def provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            try:
                if _is_openai_model(mapping.model_id):
                    return await _invoke_responses_api(
                        credentials, endpoint, region, request, mapping,
                    )
                elif _is_anthropic_model(mapping.model_id):
                    return await _invoke_messages_api(
                        credentials, endpoint, region, request, mapping,
                    )
                else:
                    return await _invoke_responses_api(
                        credentials, endpoint, region, request, mapping,
                    )
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(502, mapping.provider, f"Bedrock Mantle error: {exc}") from exc

        return provider_fn

    return create


def _sigv4_request(credentials, region: str, url: str, body: str) -> dict:
    """Make a SigV4-signed POST request and return parsed JSON."""
    aws_request = AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/json",
    })
    resolved_creds = credentials.get_frozen_credentials()
    SigV4Auth(resolved_creds, _MANTLE_SERVICE, region).add_auth(aws_request)

    req = urllib.request.Request(
        url,
        data=body.encode(),
        headers=dict(aws_request.headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise ProviderError(e.code, "bedrock-mantle", f"Mantle HTTP {e.code}: {error_body[:200]}") from None


async def _invoke_responses_api(
    credentials,
    endpoint: str,
    region: str,
    request: ChatCompletionRequest,
    mapping: ProviderModelMapping,
) -> ChatCompletionResponse:
    """Call the OpenAI Responses API on Mantle for GPT models."""
    messages = list(request.messages)
    if request.system:
        instructions = request.system
    else:
        instructions = None

    input_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            instructions = content
        else:
            input_parts.append({"role": role, "content": content})

    if len(input_parts) == 1 and input_parts[0]["role"] == "user":
        input_val = input_parts[0]["content"]
    else:
        input_val = input_parts

    payload: dict = {
        "model": mapping.model_id,
        "input": input_val,
    }
    if instructions:
        payload["instructions"] = instructions
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p

    url = f"{endpoint}/openai/v1/responses"
    body = json.dumps(payload)

    response_data = await asyncio.to_thread(_sigv4_request, credentials, region, url, body)

    output = response_data.get("output", [])
    text = ""
    for item in output:
        if item.get("type") == "message":
            for content_block in item.get("content", []):
                if content_block.get("type") == "output_text":
                    text += content_block.get("text", "")

    usage_data = response_data.get("usage", {})
    prompt_tokens = usage_data.get("input_tokens", 0)
    completion_tokens = usage_data.get("output_tokens", 0)

    return ChatCompletionResponse(
        id=response_data.get("id", "mantle-response"),
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": response_data.get("status", "completed"),
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )


async def _invoke_messages_api(
    credentials,
    endpoint: str,
    region: str,
    request: ChatCompletionRequest,
    mapping: ProviderModelMapping,
) -> ChatCompletionResponse:
    """Call the Anthropic Messages API on Mantle for Claude models."""
    messages = []
    system_text = request.system or ""

    for msg in request.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_text = content
        else:
            messages.append({"role": role, "content": content})

    payload: dict = {
        "model": mapping.model_id,
        "messages": messages,
        "max_tokens": request.max_tokens or 4096,
    }
    if system_text:
        payload["system"] = system_text
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p

    payload["anthropic_version"] = "2023-06-01"

    url = f"{endpoint}/anthropic/v1/messages"
    body = json.dumps(payload)

    response_data = await asyncio.to_thread(_sigv4_request, credentials, region, url, body)

    content_blocks = response_data.get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

    usage_data = response_data.get("usage", {})
    prompt_tokens = usage_data.get("input_tokens", 0)
    completion_tokens = usage_data.get("output_tokens", 0)

    return ChatCompletionResponse(
        id=response_data.get("id", "mantle-response"),
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": response_data.get("stop_reason", "end_turn"),
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )
