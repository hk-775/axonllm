"""Bedrock Mantle provider — routes to the right Mantle API by model via SigV4.

Uses the bedrock-mantle.{region}.api.aws endpoint, which exposes three
inference APIs, each serving a different subset of models:
- Anthropic Messages API (/anthropic/v1/messages) — Claude models (anthropic.*)
- OpenAI Responses API (/openai/v1/responses) — frontier GPT models (gpt-5.x)
- Chat Completions API (/v1/chat/completions) — everything else, including
  gpt-oss, DeepSeek, Qwen, and other open-weight families

Model IDs are prefixed by family (anthropic.*, openai.*, deepseek.*, qwen.*,
...). The prefix does NOT uniquely determine the API: openai.gpt-5.6-* uses
the Responses API while openai.gpt-oss-* uses Chat Completions. We therefore
pick a preferred API by heuristic and fall back to Chat Completions when the
provider reports the model is not supported on the chosen route.

Auth: SigV4 (bedrock service). All billing flows through the AWS account.
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

_ANTHROPIC_PREFIXES = ("anthropic.",)
# openai.* models split across two APIs: the frontier gpt-5.x line uses the
# Responses API; open-weight openai.gpt-oss-* uses Chat Completions.
_RESPONSES_PREFIXES = ("openai.gpt-5", "openai.gpt-4", "openai.o1", "openai.o3", "openai.o4")


def _is_anthropic_model(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _ANTHROPIC_PREFIXES)


def _prefers_responses_api(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _RESPONSES_PREFIXES)


def _is_unsupported_route_error(exc: ProviderError) -> bool:
    """True when Mantle rejects a model for the chosen API path (not a real failure)."""
    msg = exc.message.lower()
    return exc.status_code == 400 and (
        "does not support" in msg or "isn't supported on this route" in msg
    )


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
            model_id = mapping.model_id
            try:
                # Anthropic models always use the Messages API.
                if _is_anthropic_model(model_id):
                    return await _invoke_messages_api(
                        credentials, endpoint, region, request, mapping,
                    )
                # Frontier GPT models use the Responses API; if Mantle reports
                # the model isn't supported there, fall back to Chat Completions.
                if _prefers_responses_api(model_id):
                    try:
                        return await _invoke_responses_api(
                            credentials, endpoint, region, request, mapping,
                        )
                    except ProviderError as exc:
                        if not _is_unsupported_route_error(exc):
                            raise
                # Everything else (gpt-oss, DeepSeek, Qwen, ...) uses Chat Completions.
                return await _invoke_chat_completions_api(
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


async def _invoke_chat_completions_api(
    credentials,
    endpoint: str,
    region: str,
    request: ChatCompletionRequest,
    mapping: ProviderModelMapping,
) -> ChatCompletionResponse:
    """Call the OpenAI-compatible Chat Completions API on Mantle.

    Serves open-weight families (gpt-oss, DeepSeek, Qwen, etc.) at the
    top-level /v1/chat/completions path and returns standard OpenAI
    chat.completion JSON.
    """
    messages = list(request.messages)
    if request.system and not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": request.system}, *messages]

    payload: dict = {
        "model": mapping.model_id,
        "messages": messages,
    }
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p

    url = f"{endpoint}/v1/chat/completions"
    body = json.dumps(payload)

    response_data = await asyncio.to_thread(_sigv4_request, credentials, region, url, body)

    choices = response_data.get("choices", [])
    first = choices[0] if choices else {}
    message = first.get("message", {})
    text = message.get("content") or ""

    usage_data = response_data.get("usage", {})
    prompt_tokens = usage_data.get("prompt_tokens", 0)
    completion_tokens = usage_data.get("completion_tokens", 0)

    return ChatCompletionResponse(
        id=response_data.get("id", "mantle-response"),
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": first.get("finish_reason", "stop"),
        }],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
        ),
        model=mapping.model_id,
        provider=mapping.provider,
    )
