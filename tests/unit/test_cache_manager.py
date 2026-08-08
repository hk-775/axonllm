"""Unit tests for CacheManager."""

import asyncio
import copy
import pickle
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.gateway.cache_manager import CacheManager
from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse, TokenUsage


def _make_request(
    model: str = "gpt-4",
    messages: list[dict] | None = None,
    temperature: float | None = 0.7,
    max_tokens: int | None = 100,
    system: str | None = None,
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=messages or [{"role": "user", "content": "hello"}],
        temperature=temperature,
        max_tokens=max_tokens,
        system=system,
    )


def _make_response(content: str = "Hi there!") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="gpt-4",
        provider="openai",
    )


@pytest.mark.asyncio
async def test_cache_miss_returns_none():
    cm = CacheManager()
    result = await cm.get("nonexistent-key")
    assert result is None


@pytest.mark.asyncio
async def test_put_then_get_returns_response():
    cm = CacheManager()
    response = _make_response()
    await cm.put("key-1", response, ttl_seconds=300)
    cached = await cm.get("key-1")
    assert cached is response


@pytest.mark.asyncio
async def test_expired_entry_returns_none():
    cm = CacheManager()
    response = _make_response()

    now = datetime(2025, 1, 1, 12, 0, 0)
    expired = now + timedelta(seconds=61)

    with patch("src.gateway.cache_manager.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await cm.put("key-exp", response, ttl_seconds=60)

        mock_dt.now.return_value = expired
        result = await cm.get("key-exp")

    assert result is None


@pytest.mark.asyncio
async def test_expired_entry_is_cleaned_up():
    cm = CacheManager()
    response = _make_response()

    now = datetime(2025, 1, 1, 12, 0, 0)
    expired = now + timedelta(seconds=61)

    with patch("src.gateway.cache_manager.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await cm.put("key-exp", response, ttl_seconds=60)

        mock_dt.now.return_value = expired
        await cm.get("key-exp")

    # Entry should have been removed from internal cache
    assert (None, "key-exp") not in cm._cache


@pytest.mark.asyncio
async def test_same_request_produces_same_cache_key():
    cm = CacheManager()
    req = _make_request()
    key1 = cm.compute_cache_key(req, "proj-1")
    key2 = cm.compute_cache_key(req, "proj-1")
    assert key1 == key2


@pytest.mark.asyncio
async def test_different_requests_produce_different_cache_keys():
    cm = CacheManager()
    req1 = _make_request(model="gpt-4")
    req2 = _make_request(model="claude-3")
    key1 = cm.compute_cache_key(req1, "proj-1")
    key2 = cm.compute_cache_key(req2, "proj-1")
    assert key1 != key2


@pytest.mark.asyncio
async def test_different_projects_produce_different_cache_keys():
    cm = CacheManager()
    req = _make_request()
    key1 = cm.compute_cache_key(req, "proj-1")
    key2 = cm.compute_cache_key(req, "proj-2")
    assert key1 != key2


@pytest.mark.asyncio
async def test_same_project_in_different_tenants_produces_different_cache_keys():
    cm = CacheManager()
    req = _make_request()
    key1 = cm.compute_cache_key(req, "shared-project", "tenant-a")
    key2 = cm.compute_cache_key(req, "shared-project", "tenant-b")
    assert key1 != key2


@pytest.mark.asyncio
async def test_raw_key_retrieval_requires_matching_tenant_scope():
    cm = CacheManager()
    response = _make_response("tenant a")
    await cm.put("shared-key", response, 300, tenant_id="tenant-a")

    assert await cm.get("shared-key") is None
    assert await cm.get("shared-key", tenant_id="tenant-b") is None
    assert await cm.get("shared-key", tenant_id="tenant-a") is response


@pytest.mark.asyncio
async def test_computed_key_carries_scope_for_existing_gateway_call_shape():
    cm = CacheManager()
    request = _make_request()
    response = _make_response("tenant a")
    key = cm.compute_cache_key(request, "shared-project", "tenant-a")

    await cm.put(key, response, 300)

    assert await cm.get(key) is response
    assert await cm.get(key, tenant_id="tenant-b") is None
    assert await cm.get(str(key)) is None
    assert await cm.get(str(key), tenant_id="tenant-a") is response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "round_trip",
    [
        copy.copy,
        copy.deepcopy,
        lambda value: pickle.loads(pickle.dumps(value)),
    ],
)
async def test_computed_key_keeps_scope_through_object_round_trip(round_trip):
    cm = CacheManager()
    request = _make_request()
    response = _make_response("tenant a")
    key = cm.compute_cache_key(request, "shared-project", "tenant-a")
    round_tripped_key = round_trip(key)

    await cm.put(round_tripped_key, response, 300)

    assert await cm.get(round_tripped_key) is response
    assert await cm.get(round_tripped_key, tenant_id="tenant-b") is None


@pytest.mark.asyncio
async def test_digest_collision_cannot_overwrite_another_tenant():
    cm = CacheManager()
    request = _make_request()
    tenant_a_response = _make_response("tenant a")
    tenant_b_response = _make_response("tenant b")

    with patch("src.gateway.cache_manager.hashlib.sha256") as sha256:
        sha256.return_value.hexdigest.return_value = "forced-collision"
        tenant_a_key = cm.compute_cache_key(
            request,
            "shared-project",
            "tenant-a",
        )
        tenant_b_key = cm.compute_cache_key(
            request,
            "shared-project",
            "tenant-b",
        )

    assert tenant_a_key == tenant_b_key
    await cm.put(tenant_a_key, tenant_a_response, 300)
    await cm.put(tenant_b_key, tenant_b_response, 300)

    assert await cm.get(tenant_a_key) is tenant_a_response
    assert await cm.get(tenant_b_key) is tenant_b_response
    assert len(cm._cache) == 2


@pytest.mark.asyncio
async def test_different_messages_produce_different_cache_keys():
    cm = CacheManager()
    req1 = _make_request(messages=[{"role": "user", "content": "hello"}])
    req2 = _make_request(messages=[{"role": "user", "content": "goodbye"}])
    key1 = cm.compute_cache_key(req1, "proj-1")
    key2 = cm.compute_cache_key(req2, "proj-1")
    assert key1 != key2


@pytest.mark.asyncio
async def test_different_system_instructions_produce_different_cache_keys():
    cm = CacheManager()
    req1 = _make_request(system="Answer for customers.")
    req2 = _make_request(system="Answer for administrators.")
    key1 = cm.compute_cache_key(req1, "proj-1")
    key2 = cm.compute_cache_key(req2, "proj-1")
    assert key1 != key2


@pytest.mark.asyncio
async def test_different_temperature_produces_different_cache_key():
    cm = CacheManager()
    req1 = _make_request(temperature=0.5)
    req2 = _make_request(temperature=0.9)
    key1 = cm.compute_cache_key(req1, "proj-1")
    key2 = cm.compute_cache_key(req2, "proj-1")
    assert key1 != key2
