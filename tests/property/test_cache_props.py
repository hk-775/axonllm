# Feature: litellm-service, Property 28: Cache round-trip within TTL
"""Property-based tests for the CacheManager component.

Properties covered:
  28 – Cache serves identical responses within TTL
"""

import asyncio

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.cache_manager import CacheManager
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Shared helpers and strategies
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


model_strategy = st.sampled_from(["gpt-4", "claude-3", "gemini-pro", "command-r"])
project_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=20,
).filter(lambda s: len(s.strip()) > 0)

message_content_strategy = st.text(min_size=1, max_size=100).filter(lambda s: len(s.strip()) > 0)

temperature_strategy = st.one_of(st.none(), st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False))
max_tokens_strategy = st.one_of(st.none(), st.integers(min_value=1, max_value=4096))
ttl_strategy = st.integers(min_value=10, max_value=3600)

response_content_strategy = st.text(min_size=1, max_size=200).filter(lambda s: len(s.strip()) > 0)
token_count_strategy = st.integers(min_value=1, max_value=1000)


# ===========================================================================
# Property 28: Cache serves identical responses within TTL
# Feature: litellm-service, Property 28: Cache round-trip within TTL
# ===========================================================================


@given(
    model=model_strategy,
    content=message_content_strategy,
    temperature=temperature_strategy,
    max_tokens=max_tokens_strategy,
    project_id=project_id_strategy,
    ttl=ttl_strategy,
    resp_content=response_content_strategy,
    prompt_tokens=token_count_strategy,
    completion_tokens=token_count_strategy,
)
@settings(max_examples=100)
def test_cache_round_trip_within_ttl(
    model, content, temperature, max_tokens, project_id, ttl,
    resp_content, prompt_tokens, completion_tokens,
):
    """Property 28: Cache serves identical responses within TTL.

    For any project with caching enabled and any two semantically identical
    requests within TTL, the second request SHALL return the cached response.

    Verifies:
    - compute_cache_key is deterministic (same request+project → same key)
    - put then get within TTL returns the stored response
    - Different request or project produces a different cache key

    **Validates: Requirements 10.6, 10.7**
    """
    cache = CacheManager()

    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": content}],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    response = ChatCompletionResponse(
        id="resp-cached",
        choices=[{"message": {"role": "assistant", "content": resp_content}}],
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=model,
        provider="openai",
    )

    # 1. Deterministic cache key: same request + project → same key
    key1 = cache.compute_cache_key(request, project_id)
    key2 = cache.compute_cache_key(request, project_id)
    assert key1 == key2, (
        f"Cache key should be deterministic, got '{key1}' and '{key2}'"
    )

    # 2. Round-trip: put then get returns the same response
    _run(cache.put(key1, response, ttl))
    cached = _run(cache.get(key1))
    assert cached is response, (
        "get() within TTL should return the exact response object that was put()"
    )

    # 3. Different project → different key
    other_project = project_id + "-other"
    key_other_project = cache.compute_cache_key(request, other_project)
    assert key1 != key_other_project, (
        "Different project_id must produce a different cache key"
    )

    # 4. Different request (different model) → different key
    different_request = ChatCompletionRequest(
        messages=[{"role": "user", "content": content}],
        model=model + "-different",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    key_diff_request = cache.compute_cache_key(different_request, project_id)
    assert key1 != key_diff_request, (
        "Different request must produce a different cache key"
    )

    # 5. Cache miss for a key that was never stored
    miss = _run(cache.get("nonexistent-key-" + project_id))
    assert miss is None, "Cache miss should return None"
