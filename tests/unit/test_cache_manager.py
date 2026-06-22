"""Unit tests for CacheManager."""

import asyncio
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
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=messages or [{"role": "user", "content": "hello"}],
        temperature=temperature,
        max_tokens=max_tokens,
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
    assert "key-exp" not in cm._cache


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
async def test_different_messages_produce_different_cache_keys():
    cm = CacheManager()
    req1 = _make_request(messages=[{"role": "user", "content": "hello"}])
    req2 = _make_request(messages=[{"role": "user", "content": "goodbye"}])
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
