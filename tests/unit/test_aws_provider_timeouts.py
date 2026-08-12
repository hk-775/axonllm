"""AWS provider transports honor concrete route timeout policy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import threading

import pytest

import src.gateway.bedrock_provider as bedrock_provider
import src.gateway.mantle_provider as mantle_provider
from src.gateway.models import ChatCompletionRequest, ProviderModelMapping
from src.gateway.router import ProviderError


class _ServiceError(Exception):
    pass


_BEDROCK_EXCEPTIONS = SimpleNamespace(
    AccessDeniedException=_ServiceError,
    ResourceNotFoundException=_ServiceError,
    ThrottlingException=_ServiceError,
)


def test_bedrock_client_uses_configured_connect_and_read_timeouts(
    monkeypatch,
) -> None:
    captured: dict = {}

    def client(service: str, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(bedrock_provider.boto3, "client", client)

    bedrock_provider.create_bedrock_provider_fn(
        region="us-east-1",
        connect_timeout=6,
        read_timeout=44,
    )

    assert captured["service"] == "bedrock-runtime"
    assert captured["config"].connect_timeout == 6
    assert captured["config"].read_timeout == 44


async def test_bedrock_request_has_hard_wall_clock_deadline(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _Client:
        exceptions = _BEDROCK_EXCEPTIONS

        def converse(self, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            return {}

    monkeypatch.setattr(
        bedrock_provider.boto3,
        "client",
        lambda *_args, **_kwargs: _Client(),
    )
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="launch-model",
    )
    provider_fn = bedrock_provider.create_bedrock_provider_fn(
        connect_timeout=0.01,
        read_timeout=0.01,
    )(request)
    mapping = ProviderModelMapping(
        provider="bedrock",
        model_id="amazon.nova-pro-v1:0",
    )

    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(ProviderError) as caught:
            await provider_fn(mapping)
    finally:
        release.set()
    elapsed = asyncio.get_running_loop().time() - started

    assert entered.is_set()
    assert elapsed < 0.5
    assert caught.value.status_code == 504
    assert caught.value.message == "Bedrock request timed out"


async def test_bedrock_invoke_body_is_bounded_and_closed(monkeypatch) -> None:
    class _Body:
        def __init__(self) -> None:
            self.chunks = [b'{"content":"', b"oversized", b'"}']
            self.closed = False

        def read(self, _amount: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

        def close(self) -> None:
            self.closed = True

    body = _Body()

    class _Client:
        exceptions = _BEDROCK_EXCEPTIONS

        def invoke_model(self, **_kwargs):
            return {"body": body}

    monkeypatch.setattr(
        bedrock_provider,
        "_MAX_BEDROCK_RESPONSE_BYTES",
        8,
    )
    monkeypatch.setattr(
        bedrock_provider.boto3,
        "client",
        lambda *_args, **_kwargs: _Client(),
    )
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="launch-model",
    )
    provider_fn = bedrock_provider.create_bedrock_provider_fn()(request)
    mapping = ProviderModelMapping(
        provider="bedrock",
        model_id="anthropic.claude-test",
    )

    with pytest.raises(ProviderError, match="maximum size") as caught:
        await provider_fn(mapping)

    assert caught.value.status_code == 502
    assert body.closed is True


async def test_bedrock_transport_errors_do_not_expose_secrets(monkeypatch) -> None:
    class _Client:
        exceptions = _BEDROCK_EXCEPTIONS

        def converse(self, **_kwargs):
            raise RuntimeError("credential=provider-secret")

    monkeypatch.setattr(
        bedrock_provider.boto3,
        "client",
        lambda *_args, **_kwargs: _Client(),
    )
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="launch-model",
    )
    provider_fn = bedrock_provider.create_bedrock_provider_fn()(request)
    mapping = ProviderModelMapping(
        provider="bedrock",
        model_id="amazon.nova-pro-v1:0",
    )

    with pytest.raises(ProviderError) as caught:
        await provider_fn(mapping)

    assert caught.value.status_code == 502
    assert caught.value.message == "Bedrock request failed"
    assert "provider-secret" not in str(caught.value)


def test_mantle_http_transport_uses_separate_connect_and_read_timeouts(
    monkeypatch,
) -> None:
    captured: dict = {}

    class _Credentials:
        def get_frozen_credentials(self):
            return object()

    class _Signer:
        def __init__(self, *_args):
            pass

        def add_auth(self, _request) -> None:
            pass

    class _Pool:
        def request(self, method: str, url: str, **kwargs):
            captured.update(method=method, url=url, **kwargs)

            class _Response:
                status = 200

                def __init__(self) -> None:
                    self.body = b"{}"

                def read(self, **_kwargs):
                    body, self.body = self.body, b""
                    return body

                def release_conn(self) -> None:
                    pass

                def close(self) -> None:
                    pass

            return _Response()

    monkeypatch.setattr(mantle_provider, "SigV4Auth", _Signer)
    monkeypatch.setattr(mantle_provider, "_MANTLE_HTTP", _Pool())

    result = mantle_provider._sigv4_request(
        _Credentials(),
        "us-east-1",
        "https://mantle.example/v1/chat/completions",
        "{}",
        connect_timeout=7,
        read_timeout=53,
    )

    assert result == {}
    assert captured["method"] == "POST"
    assert captured["timeout"].connect_timeout == 7
    assert captured["timeout"].read_timeout == 53
    assert captured["pool_timeout"] == 7
    assert captured["retries"] is False
    assert captured["preload_content"] is False


def test_mantle_pool_exhaustion_maps_to_gateway_timeout(monkeypatch) -> None:
    class _Session:
        def get_credentials(self):
            return object()

    async def raise_pool_exhaustion(*_args, **_kwargs):
        raise mantle_provider.urllib3.exceptions.EmptyPoolError(
            None,
            "connection pool exhausted",
        )

    monkeypatch.setattr(mantle_provider.boto3, "Session", lambda: _Session())
    monkeypatch.setattr(
        mantle_provider,
        "_invoke_chat_completions_api",
        raise_pool_exhaustion,
    )

    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="launch-model",
    )
    provider_fn = mantle_provider.create_mantle_provider_fn(
        region="us-east-1",
    )(request)
    mapping = ProviderModelMapping(
        provider="bedrock-mantle",
        model_id="qwen.qwen3-32b",
    )

    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider_fn(mapping))

    assert caught.value.status_code == 504
    assert caught.value.provider == "bedrock-mantle"
    assert caught.value.message == "Bedrock Mantle request timed out"
