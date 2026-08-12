"""Route-level provider balancing, credentials, and transport tests."""

from __future__ import annotations

import asyncio
import random

import aiohttp
import pytest

from src.gateway.http_client import HttpClient
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
    StreamChunk,
    TokenUsage,
)
from src.gateway.multi_provider_factory import MultiProviderFactory
from src.gateway.provider_config import ProviderConfig
from src.gateway.provider_routes import (
    NoAvailableRouteError,
    ProviderRoute,
    ProviderRoutePool,
    RouteLease,
)
from src.gateway.router import ProviderError


def _route(
    route_id: str,
    *,
    endpoint: str = "https://api.openai.com",
    api_key: str = "secret",
    **overrides,
) -> ProviderRoute:
    return ProviderRoute(
        route_id=route_id,
        provider="openai",
        endpoint=endpoint,
        credentials={"api_key": api_key},
        **overrides,
    )


def _response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="response-1",
        choices=[{"message": {"role": "assistant", "content": "ok"}}],
        usage=TokenUsage(10, 4, 14),
        model="gpt-test",
        provider="openai",
    )


def test_route_failure_isolated_and_next_route_remains_available() -> None:
    primary = _route("openai:primary")
    backup = _route("openai:backup")
    pool = ProviderRoutePool([primary, backup], rng=random.Random(1))

    lease = RouteLease(primary, "gpt-test", 0.0)
    pool.record_failure(lease, 401)

    assert not pool.has_available(
        "openai", "gpt-test", exclude_route_id="openai:backup"
    )
    assert pool.has_available(
        "openai", "gpt-test", exclude_route_id="openai:primary"
    )
    assert pool.acquire("openai", "gpt-test").route.route_id == "openai:backup"


def test_adaptive_latency_moves_traffic_to_the_faster_route() -> None:
    fast = _route("openai:fast")
    slow = _route("openai:slow")
    pool = ProviderRoutePool([fast, slow], rng=random.Random(7))

    for _ in range(10):
        pool.record_success(
            RouteLease(fast, "gpt-test", 0.0),
            latency_ms=20,
            output_tokens=10,
        )
        pool.record_success(
            RouteLease(slow, "gpt-test", 0.0),
            latency_ms=200,
            output_tokens=10,
        )

    selected = {"openai:fast": 0, "openai:slow": 0}
    for _ in range(1000):
        lease = pool.acquire("openai", "gpt-test")
        selected[lease.route.route_id] += 1
        pool.release(lease)

    assert selected["openai:fast"] > selected["openai:slow"] * 5


def test_shared_capacity_group_prevents_fake_capacity_from_multiple_keys() -> None:
    routes = [
        _route(
            "openai:key-a",
            capacity_group="shared-account",
            capacity_limit=1,
        ),
        _route(
            "openai:key-b",
            capacity_group="shared-account",
            capacity_limit=1,
        ),
    ]
    pool = ProviderRoutePool(routes, rng=random.Random(2))

    lease = pool.acquire("openai", "gpt-test")
    with pytest.raises(NoAvailableRouteError):
        pool.acquire("openai", "gpt-test")
    pool.release(lease)

    assert pool.acquire("openai", "gpt-test")


def test_route_snapshot_never_exposes_credentials() -> None:
    snapshot = ProviderRoutePool(
        [_route("openai:primary", api_key="must-not-leak")]
    ).snapshot()[0]

    assert snapshot["has_credentials"] is True
    assert snapshot["adaptive_weight"] == 1.0
    assert "credentials" not in snapshot
    assert "must-not-leak" not in repr(snapshot)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@api.example",
        "https://api.example?token=secret",
        "https://api.example#secret",
    ],
)
def test_route_endpoint_rejects_embedded_private_material(endpoint: str) -> None:
    with pytest.raises(ValueError):
        _route("openai:invalid", endpoint=endpoint)


def test_private_header_rotation_changes_route_fingerprint() -> None:
    first = _route(
        "openai:primary",
        extra_headers={"X-Route-Token": "first"},
    )
    rotated = _route(
        "openai:primary",
        extra_headers={"X-Route-Token": "second"},
    )

    assert first.fingerprint() != rotated.fingerprint()


def test_route_preserves_refreshable_credential_provider() -> None:
    credential_provider = type(
        "_Refreshable",
        (),
        {"get_token": lambda self: "token"},
    )()
    config = ProviderConfig(
        provider_name="vertex_ai",
        base_url="https://us-central1-aiplatform.googleapis.com",
        auth_type="gcp_service_account",
        credentials={"credential_source": "google-auth"},
        credential_provider=credential_provider,
        extra_params={"project": "project-a", "location": "us-central1"},
    )

    route = ProviderRoute.from_provider_config(config)

    assert route.credential_provider is credential_provider
    assert route.to_provider_config().credential_provider is credential_provider


def test_hot_route_config_inherits_refreshable_google_credentials() -> None:
    credential_provider = type(
        "_Refreshable",
        (),
        {"get_token": lambda self: "token"},
    )()
    config = ProviderConfig(
        provider_name="vertex_ai",
        base_url="https://us-central1-aiplatform.googleapis.com",
        auth_type="gcp_service_account",
        credentials={"credential_source": "google-auth"},
        credential_provider=credential_provider,
        extra_params={"project": "project-a", "location": "us-central1"},
    )
    factory = MultiProviderFactory({"vertex_ai": config})

    result = factory.configure_routes([
        {
            "route_id": "vertex_ai:secondary",
            "provider": "vertex_ai",
            "endpoint": "https://us-east1-aiplatform.googleapis.com",
            "auth_type": "gcp_service_account",
            "credentials": {"access_token": "must-not-be-used"},
            "extra_params": {
                "project": "project-a",
                "location": "us-east1",
            },
        }
    ])

    configured = factory.config_for(
        "vertex_ai",
        model_id="gemini-test",
    )
    assert result == {"routes": 1, "providers": 1}
    assert configured is not None
    assert configured.credentials == {
        "credential_source": "google-auth"
    }
    assert configured.credential_provider is credential_provider


@pytest.mark.asyncio
async def test_factory_rotates_concrete_credential_and_endpoint_after_401() -> None:
    routes = [
        _route(
            "openai:primary",
            endpoint="https://primary.example",
            api_key="primary-key",
            priority=0,
        ),
        _route(
            "openai:backup",
            endpoint="https://backup.example",
            api_key="backup-key",
            priority=1,
        ),
    ]
    factory = MultiProviderFactory(provider_routes=routes)
    seen: list[tuple[str, str, str]] = []

    async def execute(request, mapping, adapter, config, **kwargs):
        seen.append(
            (config.route_id, config.base_url, config.credentials["api_key"])
        )
        if config.route_id == "openai:primary":
            raise ProviderError(401, mapping.provider, "expired key")
        return _response()

    factory._http_client.execute = execute
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-test",
    )
    mapping = ProviderModelMapping(provider="openai", model_id="gpt-test")
    provider_fn = factory.create(request)

    with pytest.raises(ProviderError) as exc_info:
        await provider_fn(mapping)
    assert exc_info.value.route_id == "openai:primary"
    assert exc_info.value.retryable is True

    response = await provider_fn(mapping)

    assert response.id == "response-1"
    assert seen == [
        ("openai:primary", "https://primary.example", "primary-key"),
        ("openai:backup", "https://backup.example", "backup-key"),
    ]


@pytest.mark.asyncio
async def test_connection_pool_is_shared_by_transport_not_api_key() -> None:
    client = HttpClient()
    first = ProviderConfig(
        provider_name="openai",
        base_url="https://api.example/v1",
        auth_type="api_key",
        credentials={"api_key": "key-a"},
    )
    second = ProviderConfig(
        provider_name="openai",
        base_url="https://api.example/other-path",
        auth_type="api_key",
        credentials={"api_key": "key-b"},
    )
    other_endpoint = ProviderConfig(
        provider_name="openai",
        base_url="https://backup.example/v1",
        auth_type="api_key",
        credentials={"api_key": "key-c"},
    )

    try:
        first_session = client._get_or_create_session(first)
        assert isinstance(first_session.cookie_jar, aiohttp.DummyCookieJar)
        assert client._get_or_create_session(second) is first_session
        assert client._get_or_create_session(other_endpoint) is not first_session
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_removed_transport_pool_stays_open_for_inflight_requests() -> None:
    client = HttpClient()
    config = ProviderConfig(
        provider_name="openai",
        base_url="https://api.example",
        auth_type="api_key",
        credentials={"api_key": "key-a"},
    )
    session = client._get_or_create_session(config)

    client.retain_configs([])

    assert session.closed is False
    assert client._sessions == {}
    assert client._retired_sessions == [session]
    await client.close()
    assert session.closed is True


@pytest.mark.asyncio
async def test_closing_stream_releases_route_capacity() -> None:
    factory = MultiProviderFactory(
        provider_routes=[_route("openai:only", max_concurrency=1)]
    )

    async def stream(*args, **kwargs):
        yield StreamChunk(
            id="chunk-1",
            choices=[{"delta": {"content": "hello"}}],
            model="gpt-test",
        )
        await asyncio.Event().wait()

    factory._http_client.execute_streaming = stream
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-test",
        stream=True,
    )
    mapping = ProviderModelMapping(provider="openai", model_id="gpt-test")
    generator = factory.execute_streaming(request, mapping)

    await generator.__anext__()
    route = next(
        item
        for item in factory.route_snapshot()
        if item["route_id"] == "openai:only"
    )
    assert route["inflight"] == 1
    await generator.aclose()

    route = next(
        item
        for item in factory.route_snapshot()
        if item["route_id"] == "openai:only"
    )
    assert route["inflight"] == 0
