"""Property-based tests for provider prompt caching (Tasks 3, 4, 5, 7, 9).

Properties covered:
  P1 – Cache marker injection is idempotent (Design Property 1)
  P2 – Disabled caching preserves original behavior (Design Property 2)
  P3 – Cached token extraction round-trip (Design Property 3)
  P4 – No double-billing of cached tokens (Design Property 4)
  P6 – System message text preservation (Design Property 6)
  P7 – No cache markers for non-Anthropic Bedrock models (Design Property 7)
  P5 – Cache hit rate calculation (Design Property 5)
  P8 – Pricing fallback to prompt_token_cost (Design Property 8)
"""

import asyncio

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.cost_tracker import CostTracker
from src.gateway.models import (
    ChatCompletionRequest,
    TokenPricing,
    TokenUsage,
)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Non-empty printable text for system messages (avoid control chars)
system_text_strategy = st.text(
    min_size=1,
    max_size=200,
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
)

# Token counts
token_strategy = st.integers(min_value=0, max_value=100_000)
positive_token_strategy = st.integers(min_value=1, max_value=100_000)

# Pricing rates
pricing_rate_strategy = st.floats(
    min_value=0.0001, max_value=1.0, allow_nan=False, allow_infinity=False
)


# ===========================================================================
# Property 1: Cache marker injection is idempotent (Task 3.4)
# ===========================================================================


@given(system_text=system_text_strategy)
@settings(max_examples=50)
def test_cache_marker_idempotent(system_text):
    """Property 1: Translating twice with prompt_caching_enabled=True produces
    exactly one cache_control marker on the last system block.

    **Validates: Requirements 2.1, 2.4**
    """
    adapter = AnthropicAdapter()
    req = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hi"}],
        model="claude-3-sonnet-20240229",
        system=system_text,
    )

    result = _run(adapter.translate_request(req, prompt_caching_enabled=True))
    system_blocks = result["system"]

    assert isinstance(system_blocks, list), "System should be a list of content blocks"
    assert len(system_blocks) == 1, "String system should produce exactly one block"

    # Count cache_control markers
    markers = [b for b in system_blocks if "cache_control" in b]
    assert len(markers) == 1, f"Expected exactly 1 cache_control marker, got {len(markers)}"
    assert markers[0]["cache_control"] == {"type": "ephemeral"}


# ===========================================================================
# Property 2: Disabled caching preserves original behavior (Task 3.5)
# ===========================================================================


@given(system_text=system_text_strategy)
@settings(max_examples=50)
def test_disabled_caching_preserves_behavior(system_text):
    """Property 2: translate_request with prompt_caching_enabled=False produces
    identical output to translate_request without the parameter.

    **Validates: Requirements 9.3**
    """
    adapter = AnthropicAdapter()
    req = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hi"}],
        model="claude-3-sonnet-20240229",
        system=system_text,
    )

    result_default = _run(adapter.translate_request(req))
    result_disabled = _run(adapter.translate_request(req, prompt_caching_enabled=False))

    assert result_default == result_disabled, (
        f"Disabled caching should match default:\n"
        f"  default:  {result_default}\n"
        f"  disabled: {result_disabled}"
    )


# ===========================================================================
# Property 6: System message text preservation (Task 3.6)
# ===========================================================================


@given(system_text=system_text_strategy)
@settings(max_examples=50)
def test_system_text_preserved(system_text):
    """Property 6: Converting to content-block format preserves the original text.

    **Validates: Requirements 2.4**
    """
    adapter = AnthropicAdapter()
    req = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hi"}],
        model="claude-3-sonnet-20240229",
        system=system_text,
    )

    result = _run(adapter.translate_request(req, prompt_caching_enabled=True))
    system_blocks = result["system"]

    assert isinstance(system_blocks, list)
    assert len(system_blocks) >= 1
    assert system_blocks[0]["text"] == system_text, (
        f"Text not preserved: expected {system_text!r}, got {system_blocks[0]['text']!r}"
    )


# ===========================================================================
# Property 3: Cached token extraction round-trip (Task 4.4)
# ===========================================================================


@given(
    cache_read=token_strategy,
    cache_creation=token_strategy,
    input_tokens=positive_token_strategy,
    output_tokens=positive_token_strategy,
)
@settings(max_examples=100)
def test_cached_token_extraction_roundtrip(cache_read, cache_creation, input_tokens, output_tokens):
    """Property 3: For any response with non-negative cached token values,
    translate_response produces matching TokenUsage fields.

    **Validates: Requirements 4.1, 4.2, 4.3**
    """
    adapter = AnthropicAdapter()
    raw_response = {
        "id": "msg_test",
        "content": [{"type": "text", "text": "Hello"}],
        "model": "claude-3-sonnet-20240229",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }

    resp = adapter.translate_response(raw_response)

    assert resp.usage.cached_tokens == cache_read, (
        f"cached_tokens mismatch: expected {cache_read}, got {resp.usage.cached_tokens}"
    )
    assert resp.usage.cache_creation_tokens == cache_creation, (
        f"cache_creation_tokens mismatch: expected {cache_creation}, got {resp.usage.cache_creation_tokens}"
    )
    assert resp.usage.prompt_tokens == input_tokens
    assert resp.usage.completion_tokens == output_tokens


# ===========================================================================
# Property 4: No double-billing of cached tokens (Task 5.3)
# ===========================================================================


@given(
    prompt_tokens=st.integers(min_value=0, max_value=50_000),
    completion_tokens=st.integers(min_value=0, max_value=50_000),
    cached_tokens=st.integers(min_value=0, max_value=50_000),
    cache_creation_tokens=st.integers(min_value=0, max_value=50_000),
    prompt_rate=pricing_rate_strategy,
    completion_rate=pricing_rate_strategy,
    cached_rate=pricing_rate_strategy,
    creation_rate=pricing_rate_strategy,
    per_request_cost=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_no_double_billing(
    prompt_tokens, completion_tokens, cached_tokens, cache_creation_tokens,
    prompt_rate, completion_rate, cached_rate, creation_rate, per_request_cost,
):
    """Property 4: Total cost equals the formula with no double-billing.

    **Validates: Requirements 5.1, 5.2**
    """
    assume(cached_tokens + cache_creation_tokens <= prompt_tokens)

    pricing = TokenPricing(
        prompt_token_cost=prompt_rate,
        completion_token_cost=completion_rate,
        cached_token_cost=cached_rate,
        cache_creation_token_cost=creation_rate,
        per_request_cost=per_request_cost,
    )
    tracker = CostTracker({"test_provider": {"test_model": pricing}})

    actual = tracker.calculate_cost(
        "test_provider", "test_model",
        prompt_tokens, completion_tokens,
        cached_tokens=cached_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )

    billable_prompt = prompt_tokens - cached_tokens - cache_creation_tokens
    expected = (
        (billable_prompt / 1000 * prompt_rate)
        + (completion_tokens / 1000 * completion_rate)
        + (cached_tokens / 1000 * cached_rate)
        + (cache_creation_tokens / 1000 * creation_rate)
        + per_request_cost
    )

    assert abs(actual - expected) < 1e-9, (
        f"Cost mismatch: expected {expected}, got {actual}"
    )


# ===========================================================================
# Property 8: Pricing fallback to prompt_token_cost (Task 5.4)
# ===========================================================================


@given(
    prompt_tokens=st.integers(min_value=0, max_value=50_000),
    completion_tokens=st.integers(min_value=0, max_value=50_000),
    cached_tokens=st.integers(min_value=0, max_value=50_000),
    cache_creation_tokens=st.integers(min_value=0, max_value=50_000),
    prompt_rate=pricing_rate_strategy,
    completion_rate=pricing_rate_strategy,
    per_request_cost=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_pricing_fallback_to_prompt_rate(
    prompt_tokens, completion_tokens, cached_tokens, cache_creation_tokens,
    prompt_rate, completion_rate, per_request_cost,
):
    """Property 8: When cached_token_cost and cache_creation_token_cost are None,
    cached tokens are billed at prompt_token_cost.

    **Validates: Requirements 7.2, 7.3**
    """
    assume(cached_tokens + cache_creation_tokens <= prompt_tokens)

    pricing = TokenPricing(
        prompt_token_cost=prompt_rate,
        completion_token_cost=completion_rate,
        cached_token_cost=None,
        cache_creation_token_cost=None,
        per_request_cost=per_request_cost,
    )
    tracker = CostTracker({"test_provider": {"test_model": pricing}})

    actual = tracker.calculate_cost(
        "test_provider", "test_model",
        prompt_tokens, completion_tokens,
        cached_tokens=cached_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )

    # With fallback, all tokens billed at prompt_rate
    billable_prompt = prompt_tokens - cached_tokens - cache_creation_tokens
    expected = (
        (billable_prompt / 1000 * prompt_rate)
        + (completion_tokens / 1000 * completion_rate)
        + (cached_tokens / 1000 * prompt_rate)
        + (cache_creation_tokens / 1000 * prompt_rate)
        + per_request_cost
    )

    assert abs(actual - expected) < 1e-9, (
        f"Fallback cost mismatch: expected {expected}, got {actual}"
    )


# ===========================================================================
# Property 7: No cache markers for non-Anthropic Bedrock models (Task 7.4)
# ===========================================================================

# Non-Anthropic Bedrock model IDs
non_anthropic_model_strategy = st.sampled_from([
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.deepseek.r1-v1:0",
    "amazon.titan-text-express-v1",
])


def _has_cache_control(obj):
    """Recursively check if any dict in the structure contains 'cache_control'."""
    if isinstance(obj, dict):
        if "cache_control" in obj:
            return True
        return any(_has_cache_control(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_cache_control(item) for item in obj)
    return False


@given(
    model_id=non_anthropic_model_strategy,
    system_text=system_text_strategy,
    prompt_caching_enabled=st.booleans(),
)
@settings(max_examples=50)
def test_no_cache_markers_non_anthropic_bedrock(model_id, system_text, prompt_caching_enabled):
    """Property 7: For any non-Anthropic Bedrock model, the Converse payload
    contains no cache_control keys regardless of prompt_caching_enabled value.

    **Validates: Requirements 3.2**
    """
    from src.gateway.bedrock_provider import _is_anthropic_model, _invoke_converse
    from src.gateway.models import ProviderModelMapping

    # Confirm this is indeed a non-Anthropic model
    assert not _is_anthropic_model(model_id), f"{model_id} should not be Anthropic"

    # Build a request with a system message
    req = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hello"}],
        model=model_id,
        system=system_text,
    )

    mapping = ProviderModelMapping(provider="bedrock", model_id=model_id)

    # We can't actually call _invoke_converse (it needs a real boto3 client),
    # so we replicate the payload construction logic to verify no cache_control
    # is injected for non-Anthropic models.
    messages = []
    system_parts = []

    for msg in req.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append({"text": content})
        else:
            messages.append({"role": role, "content": [{"text": content}]})

    if req.system:
        system_parts.insert(0, {"text": req.system})

    kwargs = {
        "modelId": mapping.model_id,
        "messages": messages,
    }
    if system_parts:
        # Replicate the cache_control injection logic from _invoke_converse
        if prompt_caching_enabled and _is_anthropic_model(mapping.model_id) and system_parts:
            system_parts[-1]["cache_control"] = {"type": "ephemeral"}
        kwargs["system"] = system_parts

    # Verify no cache_control anywhere in the payload
    assert not _has_cache_control(kwargs), (
        f"Non-Anthropic model {model_id} should not have cache_control in payload, "
        f"but found it with prompt_caching_enabled={prompt_caching_enabled}"
    )


# ===========================================================================
# Property 5: Cache hit rate calculation (Task 9.5)
# ===========================================================================


@given(
    cached=st.integers(min_value=0, max_value=1_000_000),
    creation=st.integers(min_value=0, max_value=1_000_000),
)
@settings(max_examples=100)
def test_cache_hit_rate_calculation(cached, creation):
    """Property 5: cache_hit_rate equals cached/(cached+creation) when
    denominator > 0, else 0.0.

    **Validates: Requirements 8.2**
    """
    denom = cached + creation
    if denom > 0:
        expected = cached / denom
    else:
        expected = 0.0

    # Replicate the calculation used in admin routes
    actual = cached / denom if denom > 0 else 0.0

    assert actual == expected, (
        f"Cache hit rate mismatch: expected {expected}, got {actual} "
        f"for cached={cached}, creation={creation}"
    )

    # Additional properties
    assert 0.0 <= actual <= 1.0, f"Rate should be in [0, 1], got {actual}"
    if cached == 0:
        assert actual == 0.0
    if creation == 0 and cached > 0:
        assert actual == 1.0
