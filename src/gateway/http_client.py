"""HTTP client for communicating with LLM provider APIs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import aiohttp

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    StreamChunk,
)
from src.gateway.provider_config import (
    ProviderConfig,
    build_provider_url,
    get_auth_headers,
)
from src.gateway.router import ProviderError

# Provider-specific headers added automatically.
_PROVIDER_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}


class HttpClient:
    """Async HTTP client that sends requests to LLM provider endpoints.

    Maintains a lazy per-provider ``aiohttp.ClientSession`` pool keyed by
    provider name.  Sessions are created on first use and reused for
    subsequent requests to the same provider.  ``close()`` tears down
    every open session.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, aiohttp.ClientSession] = {}

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(self, config: ProviderConfig) -> aiohttp.ClientSession:
        """Return an existing session for the provider, or create one."""
        name = config.provider_name
        session = self._sessions.get(name)
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(
                connect=config.connect_timeout,
                total=config.read_timeout,
            )
            session = aiohttp.ClientSession(timeout=timeout)
            self._sessions[name] = session
        return session

    async def close(self) -> None:
        """Close all open provider sessions."""
        for session in self._sessions.values():
            if not session.closed:
                await session.close()
        self._sessions.clear()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Non-streaming execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        request: ChatCompletionRequest,
        mapping: ProviderModelMapping,
        adapter: ProviderAdapter,
        config: ProviderConfig,
        prompt_caching_enabled: bool = False,
    ) -> ChatCompletionResponse:
        """Send a non-streaming request and return the translated response.

        Steps:
        1. Translate the unified request via the adapter.
        2. Build auth + provider-specific headers.
        3. POST JSON to the provider endpoint.
        4. On 2xx – parse JSON and translate through the adapter.
        5. On non-2xx – raise ``ProviderError`` with the matching status code.
        6. On network error – raise ``ProviderError(502)``.
        """
        # 1. Translate request
        payload = await adapter.translate_request(request, prompt_caching_enabled=prompt_caching_enabled)

        # Override model with the actual provider model ID (not the gateway model name)
        if "model" in payload:
            payload["model"] = mapping.model_id

        # Always use non-streaming for the execute path (streaming uses execute_streaming)
        payload.pop("stream", None)

        # 2. Build URL
        url = build_provider_url(config, mapping)

        # 3. Assemble headers
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_auth_headers(config))
        headers.update(_PROVIDER_HEADERS.get(config.provider_name, {}))
        if prompt_caching_enabled and config.provider_name == "anthropic":
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        headers.update(config.extra_headers)

        # 4. Send request
        session = self._get_or_create_session(config)
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                body_text = await resp.text()
                if 200 <= resp.status < 300:
                    body = await resp.json(content_type=None)
                    return adapter.translate_response(body)
                raise ProviderError(
                    status_code=resp.status,
                    provider=mapping.provider,
                    message=body_text,
                )
        except ProviderError:
            raise
        except aiohttp.ClientError as exc:
            raise ProviderError(
                status_code=502,
                provider=mapping.provider,
                message=f"Network error: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # Streaming (SSE) execution
    # ------------------------------------------------------------------

    async def execute_streaming(
        self,
        request: ChatCompletionRequest,
        mapping: ProviderModelMapping,
        adapter: ProviderAdapter,
        config: ProviderConfig,
        prompt_caching_enabled: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """Send a streaming request and yield translated ``StreamChunk`` objects.

        Steps:
        1. Translate the unified request via the adapter.
        2. Build auth + provider-specific headers.
        3. POST to the provider endpoint and read the response as an SSE stream.
        4. On non-2xx before streaming begins – raise ``ProviderError``.
        5. Parse ``data:`` lines, skip ``[DONE]``, translate chunks via adapter.
        6. Yield each ``StreamChunk``; stop on ``is_final=True``.
        7. On network error during streaming – raise ``ProviderError(502)``.
        """
        # 1. Translate request
        payload = await adapter.translate_request(request, prompt_caching_enabled=prompt_caching_enabled)

        # Override model with the actual provider model ID
        if "model" in payload:
            payload["model"] = mapping.model_id

        # 2. Build URL
        url = build_provider_url(config, mapping)

        # 3. Assemble headers
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_auth_headers(config))
        headers.update(_PROVIDER_HEADERS.get(config.provider_name, {}))
        if prompt_caching_enabled and config.provider_name == "anthropic":
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        headers.update(config.extra_headers)

        # 4. Send request and stream response
        session = self._get_or_create_session(config)
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                # Pre-flight check: non-2xx before streaming begins
                if not (200 <= resp.status < 300):
                    body_text = await resp.text()
                    raise ProviderError(
                        status_code=resp.status,
                        provider=mapping.provider,
                        message=body_text,
                    )

                # Read SSE lines from the response body
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

                    if not line.startswith("data: "):
                        continue

                    data = line[len("data: "):]

                    if data == "[DONE]":
                        continue

                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    chunk = adapter.translate_stream_chunk(parsed)
                    yield chunk

                    if chunk.is_final:
                        return
        except ProviderError:
            raise
        except aiohttp.ClientError as exc:
            raise ProviderError(
                status_code=502,
                provider=mapping.provider,
                message=f"Streaming network error: {exc}",
            ) from exc
