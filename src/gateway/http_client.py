"""HTTP client for communicating with LLM provider APIs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import urlparse

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
    build_provider_stream_url,
    get_auth_headers,
)
from src.gateway.router import ProviderError

# Provider-specific headers added automatically.
_PROVIDER_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}


class HttpClient:
    """Async HTTP client that sends requests to LLM provider endpoints.

    Maintains lazy sessions keyed by transport identity: endpoint authority,
    proxy/TLS identity, timeout policy, and pool limits. Credentials are applied
    per request, so two keys for the same endpoint share connections safely.
    """

    def __init__(self) -> None:
        self._sessions: dict[object, aiohttp.ClientSession] = {}
        self._retired_sessions: list[aiohttp.ClientSession] = []

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @staticmethod
    def transport_key(config: ProviderConfig) -> tuple:
        """Return the connection-pool identity for a concrete route."""
        parsed = urlparse(config.base_url)
        authority = (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port,
        )
        return (
            authority,
            config.extra_params.get("proxy_url", ""),
            config.extra_params.get("tls_identity", ""),
            config.connect_timeout,
            config.read_timeout,
            config.max_connections,
            config.max_connections_per_host,
            config.keepalive_timeout,
        )

    def _get_or_create_session(self, config: ProviderConfig) -> aiohttp.ClientSession:
        """Return an existing session for the route transport, or create one."""
        key = self.transport_key(config)
        session = self._sessions.get(key)
        # Keep compatibility with callers/tests that injected a provider-keyed
        # session before pools became transport-keyed.
        if session is None:
            session = self._sessions.get(config.provider_name)
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(
                connect=config.connect_timeout,
                total=config.read_timeout,
            )
            connector = aiohttp.TCPConnector(
                limit=config.max_connections,
                limit_per_host=config.max_connections_per_host,
                keepalive_timeout=config.keepalive_timeout,
                ttl_dns_cache=300,
            )
            session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            self._sessions[key] = session
        return session

    def retain_configs(self, configs: list[ProviderConfig]) -> None:
        """Retire pools absent from the catalog without interrupting requests.

        Catalog replacement can happen while a response is streaming. Stale
        sessions are removed from future selection immediately but remain open
        until factory shutdown so their in-flight requests can finish.
        """
        active = {self.transport_key(config) for config in configs}
        stale = [
            self._sessions.pop(key)
            for key in list(self._sessions)
            if key not in active
        ]
        for session in stale:
            if not any(retired is session for retired in self._retired_sessions):
                self._retired_sessions.append(session)

    async def close(self) -> None:
        """Close all open provider sessions."""
        for session in self._sessions.values():
            if not session.closed:
                await session.close()
        for session in self._retired_sessions:
            if not session.closed:
                await session.close()
        self._sessions.clear()
        self._retired_sessions.clear()

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
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=config.extra_params.get("proxy_url") or None,
            ) as resp:
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

        # 2. Build URL (streaming variant)
        url = build_provider_stream_url(config, mapping)

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
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=config.extra_params.get("proxy_url") or None,
            ) as resp:
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

                    # NB: do NOT stop on chunk.is_final. Providers that report
                    # usage in-stream (OpenAI stream_options.include_usage) send
                    # the usage chunk AFTER the finish_reason chunk, so returning
                    # on is_final would drop it. Read until the SSE [DONE]
                    # sentinel (above) or the connection closes.
        except ProviderError:
            raise
        except aiohttp.ClientError as exc:
            raise ProviderError(
                status_code=502,
                provider=mapping.provider,
                message=f"Streaming network error: {exc}",
            ) from exc
